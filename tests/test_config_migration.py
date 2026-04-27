import json
import os
import tempfile
import unittest

import config


class TestConfigMigration(unittest.TestCase):
    def test_old_settings_file_is_migrated_to_current_strategy_profile(self) -> None:
        old_config_file = config.CONFIG_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config.CONFIG_FILE = os.path.join(tmp, "settings.json")
                with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump({
                        "CONFIG_PROFILE_VERSION": 0,
                        "SCALP_AUTO_TRADE": True,
                        "SCALP_POSITION_USDT": 10.0,
                        "SCALP_CANDIDATE_LIMIT": 80,
                        "SCALP_TP3_TRAIL_PCT": 5.0,
                        "SCALP_STRUCTURE_TRAIL_BARS": 8,
                        "SIGNAL_COOLDOWN_SECONDS": 5,
                        "SCALP_MAX_DAILY_LOSS_USDT": 5000.0,
                        "BREAKOUT_MIN_PCT": 0.05,
                        "BREAKOUT_ATR_MULT": 0.5,
                        "BREAKOUT_MIN_VOL_RATIO": 0.20,
                        "SCALP_SURF_NEWS_ENABLED": True,
                        "SCALP_SURF_ENTRY_AI_ENABLED": True,
                        "YAOBI_SURF_NEWS_ENABLED": True,
                        "YAOBI_SURF_AI_ENABLED": True,
                        "YAOBI_SURF_TOP_N": 5,
                        "YAOBI_AI_ENABLED": True,
                        "YAOBI_OPPORTUNITY_TOP_N": 20,
                    }, f)

                manager = config.ConfigManager()

                self.assertEqual(manager.settings["CONFIG_PROFILE_VERSION"], config.ConfigManager.PROFILE_VERSION)
                self.assertEqual(manager.settings["SCALP_CANDIDATE_LIMIT"], 40)
                self.assertEqual(manager.settings["SCALP_CANDIDATE_SOURCE_MODE"], "YAOBI_ONLY")
                self.assertTrue(manager.settings["SCALP_REQUIRE_YAOBI_CONTEXT"])
                self.assertEqual(manager.settings["SCALP_TP1_RATIO"], 0.30)
                self.assertEqual(manager.settings["SCALP_TP2_RATIO"], 0.30)
                self.assertEqual(manager.settings["SCALP_TP3_TRAIL_PCT"], 8.0)
                self.assertEqual(manager.settings["SCALP_STRUCTURE_TRAIL_BARS"], 14)
                self.assertTrue(manager.settings["SCALP_TP3_AGGRESSIVE_RUNNER"])
                self.assertFalse(manager.settings["SCALP_SKIP_TP1_IN_STRONG_TREND"])
                self.assertEqual(manager.settings["SCALP_NET_BREAKEVEN_LOCK_PCT"], 0.15)
                self.assertEqual(manager.settings["SCALP_TP1_SOFT_BREAKEVEN_PCT"], 0.60)
                self.assertEqual(manager.settings["SCALP_REVERSAL_STOP_SL_FRACTION"], 0.40)
                self.assertEqual(manager.settings["SIGNAL_COOLDOWN_SECONDS"], 30)
                self.assertEqual(manager.settings["SCALP_MAX_DAILY_LOSS_USDT"], 200.0)
                self.assertEqual(manager.settings["SCALP_TP1_RR"], 1.5)
                self.assertEqual(manager.settings["SCALP_TP2_RR"], 3.0)
                self.assertEqual(manager.settings["SCALP_TIME_STOP_MINUTES"], 45)
                self.assertEqual(manager.settings["SCALP_YAOBI_MIN_ANOMALY_SCORE"], 50)
                self.assertEqual(manager.settings["SCALP_YAOBI_MIN_SCORE"], 60)
                self.assertEqual(manager.settings["BREAKOUT_MIN_PCT"], 0.15)
                self.assertEqual(manager.settings["BREAKOUT_ATR_MULT"], 0.7)
                self.assertEqual(manager.settings["BREAKOUT_MIN_VOL_RATIO"], 0.40)
                self.assertEqual(manager.settings["BREAKOUT_MAX_PREMOVE_5M_PCT"], 1.2)
                self.assertEqual(manager.settings["BREAKOUT_MAX_PREMOVE_15M_PCT"], 2.5)
                self.assertEqual(manager.settings["BREAKOUT_MAX_PREMOVE_30M_PCT"], 2.5)
                self.assertEqual(manager.settings["BREAKOUT_MAX_EMA20_DEVIATION_PCT"], 3.0)
                self.assertTrue(manager.settings["CONTINUATION_PULLBACK_ENABLED"])
                self.assertEqual(manager.settings["CONTINUATION_TAKER_MIN"], 0.58)
                self.assertEqual(manager.settings["CONTINUATION_HOT_TAKER_MIN"], 0.52)
                self.assertEqual(manager.settings["CONTINUATION_MIN_PULLBACK_PCT"], 0.20)
                self.assertEqual(manager.settings["CONTINUATION_RECLAIM_LOOKBACK"], 3)
                self.assertEqual(manager.settings["CONTINUATION_ATR_MAX_PCT"], 2.50)
                self.assertEqual(manager.settings["CONTINUATION_MAX_EMA20_DEVIATION_PCT"], 4.00)
                self.assertEqual(manager.settings["SCALP_OI_PREFETCH_TOP_N"], 30)
                self.assertTrue(manager.settings["SCALP_YAOBI_BLOCK_WAIT_CONFIRM"])
                self.assertTrue(manager.settings["SCALP_YAOBI_FUNDING_OI_GUARD"])
                self.assertTrue(manager.settings["SCALP_OPPORTUNITY_GUARD_ENABLED"])
                self.assertTrue(manager.settings["SCALP_REQUIRE_OPPORTUNITY_QUEUE"])
                self.assertTrue(manager.settings["SCALP_REQUIRE_OPPORTUNITY_PERMISSION"])
                self.assertEqual(manager.settings["SCALP_YAOBI_FUNDING_EXTREME_PCT"], 0.05)
                self.assertEqual(manager.settings["SCALP_YAOBI_OI_GUARD_MIN_24H_PCT"], 50.0)
                self.assertFalse(manager.settings["SCALP_SURF_NEWS_ENABLED"])
                self.assertFalse(manager.settings["SCALP_SURF_ENTRY_AI_ENABLED"])
                self.assertFalse(manager.settings["YAOBI_SURF_NEWS_ENABLED"])
                self.assertTrue(manager.settings["YAOBI_SURF_AI_ENABLED"])
                self.assertEqual(manager.settings["YAOBI_SURF_TOP_N"], 6)
                self.assertEqual(manager.settings["YAOBI_SURF_AI_MODEL"], "surf-ask")
                self.assertTrue(manager.settings["YAOBI_BINANCE_SHORT_INTEL_ENABLED"])
                self.assertTrue(manager.settings["YAOBI_BINANCE_LIQUIDATION_WS_ENABLED"])
                self.assertEqual(manager.settings["YAOBI_OPPORTUNITY_TOP_N"], 6)
                self.assertTrue(manager.settings["YAOBI_AI_ENABLED"])
                self.assertTrue(manager.settings["YAOBI_AI_REQUIRED_FOR_PERMISSION"])
                self.assertFalse(manager.settings["YAOBI_DUAL_AI_CONSENSUS_REQUIRED"])
                self.assertEqual(manager.settings["YAOBI_SURF_DIRECTION_MIN_CONFIDENCE"], 55)
                self.assertEqual(manager.settings["YAOBI_AI_PROVIDER_PRIORITY"], "gemini,openai,anthropic")
                self.assertEqual(manager.settings["YAOBI_AI_MODEL_GEMINI"], "gemini-2.5-flash")
                self.assertEqual(manager.settings["YAOBI_AI_MAX_SYMBOLS_PER_RUN"], 6)
                self.assertTrue(manager.settings["YAOBI_AI_FAILURE_FALLBACK_ENABLED"])
                self.assertEqual(manager.settings["YAOBI_AI_FAILURE_FALLBACK_MIN_SCORE"], 45)
                self.assertEqual(manager.settings["YAOBI_PLAYBOOK_TTL_MINUTES"], 45)
                self.assertEqual(manager.settings["YAOBI_AI_DAILY_USD_CAP"], 1.0)
                self.assertEqual(manager.settings["SCALP_POSITION_USDT"], 10.0)
                self.assertTrue(manager.settings["SCALP_AUTO_TRADE"])

                with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                    persisted = json.load(f)
                self.assertEqual(persisted["CONFIG_PROFILE_VERSION"], config.ConfigManager.PROFILE_VERSION)
                self.assertEqual(persisted["SIGNAL_COOLDOWN_SECONDS"], 30)
        finally:
            config.CONFIG_FILE = old_config_file


if __name__ == "__main__":
    unittest.main()
