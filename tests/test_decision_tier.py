"""三层决策面板（v2.8）分类逻辑 + 推送格式化的单测。"""
import asyncio
import unittest
from unittest.mock import patch

from scanner.candidates import Candidate
from scanner.scorer import classify_decision_tier, score
from scanner.notifier import (
    format_telegram_text,
    summarize_scan,
    push_yaobi_scan_summary,
)
from config import config_manager


def _new(**kw):
    """简化 Candidate 构造。"""
    return Candidate(symbol=kw.pop("symbol", "XYZ"), **kw)


class TestDecisionTier(unittest.TestCase):
    """tier 分类规则。"""

    def test_l1_main_oi_explosion(self):
        c = _new(score=85, oi_trend_grade="A", oi_change_24h_pct=150, volume_ratio=15)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "L1_MAIN")
        self.assertEqual(c.decision_subtype, "OI爆发")

    def test_l1_main_acceleration(self):
        c = _new(score=80, oi_trend_grade="S", oi_acceleration=40, oi_change_24h_pct=50, volume_ratio=8)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "L1_MAIN")
        self.assertEqual(c.decision_subtype, "加速中")

    def test_l1_main_yaobi_qidong(self):
        # 7d 死平 + 突然放量
        c = _new(score=75, oi_trend_grade="A", oi_flat_days=10, volume_ratio=6, oi_change_24h_pct=20)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "L1_MAIN")
        self.assertEqual(c.decision_subtype, "妖币启动")

    def test_l2_ambush_silent_accumulation(self):
        c = _new(score=55, oi_trend_grade="B", oi_flat_days=8, volume_ratio=2.5)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "L2_AMBUSH")
        self.assertEqual(c.decision_subtype, "静默建仓")

    def test_l2_ambush_breakout_eve(self):
        c = _new(score=60, oi_trend_grade="B", oi_change_24h_pct=40, price_change_24h=10)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "L2_AMBUSH")
        self.assertEqual(c.decision_subtype, "突破前夜")

    def test_l2_ambush_early_start(self):
        c = _new(score=55, oi_trend_grade="A", oi_acceleration=20, price_change_1h=3, volume_ratio=1)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "L2_AMBUSH")
        self.assertEqual(c.decision_subtype, "早期启动")

    def test_risk_distributing(self):
        # OI 跌 + 价格涨 = 出货
        c = _new(score=70, oi_trend_grade="A", oi_change_24h_pct=-20, price_change_24h=15)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "RISK_AVOID")
        self.assertEqual(c.decision_subtype, "出货家族")

    def test_risk_fr_warning_positive(self):
        c = _new(score=70, oi_trend_grade="A", funding_rate_pct=0.20)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "RISK_AVOID")
        self.assertEqual(c.decision_subtype, "FR警告")

    def test_risk_surf_hard_block_overrides_l1(self):
        c = _new(score=85, oi_trend_grade="S", oi_change_24h_pct=100, volume_ratio=15,
                 surf_ai_hard_block=True)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "RISK_AVOID")
        self.assertEqual(c.decision_subtype, "AI高风险")

    def test_risk_already_dropped(self):
        c = _new(score=50, price_change_24h=-30)
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "RISK_AVOID")
        self.assertEqual(c.decision_subtype, "已破位")

    def test_neutral_low_score(self):
        c = _new(score=30, oi_trend_grade="C")
        classify_decision_tier(c)
        self.assertEqual(c.decision_tier, "")
        self.assertEqual(c.decision_subtype, "")

    def test_score_full_pipeline_includes_tier(self):
        # full score() pipeline should also populate tier
        c = _new(price_change_24h=80, volume_24h=200_000_000, oi_trend_grade="S",
                 oi_change_24h_pct=200, volume_ratio=20, has_futures=True)
        result = score(c)
        self.assertEqual(result, c)
        self.assertGreater(c.score, 70)
        self.assertGreater(c.score_raw, 0)
        self.assertIn(c.decision_tier, {"L1_MAIN", "RISK_AVOID"})  # 涨幅 80 + 极端值可能进风险


class TestNotifierFormatting(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = config_manager.settings.copy()
        config_manager.settings.update({
            "YAOBI_NOTIFIER_ENABLED": True,
            "YAOBI_NOTIFIER_MIN_TIER": "L2_AMBUSH",
            "YAOBI_NOTIFIER_MAX_PER_BATCH": 8,
        })

    def tearDown(self) -> None:
        config_manager.settings.clear()
        config_manager.settings.update(self._orig)

    def test_summarize_scan_returns_none_when_no_actionable(self):
        candidates = [
            {"symbol": "X", "decision_tier": "", "score": 50},
            {"symbol": "Y", "decision_tier": "RISK_AVOID", "score": 60},  # min_tier=L2_AMBUSH skips RISK
        ]
        self.assertIsNone(summarize_scan(candidates))

    def test_summarize_scan_groups_correctly(self):
        candidates = [
            {"symbol": "A", "decision_tier": "L1_MAIN", "decision_subtype": "OI爆发",
             "score": 90, "score_raw": 110, "price_change_24h": 50, "oi_change_24h_pct": 150},
            {"symbol": "B", "decision_tier": "L2_AMBUSH", "decision_subtype": "突破前夜",
             "score": 60, "price_change_24h": 12},
            {"symbol": "C", "decision_tier": "RISK_AVOID", "decision_subtype": "出货家族",
             "score": 40},
            {"symbol": "D", "decision_tier": "", "score": 30},
        ]
        s = summarize_scan(candidates)
        self.assertIsNotNone(s)
        self.assertEqual(len(s["l1"]), 1)
        self.assertEqual(s["l1"][0]["symbol"], "A")
        self.assertEqual(len(s["l2"]), 1)
        self.assertEqual(s["l2"][0]["symbol"], "B")
        self.assertEqual(len(s["risk"]), 0)  # min_tier=L2 不包括 risk
        self.assertIn("L1×1 / L2×1", s["header"])

    def test_summarize_includes_risk_when_min_tier_all(self):
        config_manager.settings["YAOBI_NOTIFIER_MIN_TIER"] = "ALL"
        candidates = [
            {"symbol": "C", "decision_tier": "RISK_AVOID", "decision_subtype": "FR警告", "score": 40},
        ]
        s = summarize_scan(candidates)
        self.assertIsNotNone(s)
        self.assertEqual(len(s["risk"]), 1)

    def test_format_telegram_renders_emoji_and_score(self):
        candidates = [
            {"symbol": "ORCA", "decision_tier": "L1_MAIN", "decision_subtype": "OI爆发",
             "score": 90, "score_raw": 110, "price_change_24h": 75, "oi_change_24h_pct": 150},
        ]
        s = summarize_scan(candidates)
        text = format_telegram_text(s)
        self.assertIn("🌋", text)         # OI爆发 emoji
        self.assertIn("ORCA", text)
        self.assertIn("90/100", text)
        self.assertIn("raw:110", text)
        self.assertIn("L1 主战场", text)

    def test_push_skipped_when_disabled(self):
        config_manager.settings["YAOBI_NOTIFIER_ENABLED"] = False
        result = asyncio.run(push_yaobi_scan_summary([{"symbol": "A", "decision_tier": "L1_MAIN", "score": 80}]))
        self.assertEqual(result, {"skipped": "notifier_disabled"})


if __name__ == "__main__":
    unittest.main()
