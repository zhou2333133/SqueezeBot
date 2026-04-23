import asyncio
import unittest

from bot_scalp import BinanceScalpBot
from config import config_manager
from signals import scalp_signals_history


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

    async def place_limit_ioc_order(self, symbol, side, quantity, price):
        return {"orderId": 1, "executedQty": str(quantity), "avgPrice": "100", "status": "FILLED"}

    async def place_stop_loss_order(self, symbol, side, stop_price):
        return None

    async def place_reduce_only_market_order(self, symbol, side, quantity):
        self.emergency_closed = True
        return {"orderId": 2}


class TestScalpLiveSafety(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_settings = config_manager.settings.copy()
        self._orig_signals = list(scalp_signals_history)
        scalp_signals_history.clear()
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
        })

    def tearDown(self) -> None:
        config_manager.settings.clear()
        config_manager.settings.update(self._orig_settings)
        scalp_signals_history.clear()
        scalp_signals_history.extend(self._orig_signals)

    def test_real_entry_emergency_closes_when_exchange_stop_order_fails(self) -> None:
        bot = BinanceScalpBot()
        trader = StopFailTrader()
        bot.session = FakeSession()
        bot.trader = trader

        asyncio.run(bot._execute_entry("TESTUSDT", "LONG", 1.0, "测试突破", "UNKNOWN"))

        self.assertTrue(trader.emergency_closed)
        self.assertNotIn("TESTUSDT", bot.open_positions)
        self.assertEqual(scalp_signals_history[-1]["rejected_reason"], "stop_loss_order_failed_emergency_closed")


if __name__ == "__main__":
    unittest.main()
