import asyncio
import unittest

from bot_scalp import BinanceScalpBot
from config import config_manager
from scanner.candidates import Candidate, clear_candidates, get_opportunity_queue
from scanner.intelligence.case_similarity import top_case_similarities
from scanner.intelligence.regime_classifier import apply_market_intelligence
from scanner.yaobi_scanner import YaobiScanner


class TestMarketIntelligenceRegime(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = config_manager.settings.copy()
        clear_candidates()
        config_manager.settings.update({
            "YAOBI_AI_ENABLED": False,
            "YAOBI_AI_REQUIRED_FOR_PERMISSION": False,
            "YAOBI_OPPORTUNITY_MIN_SCORE": 45,
            "YAOBI_OPPORTUNITY_TOP_N": 6,
        })

    def tearDown(self) -> None:
        config_manager.settings.clear()
        config_manager.settings.update(self._orig)
        clear_candidates()

    def test_accumulation_before_oi_becomes_ambush_watch_only(self) -> None:
        c = Candidate(
            symbol="AMB",
            has_futures=True,
            score=48,
            anomaly_score=42,
            okx_top10_hold_pct=66,
            okx_smart_money_holders=2,
            okx_large_trade_pct=0.24,
            okx_buy_ratio=0.72,
            square_mentions=6,
            oi_change_24h_pct=8,
            oi_change_15m_pct=0.4,
            funding_rate_pct=0.01,
        )

        apply_market_intelligence(c)
        scanner = YaobiScanner()
        asyncio.run(scanner._apply_opportunity_queue([c]))
        rows = get_opportunity_queue()

        self.assertEqual(c.market_stage, "accumulation_before_oi")
        self.assertEqual(c.trade_permission, "AMBUSH_WATCH")
        self.assertEqual(rows[0]["symbol"], "AMB")
        self.assertEqual(rows[0]["opportunity_action"], "OBSERVE")
        self.assertEqual(rows[0]["opportunity_permission"], "OBSERVE")
        self.assertIn("埋伏观察", " ".join(rows[0]["opportunity_required_confirmation"]))

    def test_ai_cannot_turn_ambush_watch_into_auto_execution_permission(self) -> None:
        config_manager.settings["YAOBI_AI_ENABLED"] = True
        config_manager.settings["YAOBI_AI_REQUIRED_FOR_PERMISSION"] = True
        c = Candidate(
            symbol="AMB",
            has_futures=True,
            score=60,
            anomaly_score=50,
            contract_activity_score=72,
            okx_top10_hold_pct=66,
            okx_smart_money_holders=2,
            okx_large_trade_pct=0.24,
            okx_buy_ratio=0.72,
            square_mentions=6,
            oi_change_24h_pct=8,
            oi_change_15m_pct=0.4,
            funding_rate_pct=0.01,
            taker_buy_ratio_5m=0.64,
            price_change_24h=12,
        )
        apply_market_intelligence(c)
        scanner = YaobiScanner()

        async def fake_ai(_session, _cands):
            return {
                "items": [{
                    "symbol": "AMB",
                    "market_stage": "accumulation_before_oi",
                    "playbook_type": "ambush_watch",
                    "trade_permission": "AMBUSH_WATCH",
                    "action": "WATCH_LONG_CONTINUATION",
                    "permission": "ALLOW_IF_1M_SIGNAL",
                    "confidence": 91,
                    "reasons": ["AI attempted to open a watch action"],
                    "risks": [],
                    "required_confirmation": ["wait local 1m"],
                }],
                "status": {"last_reason": "ok", "last_provider": "test"},
            }

        from unittest.mock import patch
        with patch("scanner.yaobi_scanner.analyze_opportunities", fake_ai):
            asyncio.run(scanner._apply_opportunity_queue([c]))

        rows = get_opportunity_queue()

        self.assertEqual(rows[0]["trade_permission"], "AMBUSH_WATCH")
        self.assertEqual(rows[0]["opportunity_action"], "OBSERVE")
        self.assertEqual(rows[0]["opportunity_permission"], "OBSERVE")
        self.assertEqual(rows[0]["opportunity_setup_state"], "WAIT")
        self.assertIn("AMBUSH_WATCH只允许观察", " ".join(rows[0]["opportunity_required_confirmation"]))

    def test_oi_explosion_with_hot_funding_and_weak_spot_is_bull_trap(self) -> None:
        c = Candidate(
            symbol="TRAP",
            has_futures=True,
            score=88,
            anomaly_score=80,
            contract_activity_score=75,
            price_change_24h=6,
            oi_change_24h_pct=155,
            oi_change_15m_pct=7,
            funding_rate_pct=0.12,
            taker_buy_ratio_5m=0.49,
            long_account_pct=71,
            top_trader_long_pct=68,
            okx_buy_ratio=0.34,
        )

        apply_market_intelligence(c)

        self.assertEqual(c.market_stage, "bull_trap")
        self.assertEqual(c.trade_permission, "BLOCK")
        self.assertGreaterEqual(c.risk_score, 55)

    def test_distribution_blocks_price_up_oi_down_sell_pressure(self) -> None:
        c = Candidate(
            symbol="DIST",
            has_futures=True,
            score=70,
            price_change_24h=22,
            oi_change_24h_pct=-22,
            okx_top10_hold_pct=72,
            okx_buy_ratio=0.26,
            okx_large_trade_pct=0.22,
        )

        apply_market_intelligence(c)

        self.assertEqual(c.market_stage, "distribution")
        self.assertEqual(c.trade_permission, "BLOCK")

    def test_real_breakout_uses_oi_as_confirmation(self) -> None:
        c = Candidate(
            symbol="RUN",
            has_futures=True,
            score=74,
            anomaly_score=66,
            price_change_24h=18,
            oi_change_24h_pct=72,
            oi_change_15m_pct=4.5,
            funding_rate_pct=0.01,
            taker_buy_ratio_5m=0.63,
            okx_buy_ratio=0.68,
        )

        apply_market_intelligence(c)

        self.assertEqual(c.market_stage, "real_breakout")
        self.assertEqual(c.trade_permission, "WATCH_CONFIRMATION")
        self.assertIn("OI作为确认信号", " ".join(c.intelligence_reasons))

    def test_mm_control_and_dead_regimes_are_separate(self) -> None:
        mm = Candidate(
            symbol="MM",
            has_futures=True,
            okx_top10_hold_pct=86,
            okx_large_trade_pct=0.32,
            liquidity=35_000,
            square_mentions=28,
            okx_buy_ratio=0.55,
        )
        dead = Candidate(
            symbol="DEAD",
            has_futures=True,
            price_change_24h=-42,
            liquidity=20_000,
            okx_risk_level=4,
            okx_token_tags=["honeypot"],
        )

        apply_market_intelligence(mm)
        apply_market_intelligence(dead)

        self.assertEqual(mm.market_stage, "mm_control")
        self.assertEqual(mm.trade_permission, "OBSERVE")
        self.assertEqual(dead.market_stage, "dead")
        self.assertEqual(dead.trade_permission, "BLOCK")

    def test_case_similarity_returns_behavior_match(self) -> None:
        c = Candidate(
            symbol="SIM",
            okx_top10_hold_pct=64,
            okx_smart_money_holders=3,
            okx_buy_ratio=0.76,
            okx_large_trade_pct=0.30,
            oi_change_24h_pct=6,
            square_mentions=8,
        )

        matches = top_case_similarities(c, limit=1)

        self.assertEqual(matches[0]["symbol"], "ACCUM_PROTO")
        self.assertGreaterEqual(matches[0]["similarity"], 70)

    def test_scalp_guard_blocks_market_structure_block(self) -> None:
        bot = BinanceScalpBot()
        bot.candidate_meta["TRAPUSDT"] = {
            "yaobi_context": True,
            "yaobi_decision_action": "观察",
            "yaobi_trade_permission": "BLOCK",
            "yaobi_market_stage": "bull_trap",
            "yaobi_opportunity_action": "OBSERVE",
            "yaobi_opportunity_permission": "OBSERVE",
        }

        ok, reason, _ = bot._yaobi_entry_guard("TRAPUSDT", "LONG", "动能突破多")

        self.assertFalse(ok)
        self.assertIn("市场结构剧本BLOCK", reason)


if __name__ == "__main__":
    unittest.main()
