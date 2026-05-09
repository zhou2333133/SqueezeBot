import asyncio
import unittest
from unittest.mock import patch

from bot_scalp import BinanceScalpBot
from scalp_position import ScalpPosition
from config import config_manager
import signals as signals_mod
from signals import scalp_positions, scalp_signals_history, scalp_trade_history


class FakeTickerResponse:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return {"price": "100"}


class FakeSession:
    def get(self, *args, **kwargs):
        return FakeTickerResponse()


class StopFailTrader:
    def __init__(self):
        self.emergency_closed = False

    async def is_hedge_mode(self):
        return False

    async def set_leverage(self, symbol, leverage):
        return {"symbol": symbol, "leverage": leverage}

    async def place_limit_ioc_order(self, symbol, side, quantity, price, position_side=None):
        return {"orderId": 1, "executedQty": str(quantity), "avgPrice": "100", "status": "FILLED"}

    async def place_stop_loss_order(self, symbol, side, stop_price, quantity=None, position_side=None):
        return None

    async def place_reduce_only_market_order(self, symbol, side, quantity, position_side=None):
        self.emergency_closed = True
        return {"orderId": 2}


class StopAndEmergencyFailTrader(StopFailTrader):
    async def place_reduce_only_market_order(self, symbol, side, quantity, position_side=None):
        self.emergency_closed = True
        return None


class PositionSyncTrader:
    def __init__(self, position=None, reduce_resp=None):
        self.position = position
        self.reduce_resp = reduce_resp
        self.cancel_all_calls = 0
        self.reduce_calls = []
        self.trailing_calls = 0

    async def get_position(self, symbol):
        return self.position

    async def cancel_all_orders(self, symbol):
        self.cancel_all_calls += 1
        return {"code": 200}

    async def place_reduce_only_market_order(self, symbol, side, quantity, position_side=None):
        self.reduce_calls.append((symbol, side, quantity))
        return self.reduce_resp or {"orderId": 9}

    async def place_trailing_stop_order(self, *args, **kwargs):
        self.trailing_calls += 1
        return {"orderId": 10}


class TestScalpLiveSafety(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_settings = config_manager.settings.copy()
        self._orig_signals = list(scalp_signals_history)
        self._orig_trades = list(scalp_trade_history)
        self._orig_positions = dict(scalp_positions)
        self._orig_persist_ledger = signals_mod._persist_ledger
        signals_mod._persist_ledger = lambda: None
        self._feedback_patcher = patch("scanner.knowledge_base.record_trade_feedback")
        self._feedback_patcher.start()
        scalp_positions.clear()
        scalp_signals_history.clear()
        scalp_trade_history.clear()
        config_manager.settings.update({
            "SCALP_AUTO_TRADE": True,
            "SCALP_PAPER_TRADE": False,
            "SCALP_USE_DYNAMIC_SL": False,
            "SCALP_LEVERAGE": 10,
            "SCALP_POSITION_USDT": 100.0,
            "SCALP_STOP_LOSS_PCT": 10.0,
            "SCALP_RISK_PER_TRADE_USDT": 10.0,
            "SCALP_MAX_DAILY_LOSS_USDT": 200.0,
            "SCALP_MAX_DAILY_LOSS_R": 10.0,
            "SCALP_TP1_RR": 1.2,
            "SCALP_TP2_RR": 3.0,
            "SCALP_TP1_RATIO": 0.40,
            "SCALP_TP2_RATIO": 0.30,
            "SCALP_TP3_TRAIL_PCT": 8.0,
            "SCALP_SURF_ENTRY_AI_ENABLED": False,
            "SCALP_USE_YAOBI_CONTEXT": False,
            "SCALP_SKIP_UNKNOWN_STATE": False,
            # 测试不模拟 wick 双 tick 确认；单 tick 即触发 TP/SL
            "SCALP_TP_CONFIRM_TICKS": 1,
        })

    def tearDown(self) -> None:
        config_manager.settings.clear()
        config_manager.settings.update(self._orig_settings)
        scalp_signals_history.clear()
        scalp_signals_history.extend(self._orig_signals)
        scalp_trade_history.clear()
        scalp_trade_history.extend(self._orig_trades)
        scalp_positions.clear()
        scalp_positions.update(self._orig_positions)
        self._feedback_patcher.stop()
        signals_mod._persist_ledger = self._orig_persist_ledger

    def test_real_entry_emergency_closes_when_exchange_stop_order_fails(self) -> None:
        bot = BinanceScalpBot()
        trader = StopFailTrader()
        bot.session = FakeSession()
        bot.trader = trader

        asyncio.run(bot._execute_entry("TESTUSDT", "LONG", 1.0, "测试突破", "UNKNOWN"))

        self.assertTrue(trader.emergency_closed)
        self.assertNotIn("TESTUSDT", bot.open_positions)
        self.assertEqual(scalp_signals_history[-1]["rejected_reason"], "stop_loss_order_failed_emergency_closed")

    def test_real_entry_suspends_when_exchange_stop_and_emergency_close_fail(self) -> None:
        bot = BinanceScalpBot()
        trader = StopAndEmergencyFailTrader()
        bot.session = FakeSession()
        bot.trader = trader

        asyncio.run(bot._execute_entry("TESTUSDT", "LONG", 1.0, "测试突破", "UNKNOWN"))

        self.assertTrue(trader.emergency_closed)
        self.assertIn("TESTUSDT", bot.open_positions)
        self.assertTrue(bot.open_positions["TESTUSDT"].protection_failed)
        self.assertTrue(bot._live_trading_suspended)
        self.assertEqual(
            scalp_signals_history[-1]["rejected_reason"],
            "stop_loss_order_failed_emergency_failed",
        )

    def test_tp2_uses_local_runner_without_exchange_trailing_order(self) -> None:
        bot = BinanceScalpBot()
        trader = PositionSyncTrader()
        bot.trader = trader
        bot.open_positions["RUNUSDT"] = bot_pos = ScalpPosition(
            symbol="RUNUSDT",
            direction="LONG",
            entry_price=100.0,
            quantity=1.0,
            quantity_remaining=0.6,
            sl_price=99.0,
            tp1_price=101.0,
            tp2_price=102.0,
            tp1_hit=True,
            risk_usdt=1.0,
        )

        asyncio.run(bot._check_tp_sl("RUNUSDT", 102.0))

        self.assertTrue(bot_pos.tp2_hit)
        self.assertEqual(trader.trailing_calls, 0)
        self.assertEqual(len(trader.reduce_calls), 1)

    def test_rest_sync_records_trade_when_exchange_position_is_closed(self) -> None:
        bot = BinanceScalpBot()
        bot.trader = PositionSyncTrader(position=None)
        bot.open_positions["SYNCUSDT"] = ScalpPosition(
            symbol="SYNCUSDT",
            direction="LONG",
            entry_price=100.0,
            quantity=1.0,
            quantity_remaining=1.0,
            sl_price=95.0,
            tp1_price=105.0,
            tp2_price=110.0,
            current_price=98.0,
            risk_usdt=5.0,
        )

        asyncio.run(bot._sync_live_position_once("SYNCUSDT"))

        self.assertNotIn("SYNCUSDT", bot.open_positions)
        self.assertEqual(scalp_trade_history[-1]["close_reason"], "EXCHANGE_SYNC_CLOSED")
        self.assertEqual(bot.trader.cancel_all_calls, 1)

    def test_protection_failed_position_retries_reduce_only_exit(self) -> None:
        bot = BinanceScalpBot()
        bot.trader = PositionSyncTrader(position={"positionAmt": "0.5"})
        bot._live_trading_suspended = True
        bot._live_trading_suspended_reason = "TESTUSDT: stop_loss_order_failed_emergency_failed"
        bot.open_positions["TESTUSDT"] = ScalpPosition(
            symbol="TESTUSDT",
            direction="LONG",
            entry_price=100.0,
            quantity=0.5,
            quantity_remaining=0.5,
            sl_price=95.0,
            tp1_price=105.0,
            tp2_price=110.0,
            current_price=99.0,
            risk_usdt=2.5,
            protection_failed=True,
            protection_reason="stop_loss_order_failed_emergency_failed",
        )

        asyncio.run(bot._sync_live_position_once("TESTUSDT"))

        self.assertNotIn("TESTUSDT", bot.open_positions)
        self.assertEqual(bot.trader.reduce_calls, [("TESTUSDT", "SELL", 0.5)])
        self.assertFalse(bot._live_trading_suspended)
        self.assertEqual(scalp_trade_history[-1]["close_reason"], "PROTECTION_FAILED_FORCE_EXIT")

    def test_manual_paper_close_supports_partial_then_full_close(self) -> None:
        bot = BinanceScalpBot()
        bot.open_positions["MANUALUSDT"] = ScalpPosition(
            symbol="MANUALUSDT",
            direction="LONG",
            entry_price=100.0,
            quantity=2.0,
            quantity_remaining=2.0,
            sl_price=95.0,
            tp1_price=105.0,
            tp2_price=110.0,
            current_price=110.0,
            risk_usdt=10.0,
            paper=True,
        )

        first = asyncio.run(bot.manual_close_position("MANUALUSDT", 50))

        self.assertEqual(first["status"], "success")
        self.assertIn("MANUALUSDT", bot.open_positions)
        self.assertAlmostEqual(bot.open_positions["MANUALUSDT"].quantity_remaining, 1.0)
        self.assertEqual(len(scalp_trade_history), 0)

        second = asyncio.run(bot.manual_close_position("MANUALUSDT", 100))

        self.assertEqual(second["status"], "success")
        self.assertNotIn("MANUALUSDT", bot.open_positions)
        self.assertEqual(scalp_trade_history[-1]["close_reason"], "MANUAL_CLOSE")

    def test_startup_reconcile_suspends_when_exchange_has_unknown_live_position(self) -> None:
        class UnknownLiveTrader(PositionSyncTrader):
            async def get_open_positions(self):
                return [{"symbol": "UNKNOWNUSDT", "positionAmt": "1.5", "markPrice": "12.0"}]

        bot = BinanceScalpBot()
        bot.trader = UnknownLiveTrader()

        asyncio.run(bot._startup_reconcile_positions())

        self.assertTrue(bot._live_trading_suspended)


if __name__ == "__main__":
    unittest.main()
