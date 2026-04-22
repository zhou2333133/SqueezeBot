import os
import tempfile
import unittest

import watchlist


class TestWatchlist(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_file = watchlist.WATCHLIST_FILE
        self._orig_cache = watchlist._cache
        self._orig_mtime = watchlist._cache_mtime
        watchlist.WATCHLIST_FILE = os.path.join(self._tmp.name, "watchlist.json")
        watchlist._cache = None
        watchlist._cache_mtime = None

    def tearDown(self) -> None:
        watchlist.WATCHLIST_FILE = self._orig_file
        watchlist._cache = self._orig_cache
        watchlist._cache_mtime = self._orig_mtime
        self._tmp.cleanup()

    def test_upsert_and_block_symbol_variants(self) -> None:
        item = watchlist.upsert_watch_item("bas", status="禁止交易", reason="test")

        self.assertEqual(item["symbol"], "BAS")
        self.assertTrue(watchlist.is_symbol_blocked("BASUSDT"))
        self.assertTrue(watchlist.is_symbol_blocked("BAS"))

    def test_list_items_enriches_from_candidates(self) -> None:
        watchlist.upsert_watch_item("HIGH", status="观察", reason="manual")

        rows = watchlist.list_watch_items([{
            "symbol": "HIGH",
            "score": 88,
            "anomaly_score": 66,
            "oi_trend_grade": "S",
            "decision_action": "等待确认",
        }])

        self.assertEqual(rows[0]["latest_score"], 88)
        self.assertEqual(rows[0]["latest_anomaly_score"], 66)
        self.assertEqual(rows[0]["latest_oi_grade"], "S")
        self.assertEqual(rows[0]["latest_decision_action"], "等待确认")


if __name__ == "__main__":
    unittest.main()
