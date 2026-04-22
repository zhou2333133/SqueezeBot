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
                    }, f)

                manager = config.ConfigManager()

                self.assertEqual(manager.settings["CONFIG_PROFILE_VERSION"], config.ConfigManager.PROFILE_VERSION)
                self.assertEqual(manager.settings["SCALP_CANDIDATE_LIMIT"], 50)
                self.assertEqual(manager.settings["SIGNAL_COOLDOWN_SECONDS"], 30)
                self.assertEqual(manager.settings["SCALP_MAX_DAILY_LOSS_USDT"], 200.0)
                self.assertEqual(manager.settings["BREAKOUT_MIN_PCT"], 0.10)
                self.assertEqual(manager.settings["BREAKOUT_ATR_MULT"], 0.7)
                self.assertEqual(manager.settings["BREAKOUT_MIN_VOL_RATIO"], 0.50)
                self.assertFalse(manager.settings["SCALP_SURF_NEWS_ENABLED"])
                self.assertFalse(manager.settings["SCALP_SURF_ENTRY_AI_ENABLED"])
                self.assertFalse(manager.settings["YAOBI_SURF_NEWS_ENABLED"])
                self.assertFalse(manager.settings["YAOBI_SURF_AI_ENABLED"])
                self.assertEqual(manager.settings["YAOBI_SURF_TOP_N"], 1)
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
