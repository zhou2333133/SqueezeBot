import unittest

from scanner.candidates import Candidate, clear_candidates, get_anomaly_candidates, upsert_candidate
from scanner.sources import binance_square, okx_market, surf_api
from scanner.yaobi_scanner import YaobiScanner


class TestYaobiSources(unittest.TestCase):
    def test_okx_price_info_array_payload_parsing(self) -> None:
        data = {
            "code": "0",
            "data": [{
                "price": "0.00123",
                "marketCap": "1234567",
                "liquidity": "45678",
                "holders": "3210",
                "priceChange24H": "12.5",
                "volume24H": "99999",
                "txs5M": "17",
            }],
        }

        item = okx_market._first_data(data)

        self.assertEqual(item["price"], "0.00123")
        self.assertEqual(okx_market._float_any(item, "price"), 0.00123)
        self.assertEqual(okx_market._int_any(item, "holders"), 3210)
        self.assertEqual(okx_market._float_any(item, "priceChange24H"), 12.5)

    def test_okx_sol_address_preserves_case(self) -> None:
        sol_addr = "SoLCaseSensitive123abcXYZ"
        self.assertEqual(okx_market._normalize_address("501", sol_addr), sol_addr)
        self.assertEqual(okx_market._normalize_address("1", "0xABCDEF"), "0xabcdef")

    def test_okx_price_info_parser_returns_batch_key_fields(self) -> None:
        row = {
            "chainIndex": "1",
            "tokenContractAddress": "0xABCDEF",
            "price": "1.25",
            "top10HoldPercent": "66.6",
            "txs24H": "123",
        }
        parsed = okx_market._parse_price_info(row)
        self.assertEqual(parsed["chain_id"], "1")
        self.assertEqual(parsed["address"], "0xabcdef")
        self.assertEqual(parsed["price_usd"], 1.25)
        self.assertEqual(parsed["tx_count_24h"], 123)

    def test_binance_square_extracts_nested_posts_and_mentions(self) -> None:
        payload = {
            "code": "000000",
            "data": {
                "content": [{
                    "postId": "abc",
                    "content": {"bodyTextOnly": "Watching $PEPE and SOLUSDT after breakout"},
                    "stat": {"likeCount": "12", "viewCount": "500"},
                }]
            },
        }

        posts = binance_square._extract_posts(payload)
        mentions = binance_square.extract_ticker_mentions(posts)

        self.assertEqual(len(posts), 1)
        self.assertIn("PEPE", mentions)
        self.assertIn("SOL", mentions)
        self.assertEqual(mentions["PEPE"]["count"], 1)

    def test_anomaly_radar_combines_okx_futures_and_sentiment(self) -> None:
        c = Candidate(
            symbol="RAVE",
            has_futures=True,
            price_change_24h=-44.9,
            oi_change_24h_pct=16.4,
            square_mentions=10,
            funding_rate_pct=0.339,
            whale_long_ratio=0.07,
            retail_short_pct=80.0,
            okx_large_trade_pct=0.35,
            okx_buy_ratio=0.75,
            surf_news_titles=["KOL discusses RAVE", "RAVE funding turns unusual"],
        )

        YaobiScanner()._apply_anomaly_radar([c])

        self.assertGreaterEqual(c.anomaly_score, 50)
        self.assertIn("OKX大单", c.anomaly_tags)
        self.assertIn("散户极空", c.anomaly_tags)
        self.assertEqual(c.long_short_text, "多7%/空93%")
        self.assertGreaterEqual(c.sentiment_heat, 12)

    def test_anomaly_candidates_sorted_by_anomaly_score(self) -> None:
        clear_candidates()
        try:
            low = Candidate(symbol="LOW", anomaly_score=20, score=90)
            high = Candidate(symbol="HIGH", anomaly_score=75, score=10)
            upsert_candidate(low)
            upsert_candidate(high)

            rows = get_anomaly_candidates(min_anomaly=30, limit=10)

            self.assertEqual([r["symbol"] for r in rows], ["HIGH"])
        finally:
            clear_candidates()

    def test_surf_project_terms_and_news_match_symbol_alias(self) -> None:
        terms = surf_api.project_terms("BTCUSDT")
        self.assertIn("btc", terms)
        self.assertIn("bitcoin", terms)

        item = {
            "project_name": "Bitcoin",
            "text": "Bitcoin ETF flows remain strong",
        }
        self.assertTrue(surf_api.news_matches_symbol(item, "BTCUSDT"))

    def test_surf_normalizes_news_items(self) -> None:
        rows = surf_api.normalize_news_items([{
            "id": "n1",
            "title": "Ethereum upgrade ships",
            "summary": "Mainnet upgrade completed",
            "source": "COINDESK",
            "project_name": "Ethereum",
            "published_at": 1776810000,
        }])

        self.assertEqual(rows[0]["title"], "Ethereum upgrade ships")
        self.assertIn("Mainnet upgrade completed", rows[0]["text"])


if __name__ == "__main__":
    unittest.main()
