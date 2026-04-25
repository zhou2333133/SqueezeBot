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
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urlencode

import aiohttp

from config import BINANCE_API_KEY, BINANCE_API_SECRET

logger = logging.getLogger(__name__)


def _fmt(value: float) -> str:
    """将浮点价格/数量格式化为字符串，去掉多余末尾零，防止科学计数法。"""
    # 最多 8 位有效小数，去除尾零
    s = f"{value:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_decimal(value: Decimal) -> str:
    """格式化 Decimal，避免科学计数法和多余尾零。"""
    if value == 0:
        return "0"
    s = format(value.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


def _as_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


class BinanceTrader:
    BASE_URL = "https://fapi.binance.com"
    _SERVER_TIME_SYNC_INTERVAL = 300.0   # 5min 重新拉一次服务器时间
    _SERVER_TIME_SYNC_TIMEOUT = 5.0
    _SERVER_TIME_WARN_OFFSET_MS = 1000   # 偏差 > 1s 才打警告，避免日志噪音

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.api_key = BINANCE_API_KEY
        self.api_secret = BINANCE_API_SECRET
        self._symbol_filters: dict[str, dict] = {}
        # ── 时钟漂移修复 ──────────────────────────────────────────────────
        # Windows 系统经常出现 +/- 数秒漂移，导致 -1021 Timestamp out of recvWindow。
        # 我们定期拉 GET /fapi/v1/time，按服务器时间补偿本地戳。
        self._time_offset_ms: int = 0
        self._last_time_sync: float = 0.0

    async def _ensure_server_time_synced(self) -> None:
        now = time.time()
        if now - self._last_time_sync < self._SERVER_TIME_SYNC_INTERVAL:
            return
        try:
            async with self.session.get(
                f"{self.BASE_URL}/fapi/v1/time",
                timeout=aiohttp.ClientTimeout(total=self._SERVER_TIME_SYNC_TIMEOUT),
            ) as resp:
                if resp.status >= 400:
                    return
                data = await resp.json()
                server_ms = int(data.get("serverTime", 0) or 0)
                if server_ms <= 0:
                    return
                local_ms = int(time.time() * 1000)
                offset = server_ms - local_ms
                prev = self._time_offset_ms
                self._time_offset_ms = offset
                self._last_time_sync = now
                if abs(offset) >= self._SERVER_TIME_WARN_OFFSET_MS or abs(offset - prev) >= self._SERVER_TIME_WARN_OFFSET_MS:
                    logger.warning(
                        "⏱ Binance 服务器时间偏差 %+d ms (本地 %d → 服务器 %d)，已自动补偿",
                        offset, local_ms, server_ms,
                    )
        except Exception as e:
            logger.debug("⏱ Binance server time 同步失败: %s", e)

    def _sign(self, params: dict) -> str:
        """生成 HMAC-SHA256 签名"""
        query_string = urlencode(params)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _request(self, method: str, endpoint: str, params: dict, _retry_on_time_skew: bool = True):
        """发送已签名的请求；遇到 -1021 时强制重新同步时间并自动重试一次。"""
        if not self.api_key or self.api_key == "YOUR_BINANCE_API_KEY":
            logger.warning("交易功能未开启：BINANCE_API_KEY 未配置。")
            return None

        await self._ensure_server_time_synced()
        # 复制一份 params，重试时不污染调用方
        signed = dict(params)
        signed["timestamp"]  = int(time.time() * 1000) + self._time_offset_ms
        signed["recvWindow"] = 10000   # 叠加 offset 修复后再容忍 ±10s
        signed["signature"]  = self._sign(signed)
        headers = {"X-MBX-APIKEY": self.api_key}

        try:
            async with self.session.request(
                method,
                f"{self.BASE_URL}{endpoint}",
                headers=headers,
                params=signed,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    code = (body or {}).get("code") if isinstance(body, dict) else None
                    if code == -1021 and _retry_on_time_skew:
                        # 强制立即重新同步时间并重试一次（recvWindow 时间戳偏差）
                        logger.warning("⏱ -1021 时间戳偏差，强制重新同步并重试一次")
                        self._last_time_sync = 0.0
                        await self._ensure_server_time_synced()
                        return await self._request(method, endpoint, params, _retry_on_time_skew=False)
                    logger.error(
                        "币安 API 错误 %s [%s %s]: %s",
                        resp.status, method, endpoint, body,
                    )
                    return None
                return body
        except Exception as e:
            logger.error("币安 API 请求异常 [%s %s]: %s", method, endpoint, e)
            return None

    async def _ensure_exchange_filters(self) -> bool:
        """加载 Binance 合约交易规则，用于按 tickSize/stepSize 格式化下单参数。"""
        if self._symbol_filters:
            return True
        try:
            async with self.session.get(
                f"{self.BASE_URL}/fapi/v1/exchangeInfo",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    logger.error("币安 exchangeInfo 获取失败 %s: %s", resp.status, body)
                    return False
        except Exception as e:
            logger.error("币安 exchangeInfo 请求异常: %s", e)
            return False

        filters: dict[str, dict] = {}
        for item in body.get("symbols", []) or []:
            symbol = item.get("symbol")
            if not symbol:
                continue
            by_type = {f.get("filterType"): f for f in item.get("filters", []) or []}
            filters[symbol] = {
                "status": item.get("status", ""),
                "filters": by_type,
            }
        self._symbol_filters = filters
        return bool(filters)

    async def _rules(self, symbol: str) -> dict | None:
        symbol = symbol.upper()
        if not await self._ensure_exchange_filters():
            return None
        rules = self._symbol_filters.get(symbol)
        if not rules:
            logger.error("币安交易规则缺失: %s，跳过下单", symbol)
            return None
        if rules.get("status") and rules.get("status") != "TRADING":
            logger.error("币安合约非交易状态: %s status=%s，跳过下单", symbol, rules.get("status"))
            return None
        return rules

    @staticmethod
    def _floor_to_step(value, step) -> Decimal:
        v = _as_decimal(value)
        s = _as_decimal(step)
        if s <= 0:
            return v
        return (v / s).to_integral_value(rounding=ROUND_DOWN) * s

    async def _price_param(self, symbol: str, price: float) -> str | None:
        rules = await self._rules(symbol)
        if not rules:
            return None
        price_filter = rules["filters"].get("PRICE_FILTER", {})
        tick = price_filter.get("tickSize", "0")
        normalized = self._floor_to_step(price, tick)
        min_price = _as_decimal(price_filter.get("minPrice", "0"))
        max_price = _as_decimal(price_filter.get("maxPrice", "0"))
        if normalized <= 0 or (min_price > 0 and normalized < min_price):
            logger.error("价格低于交易规则: %s price=%s normalized=%s min=%s", symbol, price, normalized, min_price)
            return None
        if max_price > 0 and normalized > max_price:
            logger.error("价格高于交易规则: %s price=%s normalized=%s max=%s", symbol, price, normalized, max_price)
            return None
        return _fmt_decimal(normalized)

    async def _quantity_param(
        self,
        symbol: str,
        quantity: float,
        *,
        market: bool,
        price: float | None = None,
        enforce_notional: bool = False,
    ) -> str | None:
        rules = await self._rules(symbol)
        if not rules:
            return None
        filters = rules["filters"]
        lot = filters.get("MARKET_LOT_SIZE" if market else "LOT_SIZE") or filters.get("LOT_SIZE", {})
        step = lot.get("stepSize", "0")
        normalized = self._floor_to_step(quantity, step)
        min_qty = _as_decimal(lot.get("minQty", "0"))
        max_qty = _as_decimal(lot.get("maxQty", "0"))
        if normalized <= 0 or (min_qty > 0 and normalized < min_qty):
            logger.error("数量低于交易规则: %s qty=%s normalized=%s min=%s", symbol, quantity, normalized, min_qty)
            return None
        if max_qty > 0 and normalized > max_qty:
            logger.error("数量高于交易规则: %s qty=%s normalized=%s max=%s", symbol, quantity, normalized, max_qty)
            return None

        min_notional = _as_decimal((filters.get("MIN_NOTIONAL") or {}).get("notional", "0"))
        if enforce_notional and price and min_notional > 0:
            notional = normalized * _as_decimal(price)
            if notional < min_notional:
                logger.error(
                    "名义价值低于交易规则: %s qty=%s price=%s notional=%s min=%s",
                    symbol, normalized, price, notional, min_notional,
                )
                return None
        return _fmt_decimal(normalized)

    async def get_position_mode(self) -> dict | None:
        """查询当前持仓模式。当前实盘保护逻辑只支持单向持仓。"""
        return await self._request("GET", "/fapi/v1/positionSide/dual", {})

    async def is_hedge_mode(self) -> bool | None:
        resp = await self.get_position_mode()
        if resp is None:
            return None
        val = resp.get("dualSidePosition", False)
        if isinstance(val, str):
            return val.lower() == "true"
        return bool(val)

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
        qty = await self._quantity_param(symbol, quantity, market=True, enforce_notional=False)
        if not qty:
            return None
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
        })
        if resp and resp.get("orderId"):
            logger.info("✅ 市价单成功: %s %s %.6f (OrderID: %s)", symbol, side, quantity, resp["orderId"])
        return resp

    async def place_reduce_only_market_order(self, symbol: str, side: str, quantity: float) -> dict | None:
        """只减仓市价单，供TP/SL/紧急平仓使用，避免状态延迟造成反向开仓。"""
        qty = await self._quantity_param(symbol, quantity, market=True, enforce_notional=False)
        if not qty:
            return None
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
        })
        if resp and resp.get("orderId"):
            logger.info("✅ ReduceOnly 市价平仓: %s %s %.6f (OrderID: %s)", symbol, side, quantity, resp["orderId"])
        return resp

    async def place_stop_loss_order(self, symbol: str, side: str, stop_price: float) -> dict | None:
        """止损单（平仓市价止损）"""
        stop = await self._price_param(symbol, stop_price)
        if not stop:
            return None
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "stopPrice": stop,
            "closePosition": "true",
        })
        if resp and resp.get("orderId"):
            logger.info("✅ 止损单成功: %s %s 触发价 %s", symbol, side, stop)
        return resp

    async def place_take_profit_order(self, symbol: str, side: str, stop_price: float) -> dict | None:
        """止盈单（平仓市价止盈）"""
        stop = await self._price_param(symbol, stop_price)
        if not stop:
            return None
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": stop,
            "closePosition": "true",
        })
        if resp and resp.get("orderId"):
            logger.info("✅ 止盈单成功: %s %s 触发价 %s", symbol, side, stop)
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
        qty = await self._quantity_param(symbol, quantity, market=True, enforce_notional=False)
        activ = await self._price_param(symbol, activation_price)
        if not qty or not activ:
            return None
        callback = max(0.1, min(10.0, float(callback_rate)))
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "TRAILING_STOP_MARKET",
            "activationPrice": activ,
            "callbackRate": f"{callback:.1f}",
            "quantity": qty,
            "reduceOnly": "true",
        })
        if resp and resp.get("orderId"):
            logger.info(
                "✅ 追踪止损单成功: %s %s 激活价 %s 回调 %.1f%%",
                symbol, side, activ, callback,
            )
        return resp

    async def get_position(self, symbol: str) -> dict | None:
        """查询当前合约持仓（可选用，用于监控或平仓逻辑）"""
        resp = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if resp is None:
            raise RuntimeError("positionRisk request failed")
        positions = [p for p in resp if float(p.get("positionAmt", 0)) != 0]
        return positions[0] if positions else None

    async def place_limit_ioc_order(self, symbol: str, side: str, quantity: float, price: float) -> dict | None:
        """IOC 限价单（Immediate Or Cancel）— 防飞单追高核心机制"""
        qty = await self._quantity_param(symbol, quantity, market=False, price=price, enforce_notional=True)
        px = await self._price_param(symbol, price)
        if not qty or not px:
            return None
        resp = await self._request("POST", "/fapi/v1/order", {
            "symbol":      symbol,
            "side":        side,
            "type":        "LIMIT",
            "timeInForce": "IOC",
            "quantity":    qty,
            "price":       px,
        })
        if resp and resp.get("orderId"):
            filled = float(resp.get("executedQty", 0))
            status = resp.get("status", "")
            logger.info(
                "✅ IOC限价单: %s %s %.6f @ %.6f | 状态:%s 实际成交:%.6f",
                symbol, side, quantity, price, status, filled,
            )
        return resp

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
