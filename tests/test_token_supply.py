"""
test_token_supply.py — V4AF 启发式 vesting 分组单测
"""
import unittest

from scanner.sources.token_supply import classify_vesting


class TestClassifyVesting(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "FLASH_VOLUME_24H_MIN_USD": 1_000_000.0,
            "FLASH_FDV_RATIO_MAX": 0.85,
            "FLASH_BAN_NEAR_FULL_CIRC_MEME": True,
        }

    def test_low_volume_banned(self) -> None:
        phase, eligible, reason = classify_vesting(
            listing_age_days=30, market_cap=10_000_000, fdv=50_000_000,
            volume_24h=500_000, circulation_pct=0.2,
            cfg=self.cfg,
        )
        self.assertFalse(eligible)
        self.assertEqual(phase, "low_volume")
        self.assertIn("volume_24h", reason)

    def test_near_full_meme_banned(self) -> None:
        phase, eligible, reason = classify_vesting(
            listing_age_days=10, market_cap=90, fdv=100,
            volume_24h=5_000_000, circulation_pct=0.95,
            cfg=self.cfg,
        )
        self.assertFalse(eligible)
        self.assertEqual(phase, "near_full_meme")

    def test_pre_unlock_eligible(self) -> None:
        phase, eligible, _ = classify_vesting(
            listing_age_days=20, market_cap=10_000_000, fdv=100_000_000,
            volume_24h=5_000_000, circulation_pct=0.10,
            cfg=self.cfg,
        )
        self.assertTrue(eligible)
        self.assertEqual(phase, "pre_unlock")

    def test_unlock_active_eligible(self) -> None:
        phase, eligible, _ = classify_vesting(
            listing_age_days=180, market_cap=20_000_000, fdv=100_000_000,
            volume_24h=8_000_000, circulation_pct=0.40,
            cfg=self.cfg,
        )
        self.assertTrue(eligible)
        self.assertEqual(phase, "unlock_active")

    def test_unlock_late_old_dumpheap(self) -> None:
        phase, eligible, _ = classify_vesting(
            listing_age_days=500, market_cap=2_000_000, fdv=10_000_000,
            volume_24h=2_500_000, circulation_pct=0.95,
            price_drawdown_from_ath_pct=85.0,
            cfg=self.cfg,
        )
        self.assertTrue(eligible)
        self.assertEqual(phase, "unlock_late")

    def test_no_listing_date_unknown(self) -> None:
        phase, eligible, reason = classify_vesting(
            listing_age_days=0, market_cap=10_000_000, fdv=50_000_000,
            volume_24h=5_000_000, circulation_pct=0.2,
            cfg=self.cfg,
        )
        self.assertFalse(eligible)
        self.assertEqual(phase, "unknown")
        self.assertEqual(reason, "no_listing_date")


if __name__ == "__main__":
    unittest.main()
