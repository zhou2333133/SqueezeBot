import unittest

from bot_scalp import BinanceScalpBot, ScalpPosition
from config import config_manager
from scanner.candidates import Candidate, clear_candidates, upsert_candidate


class TestScalpEngine(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_settings = config_manager.settings.copy()
        clear_candidates()
        config_manager.settings.update({
            "FEE_RATE_PER_SIDE": 0.0004,
            "SLIPPAGE_RATE_PER_SIDE": 0.0005,
            "BREAKOUT_MIN_PCT": 0.15,
            "BREAKOUT_ATR_MULT": 0.7,
            "BREAKOUT_ATR_MIN_PCT": 0.50,
            "BREAKOUT_ATR_MAX_PCT": 1.20,
            "BREAKOUT_MAX_PREMOVE_30M_PCT": 3.0,
            "SCALP_NET_BREAKEVEN_LOCK_PCT": 0.15,
            "SCALP_TP1_SOFT_BREAKEVEN_PCT": 0.30,
            "SCALP_TP3_AGGRESSIVE_RUNNER": True,
            "SCALP_USE_YAOBI_CONTEXT": True,
            "SCALP_YAOBI_CONTEXT_TOP_N": 30,
            "SCALP_YAOBI_MIN_SCORE": 30,
            "SCALP_YAOBI_MIN_ANOMALY_SCORE": 35,
            "SCALP_YAOBI_BLOCK_DECISION_BAN": True,
            "SCALP_YAOBI_BLOCK_WAIT_CONFIRM": True,
            "SCALP_YAOBI_BLOCK_HIGH_RISK": True,
            "SCALP_YAOBI_FUNDING_OI_GUARD": True,
            "SCALP_YAOBI_FUNDING_EXTREME_PCT": 0.05,
            "SCALP_YAOBI_OI_GUARD_MIN_24H_PCT": 50.0,
        })

    def tearDown(self) -> None:
        config_manager.settings.clear()
        config_manager.settings.update(self._orig_settings)
        clear_candidates()

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

    def test_breakeven_price_includes_roundtrip_cost_and_profit_lock(self) -> None:
        bot = BinanceScalpBot()
        pos = ScalpPosition(
            symbol="BTCUSDT",
            direction="LONG",
            entry_price=100.0,
            quantity=1.0,
            quantity_remaining=1.0,
            sl_price=95.0,
            tp1_price=105.0,
            tp2_price=110.0,
        )

        self.assertAlmostEqual(bot._breakeven_price(pos), 100.33, places=6)
        pos.direction = "SHORT"
        self.assertAlmostEqual(bot._breakeven_price(pos), 99.67, places=6)

    def test_tp1_soft_breakeven_gives_breathing_room_without_loosening_stop(self) -> None:
        bot = BinanceScalpBot()
        bot.kline_buffer["RUNUSDT"] = [
            {"o": 100.0, "h": 100.8, "l": 99.85, "c": 100.2, "q": 1000.0, "Q": 600.0}
            for _ in range(5)
        ]
        pos = ScalpPosition(
            symbol="RUNUSDT",
            direction="LONG",
            entry_price=100.0,
            quantity=1.0,
            quantity_remaining=1.0,
            sl_price=95.0,
            tp1_price=101.2,
            tp2_price=103.0,
        )

        self.assertAlmostEqual(bot._tp1_soft_breakeven_price(pos), 99.6503, places=4)

        pos.sl_price = 99.9
        self.assertAlmostEqual(bot._tp1_soft_breakeven_price(pos), 99.9, places=6)

        pos.direction = "SHORT"
        pos.sl_price = 105.0
        self.assertAlmostEqual(bot._tp1_soft_breakeven_price(pos), 101.0016, places=4)

    def test_breakout_premove_filter_blocks_late_chase(self) -> None:
        bot = BinanceScalpBot()
        bot.kline_buffer["BASUSDT"] = [
            {"o": 100.0 + i * 0.1, "h": 100.5 + i * 0.1, "l": 99.5 + i * 0.1, "c": 100.0 + i * 0.1, "q": 1000.0, "Q": 600.0}
            for i in range(30)
        ]

        ok, reason = bot._breakout_premove_allowed("BASUSDT", "LONG", 104.0)

        self.assertFalse(ok)
        self.assertIn("30m同向已走", reason)

    def test_yaobi_wait_confirm_and_crowded_funding_block_entries(self) -> None:
        bot = BinanceScalpBot()
        bot.candidate_meta["METUSDT"] = {
            "yaobi_context": True,
            "yaobi_decision_action": "等待确认",
            "yaobi_decision_note": "OI强但仍需确认",
        }

        ok, reason = bot._yaobi_entry_guard("METUSDT", "LONG")

        self.assertFalse(ok)
        self.assertIn("等待确认", reason)

        bot.candidate_meta["METUSDT"].update({
            "yaobi_decision_action": "观察",
            "yaobi_oi_trend_grade": "A",
            "yaobi_oi_change_24h_pct": 82.9,
            "yaobi_funding_rate_pct": -0.1851,
        })

        ok, reason = bot._yaobi_entry_guard("METUSDT", "SHORT")

        self.assertFalse(ok)
        self.assertIn("禁止追空", reason)

    def test_tp3_aggressive_runner_uses_looser_trailing_candidate(self) -> None:
        bot = BinanceScalpBot()
        bot.kline_buffer["RUNUSDT"] = [
            {"o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "q": 1000.0, "Q": 600.0}
            for i in range(25)
        ]
        pos = ScalpPosition(
            symbol="RUNUSDT",
            direction="LONG",
            entry_price=100.0,
            quantity=1.0,
            quantity_remaining=0.6,
            sl_price=100.3,
            tp1_price=102.0,
            tp2_price=104.0,
            tp2_hit=True,
            trail_ref_price=120.0,
            trail_pct=8.0,
            structure_trail_bars=14,
        )

        bot._apply_tp3_trailing_stop(pos, 124.0)

        self.assertAlmostEqual(pos.trail_ref_price, 124.0)
        self.assertAlmostEqual(pos.sl_price, 110.0, places=6)

        config_manager.settings["SCALP_TP3_AGGRESSIVE_RUNNER"] = False
        pos.sl_price = 100.3
        pos.trail_ref_price = 120.0

        bot._apply_tp3_trailing_stop(pos, 124.0)

        self.assertGreater(pos.sl_price, 114.0)

    def test_yaobi_futures_context_is_shared_to_scalp_candidates(self) -> None:
        upsert_candidate(Candidate(
            symbol="BAS",
            has_futures=True,
            score=82,
            anomaly_score=44,
            sources=["binance_futures", "okx_hot"],
            decision_action="允许交易",
            decision_confidence=70,
            oi_trend_grade="A",
            okx_buy_ratio=0.68,
            address="0xabc",
            chain="base",
        ))
        bot = BinanceScalpBot()
        bot.monitored_symbols = ["BASUSDT"]

        candidates: dict[str, dict] = {}
        stats = bot._merge_yaobi_context(candidates)

        self.assertEqual(stats["added"], 1)
        self.assertIn("BASUSDT", candidates)
        self.assertTrue(candidates["BASUSDT"]["yaobi_context"])
        self.assertEqual(candidates["BASUSDT"]["yaobi_score"], 82)
        self.assertEqual(candidates["BASUSDT"]["yaobi_address"], "0xabc")

    def test_entry_context_includes_candidate_path_and_yaobi_context(self) -> None:
        bot = BinanceScalpBot()
        bot.candidate_meta["BASUSDT"] = {
            "rank": 3,
            "direction_bias": "ANY",
            "change_24h": 20.0,
            "volume_24h": 2_000_000,
            "candidate_sources": ["binance_24h", "yaobi_shared"],
            "yaobi_context": True,
            "yaobi_score": 75,
            "yaobi_decision_action": "允许交易",
            "yaobi_oi_trend_grade": "A",
            "scalp_candidate_seen_price": 1.0,
            "scalp_candidate_seen_ts": 1.0,
            "scalp_candidate_seen_time": "2026-04-22 10:00:00",
            "scalp_candidate_max_up_pct": 0.0,
            "scalp_candidate_max_down_pct": 0.0,
        }
        bot._update_candidate_path("BASUSDT", 1.12)
        bot.kline_buffer["BASUSDT"] = [
            {"o": 1.0, "h": 1.1, "l": 0.99, "c": 1.0 + i * 0.001, "q": 1000.0, "Q": 600.0}
            for i in range(60)
        ]
        bot._live_candle["BASUSDT"] = {
            "h": 1.13,
            "l": 1.10,
            "taker_buy": 700.0,
            "total_vol": 1000.0,
            "close": 1.12,
            "open": 1.10,
        }

        ctx = bot._entry_context_snapshot("BASUSDT", "LONG", 0.3, "动能突破多", "TREND_EARLY")

        self.assertTrue(ctx["yaobi_context"])
        self.assertEqual(ctx["yaobi_score"], 75)
        self.assertEqual(ctx["candidate_sources"], ["binance_24h", "yaobi_shared"])
        self.assertAlmostEqual(ctx["scalp_candidate_max_up_pct"], 12.0, places=3)
        self.assertAlmostEqual(ctx["pre_entry_favorable_from_candidate_pct"], 12.0, places=3)


if __name__ == "__main__":
    unittest.main()
