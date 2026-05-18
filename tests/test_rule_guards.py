"""测试短期风控闸门：#2 LONG连亏、#3 快速拦截、market_breadth_guard。"""
import time
import unittest
from unittest.mock import MagicMock


class TestRapidBlock(unittest.TestCase):
    """#3 同币当天 2 笔 SL 后 ban"""

    def setUp(self):
        from rule_selector import _RAPID_BLOCKS, _LONG_PAUSE
        _RAPID_BLOCKS.clear()
        _LONG_PAUSE.clear()

    def test_sl_exact_match(self):
        from rule_selector import record_rapid_block, is_rapid_blocked
        # SL_软保本 不应计入
        record_rapid_block("BTCUSDT", "SL_软保本", -5.0)
        record_rapid_block("BTCUSDT", "SL_保本", -3.0)
        r = is_rapid_blocked("BTCUSDT")
        self.assertFalse(r["blocked"], "SL_软保本/SL_保本不应计入")

        # 真实 SL 才会计入
        record_rapid_block("BTCUSDT", "SL", -10.0)
        record_rapid_block("BTCUSDT", "SL", -8.0)
        r = is_rapid_blocked("BTCUSDT")
        self.assertTrue(r["blocked"], "2 笔真实 SL 应触发 ban")
        self.assertIn("SL", r["reason"])

    def test_cross_day_reset(self):
        from rule_selector import record_rapid_block, is_rapid_blocked, _RAPID_BLOCKS
        # 模拟 2 笔 SL → ban
        record_rapid_block("ETHUSDT", "SL", -5.0)
        record_rapid_block("ETHUSDT", "SL", -5.0)
        self.assertTrue(is_rapid_blocked("ETHUSDT")["blocked"])
        # 模拟跨天
        _RAPID_BLOCKS["ETHUSDT"]["date"] = "2000-01-01"
        record_rapid_block("ETHUSDT", "TP1", 10.0)  # 触发跨天重置
        self.assertFalse(is_rapid_blocked("ETHUSDT")["blocked"], "跨天后 ban 应解除")
        self.assertEqual(_RAPID_BLOCKS["ETHUSDT"]["blocked_until"], 0.0, "blocked_until 应清零")
        # 新一天 2 笔 SL 应再次触发 ban
        record_rapid_block("ETHUSDT", "SL", -5.0)
        record_rapid_block("ETHUSDT", "SL", -5.0)
        self.assertTrue(is_rapid_blocked("ETHUSDT")["blocked"], "新一天 2 SL 应重新 ban")

    def test_non_sl_not_counted(self):
        from rule_selector import record_rapid_block, is_rapid_blocked
        record_rapid_block("SOLUSDT", "TP1", 5.0)
        record_rapid_block("SOLUSDT", "TP2", 8.0)
        record_rapid_block("SOLUSDT", "时间止损", -2.0)
        self.assertFalse(is_rapid_blocked("SOLUSDT")["blocked"], "非 SL 不计入")


class TestLongPause(unittest.TestCase):
    """#2 LONG 连亏暂停"""

    def setUp(self):
        from rule_selector import _RAPID_BLOCKS, _LONG_PAUSE
        _RAPID_BLOCKS.clear()
        _LONG_PAUSE.clear()

    def test_long_loss_pause(self):
        from rule_selector import record_direction_result, is_long_paused
        record_direction_result("BTCUSDT", "LONG", -10.0)
        self.assertFalse(is_long_paused("BTCUSDT")["paused"], "1 笔亏损不暂停")
        record_direction_result("BTCUSDT", "LONG", -5.0)
        self.assertTrue(is_long_paused("BTCUSDT")["paused"], "2 笔亏损暂停")

    def test_short_unaffected(self):
        from rule_selector import record_direction_result, is_long_paused
        record_direction_result("BTCUSDT", "LONG", -10.0)
        record_direction_result("BTCUSDT", "LONG", -5.0)
        self.assertTrue(is_long_paused("BTCUSDT")["paused"], "LONG 暂停")
        # SHORT 不受影响
        from rule_selector import should_trade
        r = should_trade("BTCUSDT", "T", "t", "s", direction="SHORT")
        self.assertTrue(r["allow"], "SHORT 不受影响")

    def test_long_win_resets(self):
        from rule_selector import record_direction_result, is_long_paused
        record_direction_result("BTCUSDT", "LONG", -10.0)
        record_direction_result("BTCUSDT", "LONG", -5.0)
        self.assertTrue(is_long_paused("BTCUSDT")["paused"])
        record_direction_result("BTCUSDT", "LONG", 10.0)
        self.assertFalse(is_long_paused("BTCUSDT")["paused"], "LONG 盈利解除暂停")


class TestMarketBreadthGuard(unittest.TestCase):
    """#1/#5 市场宽度守卫"""

    def setUp(self):
        from market_breadth_guard import _cache
        _cache.clear()

    def test_no_bot_returns_safe_default(self):
        from market_breadth_guard import should_degrade_long
        r = should_degrade_long(None)
        self.assertFalse(r["degrade"], "bot=None 应返回 False")
        self.assertEqual(r["reason"], "")

    def test_cache_not_poisoned_by_none(self):
        """验证 bot=None 的结果不缓存，不影响后续真实调用。"""
        from market_breadth_guard import should_degrade_long, _cache
        _cache.clear()
        r1 = should_degrade_long(None)
        self.assertFalse(r1["degrade"])
        # None 的结果不应被缓存
        self.assertNotEqual(_cache.get("cached_at", 0), r1)

    def test_btc_drop_triggers_degrade(self):
        from market_breadth_guard import should_degrade_long, _cache
        _cache.clear()
        bot = MagicMock()
        # BTC 5m 跌 2%
        bot.kline_buffer = {
            "BTCUSDT": [
                {"c": 50000}, {"c": 49800}, {"c": 49600},
                {"c": 49400}, {"c": 49000},
            ]
        }
        bot.candidate_symbols = []
        r = should_degrade_long(bot)
        self.assertTrue(r["degrade"], "BTC 5m 跌 2% 应触发降级")
        self.assertIn("BTC", r["reason"])

    def test_decline_ratio_triggers_degrade(self):
        from market_breadth_guard import should_degrade_long, _cache
        _cache.clear()
        bot = MagicMock()
        bot.kline_buffer = {"BTCUSDT": [{"c": 50000}] * 10}
        # 15 个币，10 个下跌
        bot.candidate_symbols = [f"COIN{i}USDT" for i in range(15)]
        for i in range(15):
            buf = [{"c": 100}] * 5
            if i < 10:  # 前 10 个下跌
                buf[-1] = {"c": 90}
            bot.kline_buffer[f"COIN{i}USDT"] = buf
        r = should_degrade_long(bot)
        self.assertTrue(r["degrade"], "下跌占比 > 60% 应触发降级")
        self.assertIn("下跌", r["reason"])


if __name__ == "__main__":
    unittest.main()
