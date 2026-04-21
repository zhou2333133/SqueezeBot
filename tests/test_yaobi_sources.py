import unittest

from scanner.sources import binance_square, okx_market


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


if __name__ == "__main__":
    unittest.main()
