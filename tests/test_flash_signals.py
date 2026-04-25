"""
test_flash_signals.py — V4AF 入场条件 + 智能时间止损单测
"""
import time
import unittest

from bot_flash import (
    detect_4h_exhaustion, detect_1h_lower_high,
    FlashCrashBot, FlashPosition,
)


def _kline(o, h, l, c, v=1000.0, taker_buy_qv=None, qv=None):
    """生成测试用 1H/4H K 线行（match binance_klines.py 输出格式）。"""
    qv = qv if qv is not None else (c * v)
    taker_buy_qv = taker_buy_qv if taker_buy_qv is not None else (qv * 0.5)
    now_ms = int(time.time() * 1000)
    # close_time < now_ms → 视为已收盘，方便 closed_only 取到
    return {
        "open_time": now_ms - 3600_000,
        "o": o, "h": h, "l": l, "c": c,
        "v": v, "qv": qv,
        "close_time": now_ms - 60_000,
        "trades": 100,
        "taker_buy_v": v * 0.5,
        "taker_buy_qv": taker_buy_qv,
    }


class TestDetect4HExhaustion(unittest.TestCase):
    def test_bullish_to_bearish_with_sell_pressure(self) -> None:
        rows = [
            _kline(100, 110, 99, 108),                          # prev bullish (close > open)
            _kline(108, 110, 100, 102, qv=10000, taker_buy_qv=4000),  # cur bearish, taker_buy=0.40
        ]
        ok, detail = detect_4h_exhaustion(rows)
        self.assertTrue(ok)
        self.assertLess(detail["taker_buy_pct_4h"], 0.50)

    def test_no_signal_when_buy_dominant(self) -> None:
        rows = [
            _kline(100, 110, 99, 108),
            _kline(108, 110, 100, 102, qv=10000, taker_buy_qv=7000),  # taker_buy=0.70
        ]
        ok, _ = detect_4h_exhaustion(rows)
        self.assertFalse(ok)

    def test_no_signal_when_prev_bearish(self) -> None:
        rows = [
            _kline(110, 110, 100, 100),       # prev bearish, not "刚转向"
            _kline(100, 102, 95, 96, qv=10000, taker_buy_qv=4000),
        ]
        ok, _ = detect_4h_exhaustion(rows)
        self.assertFalse(ok)

    def test_insufficient_kline(self) -> None:
        ok, detail = detect_4h_exhaustion([])
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "kline_insufficient")


class TestDetect1HLowerHigh(unittest.TestCase):
    def test_lower_high_confirmed(self) -> None:
        # peak at index 4 (high=110), then h=108 → drop 1.8%
        rows = [
            _kline(100, 102, 99, 101),
            _kline(101, 105, 100, 103),
            _kline(103, 108, 102, 107),
            _kline(107, 109, 106, 108),
            _kline(108, 110, 107, 109),  # peak
            _kline(109, 108.5, 105, 106),  # lower high (h=108.5, drop = (110-108.5)/110 = 1.36%)
            _kline(106, 107, 102, 103),
            _kline(103, 104, 100, 101),
        ]
        ok, detail = detect_1h_lower_high(rows, lookback=8, min_drop_pct=1.0)
        self.assertTrue(ok)
        self.assertEqual(detail["peak_high"], 110)
        self.assertGreaterEqual(detail["drop_pct"], 1.0)

    def test_still_climbing_no_lower_high(self) -> None:
        # 价格持续创新高，最新一根就是 peak → peak_is_latest，没有 after-peak 数据
        rows = [
            _kline(100, 105, 99, 104),
            _kline(104, 108, 103, 107),
            _kline(107, 110, 106, 109),
            _kline(109, 112, 108, 111),
            _kline(111, 114, 110, 113),
            _kline(113, 117, 112, 116),
            _kline(116, 119, 115, 118),
            _kline(118, 121, 117, 120),  # latest = peak
        ]
        ok, detail = detect_1h_lower_high(rows, lookback=8, min_drop_pct=0.5)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "peak_is_latest")

    def test_drop_below_threshold(self) -> None:
        # peak high = 110, max drop after = 109.95 → 0.045%, below 0.5% threshold
        rows = [
            _kline(100, 102, 99, 101),
            _kline(101, 105, 100, 103),
            _kline(103, 108, 102, 107),
            _kline(107, 109, 106, 108),
            _kline(108, 110, 107, 109),     # peak
            _kline(109, 109.95, 108, 109),  # drop 0.045%
            _kline(109, 109.9, 108, 109),
            _kline(109, 109.8, 108, 109),
        ]
        ok, detail = detect_1h_lower_high(rows, lookback=8, min_drop_pct=0.5)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "drop_too_small")

    def test_peak_at_latest_no_signal(self) -> None:
        # peak at last bar → no after-peak data, can't confirm lower high
        rows = [
            _kline(100, 102, 99, 101),
            _kline(101, 105, 100, 103),
            _kline(103, 108, 102, 107),
            _kline(107, 109, 106, 108),
            _kline(108, 110, 107, 109),
            _kline(109, 111, 108, 110),
            _kline(110, 113, 109, 112),
            _kline(112, 115, 111, 114),  # latest = peak
        ]
        ok, detail = detect_1h_lower_high(rows, lookback=8, min_drop_pct=0.5)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "peak_is_latest")


class TestSmartTimeStopReview(unittest.TestCase):
    """智能时间止损：到期重评估。"""

    def setUp(self) -> None:
        self.bot = FlashCrashBot()

    def _make_pos(self, **overrides) -> FlashPosition:
        defaults = {
            "symbol": "TESTUSDT", "direction": "SHORT",
            "entry_price": 100.0, "quantity": 1.0,
            "sl_price": 104.0, "trail_pct": 1.5,
            "trail_activation_pct": 1.0, "peak_price": 110.0,
            "paper": True, "next_review_ts": time.time() - 1,
            "best_price": 100.0,
        }
        defaults.update(overrides)
        return FlashPosition(**defaults)

    def test_max_extensions_reached_closes(self) -> None:
        # FLASH_REVIEW_MAX_EXTENSIONS 默认 2
        pos = self._make_pos(extension_count=2)
        decision = self.bot.evaluate_continuation(pos)
        self.assertFalse(decision["keep"])
        self.assertEqual(decision["reason"], "max_extensions_reached")

    def test_no_kline_data_closes(self) -> None:
        pos = self._make_pos()
        # klines_1h 没有 TESTUSDT 数据
        decision = self.bot.evaluate_continuation(pos)
        self.assertFalse(decision["keep"])
        self.assertEqual(decision["reason"], "no_kline_data")

    def test_price_back_above_peak_closes(self) -> None:
        from scanner.sources.binance_klines import klines_1h
        pos = self._make_pos(peak_price=110.0)
        klines_1h._cache["TESTUSDT"] = [_kline(105, 112, 104, 111)]  # cur=111 > peak=110
        try:
            decision = self.bot.evaluate_continuation(pos)
        finally:
            klines_1h._cache.pop("TESTUSDT", None)
        self.assertFalse(decision["keep"])
        self.assertEqual(decision["reason"], "price_back_above_peak")

    def test_loss_exceeds_threshold_closes(self) -> None:
        from scanner.sources.binance_klines import klines_1h
        # entry=100, cur=102, adverse=2% > FLASH_REVIEW_HOLD_LOSS_MAX_PCT=1.0
        pos = self._make_pos(entry_price=100.0)
        klines_1h._cache["TESTUSDT"] = [_kline(101, 102, 100, 102)]
        try:
            decision = self.bot.evaluate_continuation(pos)
        finally:
            klines_1h._cache.pop("TESTUSDT", None)
        self.assertFalse(decision["keep"])
        self.assertEqual(decision["reason"], "loss_exceeds_threshold")

    def test_trend_intact_keeps(self) -> None:
        from scanner.sources.binance_klines import klines_1h, klines_4h
        # entry=100, cur=98, peak=110, no higher high
        pos = self._make_pos(entry_price=100.0, peak_price=110.0)
        klines_1h._cache["TESTUSDT"] = [
            _kline(100, 101, 99, 99),
            _kline(99, 100, 98, 99),
            _kline(99, 100, 97, 98),
            _kline(98, 99, 97, 98),
        ]
        klines_4h._cache["TESTUSDT"] = [
            _kline(100, 101, 95, 96, qv=10000, taker_buy_qv=4000),
        ]
        try:
            decision = self.bot.evaluate_continuation(pos)
        finally:
            klines_1h._cache.pop("TESTUSDT", None)
            klines_4h._cache.pop("TESTUSDT", None)
        self.assertTrue(decision["keep"])
        self.assertEqual(decision["reason"], "trend_intact")
        self.assertGreater(decision["favorable_pct"], 0)


class TestDetectEntry(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = FlashCrashBot()

    def test_blocks_below_24h_gain(self) -> None:
        candidate = {
            "price_change_24h": 5.0, "flash_eligible": True,
        }
        result = self.bot.detect_entry("BUMPUSDT", candidate)
        self.assertIsNone(result)

    def test_blocks_when_not_flash_eligible(self) -> None:
        candidate = {
            "price_change_24h": 30.0, "flash_eligible": False,
        }
        result = self.bot.detect_entry("BUMPUSDT", candidate)
        self.assertIsNone(result)

    def test_full_entry_path(self) -> None:
        from scanner.sources.binance_klines import klines_1h, klines_4h
        candidate = {
            "price_change_24h": 25.0,
            "flash_eligible": True,
            "vesting_phase": "unlock_active",
            "listing_age_days": 120,
            "volume_24h": 5_000_000,
        }
        # 1H：让 lower high 能命中
        klines_1h._cache["BUMPUSDT"] = [
            _kline(100, 102, 99, 101),
            _kline(101, 105, 100, 103),
            _kline(103, 108, 102, 107),
            _kline(107, 109, 106, 108),
            _kline(108, 110, 107, 109),  # peak
            _kline(109, 108, 105, 106),  # lower high (drop ~1.8%)
            _kline(106, 107, 102, 103),
            _kline(103, 104, 100, 101),
            _kline(101, 102, 99, 100),
            _kline(100, 101, 98, 99),
            _kline(99, 100, 97, 98),
            _kline(98, 99, 96, 97),
        ]
        # 4H：bearish + sell dominant
        klines_4h._cache["BUMPUSDT"] = [
            _kline(100, 110, 99, 108),
            _kline(108, 110, 100, 102, qv=10000, taker_buy_qv=4000),
        ]
        try:
            result = self.bot.detect_entry("BUMPUSDT", candidate)
        finally:
            klines_1h._cache.pop("BUMPUSDT", None)
            klines_4h._cache.pop("BUMPUSDT", None)
        self.assertIsNotNone(result)
        self.assertEqual(result["gain_24h_pct"], 25.0)
        self.assertEqual(result["peak_price"], 110)
        self.assertGreater(result["lower_high_drop_pct"], 0)


if __name__ == "__main__":
    unittest.main()
