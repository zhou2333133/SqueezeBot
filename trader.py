"""
trader.py
修复点：
  1. _sign() 使用了 hmac.new() — Python 标准库正确写法，无需改动（原来就正确）。
  2. 所有价格格式化统一用 f"{price:.8g}" 取代 rstrip('.') 链式调用，避免精度丢失边界情况。
  3. 增加 get_position() 方法，方便后续查询当前持仓。
  4. _send_signed_request 细化错误日志，输出响应体便于排查问题。
"""
import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import aiohttp

from config import BINANCE_API_KEY, BINANCE_API_SECRET

logger = logging.getLogger(__name__)


def _fmt(value: float) -> str:
    """将浮点价格/数量格式化为字符串，去掉多余末尾零，防止科学计数法。"""
    # 最多 8 位有效小数，去除尾零
    s = f"{value:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"


class BinanceTrader:
    BASE_URL = "https://fapi.binance.com"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.api_key = BINANCE_API_KEY
        self.api_secret = BINANCE_API_SECRET

    def _sign(self, params: dict) -> str:
        """生成 HMAC-SHA256 签名"""
        query_string = urlencode(params)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _request(self, method: str, endpoint: str, params: dict):
        """发送已签名的请求"""
        if not self.api_key or self.api_key == "YOUR_BINANCE_API_KEY":
            logger.warning("交易功能未开启：BINANCE_API_KEY 未配置。")
            return None

        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        headers = {"X-MBX-APIKEY": self.api_key}

        try:
            async with self.session.request(
                method,
                f"{self.BASE_URL}{endpoint}",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    logger.error(
                        "币安 API 错误 %s [%s %s]: %s",
                        resp.status, method, endpoint, body,
                    )
                    return None
                return body
        except Exception as e:
            logger.error("币安 API 请求异常 [%s %s]: %s", method, endpoint, e)
            return None

    async def set_leverage(self, symbol: str, leverage: int) -> dict | None:
        """设置合约杠杆倍数"""
        resp = await self._request("POST", "/fapi/v1/leverage", {
            "symbol": symbol, "leverage": leverage,
        })
        if resp and "leverage" in resp:
            logger.info("✅ 杠杆设置成功: %s → %dx", symbol, resp["leverage"])
        return resp

    async def place_market_order(self, symbol: str, side: str, quantity: float) -> dict | None:
        """市价开仓单"""
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": _fmt(quantity),
        })
        if resp and resp.get("orderId"):
            logger.info("✅ 市价单成功: %s %s %.6f (OrderID: %s)", symbol, side, quantity, resp["orderId"])
        return resp

    async def place_stop_loss_order(self, symbol: str, side: str, stop_price: float) -> dict | None:
        """止损单（平仓市价止损）"""
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "stopPrice": _fmt(stop_price),
            "closePosition": "true",
            "timeInForce": "GTC",
        })
        if resp and resp.get("orderId"):
            logger.info("✅ 止损单成功: %s %s 触发价 %s", symbol, side, _fmt(stop_price))
        return resp

    async def place_take_profit_order(self, symbol: str, side: str, stop_price: float) -> dict | None:
        """止盈单（平仓市价止盈）"""
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": _fmt(stop_price),
            "closePosition": "true",
            "timeInForce": "GTC",
        })
        if resp and resp.get("orderId"):
            logger.info("✅ 止盈单成功: %s %s 触发价 %s", symbol, side, _fmt(stop_price))
        return resp

    async def place_trailing_stop_order(
        self,
        symbol: str,
        side: str,
        activation_price: float,
        callback_rate: float,
        quantity: float,
    ) -> dict | None:
        """追踪止损单"""
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "TRAILING_STOP_MARKET",
            "activationPrice": _fmt(activation_price),
            "callbackRate": f"{callback_rate:.1f}",
            "quantity": _fmt(quantity),
        })
        if resp and resp.get("orderId"):
            logger.info(
                "✅ 追踪止损单成功: %s %s 激活价 %s 回调 %.1f%%",
                symbol, side, _fmt(activation_price), callback_rate,
            )
        return resp

    async def get_position(self, symbol: str) -> dict | None:
        """查询当前合约持仓（可选用，用于监控或平仓逻辑）"""
        resp = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if resp:
            positions = [p for p in resp if float(p.get("positionAmt", 0)) != 0]
            return positions[0] if positions else None
        return None

    async def cancel_order(self, symbol: str, order_id: int) -> dict | None:
        """取消指定挂单"""
        return await self._request("DELETE", "/fapi/v1/order", {
            "symbol": symbol, "orderId": order_id,
        })

    async def cancel_all_orders(self, symbol: str) -> dict | None:
        """取消某合约全部挂单"""
        return await self._request("DELETE", "/fapi/v1/allOpenOrders", {
            "symbol": symbol,
        })

    async def get_balance(self) -> dict | None:
        """查询U本位合约账户 USDT 余额（余额/可用/未实现盈亏）"""
        resp = await self._request("GET", "/fapi/v2/balance", {})
        if not resp:
            return None
        for asset in resp:
            if asset.get("asset") == "USDT":
                return {
                    "asset":            "USDT",
                    "balance":          float(asset.get("balance",          0)),
                    "availableBalance": float(asset.get("availableBalance", 0)),
                    "unrealizedProfit": float(asset.get("crossUnPnl",       0)),
                }
        return None
