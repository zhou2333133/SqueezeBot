import asyncio
import unittest

from trader import BinanceTrader


class CaptureTrader(BinanceTrader):
    def __init__(self):
        super().__init__(session=None)
        self.last_request = None
        self._symbol_filters = {
            "TESTUSDT": {
                "status": "TRADING",
                "filters": {
                    "PRICE_FILTER": {"minPrice": "0.0001", "maxPrice": "1000", "tickSize": "0.0001"},
                    "LOT_SIZE": {"minQty": "1", "maxQty": "1000000", "stepSize": "1"},
                    "MARKET_LOT_SIZE": {"minQty": "1", "maxQty": "1000000", "stepSize": "1"},
                    "MIN_NOTIONAL": {"notional": "5.0"},
                },
            }
        }

    async def _request(self, method: str, endpoint: str, params: dict):
        self.last_request = (method, endpoint, dict(params))
        return {"orderId": 123, "executedQty": params.get("quantity", "0"), "status": "FILLED"}


class TestTraderLiveSafety(unittest.TestCase):
    def test_ioc_order_uses_exchange_tick_and_step_rules(self) -> None:
        trader = CaptureTrader()

        resp = asyncio.run(trader.place_limit_ioc_order("TESTUSDT", "BUY", 12.9, 1.234567))

        self.assertIsNotNone(resp)
        _, endpoint, params = trader.last_request
        self.assertEqual(endpoint, "/fapi/v1/order")
        self.assertEqual(params["quantity"], "12")
        self.assertEqual(params["price"], "1.2345")

    def test_ioc_order_rejects_below_min_notional_before_sending(self) -> None:
        trader = CaptureTrader()

        resp = asyncio.run(trader.place_limit_ioc_order("TESTUSDT", "BUY", 1.0, 1.0))

        self.assertIsNone(resp)
        self.assertIsNone(trader.last_request)

    def test_trailing_stop_callback_is_clamped_to_binance_limit(self) -> None:
        trader = CaptureTrader()

        resp = asyncio.run(trader.place_trailing_stop_order("TESTUSDT", "SELL", 1.234567, 15.0, 9.7))

        self.assertIsNotNone(resp)
        _, endpoint, params = trader.last_request
        self.assertEqual(endpoint, "/fapi/v1/order")
        self.assertEqual(params["activationPrice"], "1.2345")
        self.assertEqual(params["callbackRate"], "10.0")
        self.assertEqual(params["quantity"], "9")
        self.assertEqual(params["reduceOnly"], "true")


if __name__ == "__main__":
    unittest.main()
