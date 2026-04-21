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
            "BREAKOUT_ATR_MULT": 0.5,
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


if __name__ == "__main__":
    unittest.main()
