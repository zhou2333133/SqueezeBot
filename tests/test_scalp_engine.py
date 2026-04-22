import unittest

from bot_scalp import BinanceScalpBot, ScalpPosition
from config import config_manager


class TestScalpEngine(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_settings = config_manager.settings.copy()
        config_manager.settings.update({
            "FEE_RATE_PER_SIDE": 0.0004,
            "SLIPPAGE_RATE_PER_SIDE": 0.0005,
            "BREAKOUT_MIN_PCT": 0.10,
            "BREAKOUT_ATR_MULT": 0.7,
            "BREAKOUT_ATR_MIN_PCT": 0.50,
            "BREAKOUT_ATR_MAX_PCT": 1.20,
        })

    def tearDown(self) -> None:
        config_manager.settings.clear()
        config_manager.settings.update(self._orig_settings)

    def test_close_segment_records_net_after_costs(self) -> None:
        bot = BinanceScalpBot()
        pos = ScalpPosition(
            symbol="BTCUSDT",
            direction="LONG",
            entry_price=100.0,
            quantity=1.0,
            quantity_remaining=1.0,
            sl_price=95.0,
            tp1_price=110.0,
            tp2_price=120.0,
            risk_usdt=5.0,
        )

        segment = bot._apply_close_segment(pos, 110.0, 0.4)

        self.assertAlmostEqual(segment["gross"], 4.0, places=6)
        self.assertAlmostEqual(segment["fee"], 0.0336, places=6)
        self.assertAlmostEqual(segment["slippage"], 0.042, places=6)
        self.assertAlmostEqual(segment["net"], 3.9244, places=6)
        self.assertAlmostEqual(pos.quantity_remaining, 0.6, places=6)
        self.assertAlmostEqual(pos.realized_pnl, 3.9244, places=6)

    def test_market_state_detects_range_chop(self) -> None:
        bot = BinanceScalpBot()
        prices = [100 + (0.03 if i % 2 else -0.03) for i in range(60)]
        bot.kline_buffer["BTCUSDT"] = [
            {"o": p, "h": p + 0.05, "l": p - 0.05, "c": p, "q": 1000.0, "Q": 500.0}
            for p in prices
        ]

        self.assertEqual(bot._check_market_state("BTCUSDT", "LONG"), "RANGE_CHOP")

    def test_entry_context_snapshot_uses_existing_taker_helper(self) -> None:
        bot = BinanceScalpBot()
        bot.candidate_meta["TESTUSDT"] = {
            "rank": 1,
            "direction_bias": "ANY",
            "change_24h": 12.5,
            "volume_24h": 1_000_000,
        }
        bot.kline_buffer["TESTUSDT"] = [
            {"o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0 + i * 0.01, "q": 1000.0, "Q": 550.0}
            for i in range(60)
        ]
        bot._live_candle["TESTUSDT"] = {
            "h": 101.5,
            "l": 100.5,
            "taker_buy": 700.0,
            "total_vol": 1000.0,
            "close": 101.0,
            "open": 100.8,
        }

        ctx = bot._entry_context_snapshot("TESTUSDT", "LONG", 0.25, "动能突破多", "UNKNOWN")

        self.assertEqual(ctx["symbol"], "TESTUSDT")
        self.assertEqual(ctx["current_taker_ratio"], 0.7)
        self.assertEqual(ctx["candidate_rank"], 1)

    def test_breakout_atr_filter_respects_configured_window(self) -> None:
        bot = BinanceScalpBot()

        self.assertFalse(bot._breakout_atr_allowed(0.30))
        self.assertTrue(bot._breakout_atr_allowed(0.75))
        self.assertFalse(bot._breakout_atr_allowed(1.50))


if __name__ == "__main__":
    unittest.main()
