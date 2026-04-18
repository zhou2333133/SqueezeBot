"""
OKX OnChainOS DEX Market API 集成
文档: https://web3.okx.com/zh-hans/onchainos/dev-docs/home/what-is-onchainos

用途: 对 Binance Futures 信号进行链上 DEX 数据二次确认 —
  检查代币 DEX K线方向是否与拟交易方向一致。
  数据不可用或 API Key 未配置时自动跳过，不阻断交易。

认证: OK-ACCESS-KEY / OK-ACCESS-SIGN (HMAC-SHA256 Base64) /
      OK-ACCESS-TIMESTAMP (ISO-8601 UTC) / OK-ACCESS-PASSPHRASE
"""
import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

import aiohttp

from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.okx.com"

# Binance Futures symbol → on-chain token info
# chainIndex: 1=ETH, 56=BSC, 137=Polygon, 42161=Arbitrum, 10=Optimism, 501=Solana
# 原生代币使用 0xeeee...eeee 约定地址
_TOKEN_MAP: dict[str, dict] = {
    "ETHUSDT":   {"chainIndex": "1",     "tokenContractAddress": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},
    "BNBUSDT":   {"chainIndex": "56",    "tokenContractAddress": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},
    "SOLUSDT":   {"chainIndex": "501",   "tokenContractAddress": "So11111111111111111111111111111111111111112"},
    "AVAXUSDT":  {"chainIndex": "43114", "tokenContractAddress": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},
    "MATICUSDT": {"chainIndex": "137",   "tokenContractAddress": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},
    "ARBUSDT":   {"chainIndex": "42161", "tokenContractAddress": "0x912ce59144191c1204e64559fe8253a0e49e6548"},
    "OPUSDT":    {"chainIndex": "10",    "tokenContractAddress": "0x4200000000000000000000000000000000000042"},
    "UNIUSDT":   {"chainIndex": "1",     "tokenContractAddress": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"},
    "LINKUSDT":  {"chainIndex": "1",     "tokenContractAddress": "0x514910771af9ca656af840dff83e8264ecf986ca"},
    "AAVEUSDT":  {"chainIndex": "1",     "tokenContractAddress": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9"},
    "SUSHIUSDT": {"chainIndex": "1",     "tokenContractAddress": "0x6b3595068778dd592e39a122f4f5a5cf09c90fe2"},
    "CRVUSDT":   {"chainIndex": "1",     "tokenContractAddress": "0xd533a949740bb3306d119cc777fa900ba034cd52"},
    "LDOUSDT":   {"chainIndex": "1",     "tokenContractAddress": "0x5a98fcbea516cf06857215779fd812ca3bef1b32"},
    "MKRUSDT":   {"chainIndex": "1",     "tokenContractAddress": "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2"},
    "SNXUSDT":   {"chainIndex": "1",     "tokenContractAddress": "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f"},
    "1INCHUSDT": {"chainIndex": "1",     "tokenContractAddress": "0x111111111117dc0aa78b770fa6a738034120c302"},
}


def _sign(secret: str, ts: str, method: str, path: str) -> str:
    msg = ts + method.upper() + path
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


def _ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime(f"%Y-%m-%dT%H:%M:%S.{now.microsecond // 1000:03d}Z")


class OKXOnChainClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._enabled = bool(
            OKX_API_KEY and OKX_API_KEY != "YOUR_OKX_API_KEY"
            and OKX_SECRET_KEY and OKX_SECRET_KEY != "YOUR_OKX_SECRET_KEY"
        )

    def _headers(self, path: str) -> dict:
        ts = _ts()
        return {
            "OK-ACCESS-KEY":        OKX_API_KEY,
            "OK-ACCESS-SIGN":       _sign(OKX_SECRET_KEY, ts, "GET", path),
            "OK-ACCESS-TIMESTAMP":  ts,
            "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
            "Content-Type":         "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        if not self._enabled:
            return None
        try:
            full = path + (f"?{urlencode(params)}" if params else "")
            async with self.session.get(
                _BASE_URL + full,
                headers=self._headers(full),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.debug("OKX API %d %s", resp.status, path)
        except Exception as e:
            logger.debug("OKX 请求异常: %s", e)
        return None

    async def get_candles(self, symbol: str, bar: str = "1H", limit: int = 6) -> list | None:
        """获取代币 DEX OHLCV K线（最新在前）"""
        info = _TOKEN_MAP.get(symbol)
        if not info:
            return None
        data = await self._get("/api/v6/dex/market/candles", {**info, "bar": bar, "limit": limit})
        if data and data.get("code") == "0":
            return data.get("data")
        return None

    async def check_onchain_confirmation(self, symbol: str, direction: str) -> tuple[bool, str]:
        """
        检查近 6H DEX 价格方向是否与交易方向一致。
        返回 (True/False, 原因描述)。
        无数据时返回 (True, 跳过) — 不阻断交易。
        """
        if not self._enabled:
            return True, "OKX 未配置（跳过）"

        candles = await self.get_candles(symbol, bar="1H", limit=6)
        if not candles or len(candles) < 2:
            return True, "OKX 链上数据不可用（跳过）"

        try:
            # candles: [timestamp, open, high, low, close, vol_base, vol_usd, confirmed]
            latest_close = float(candles[0][4])
            oldest_open  = float(candles[-1][1])
            vol_usd = sum(float(c[6]) for c in candles if len(c) > 6)
            pct = (latest_close - oldest_open) / oldest_open * 100 if oldest_open else 0

            if direction == "LONG" and pct < -8.0:
                return False, f"OKX DEX 近6H 跌幅 {pct:.2f}%，与做多背离"
            if direction == "SHORT" and pct > 8.0:
                return False, f"OKX DEX 近6H 涨幅 {pct:.2f}%，与做空背离"

            return True, f"✅ OKX链上确认: 6H DEX {pct:+.2f}%，成交量 ${vol_usd/1e6:.2f}M"
        except (IndexError, TypeError, ValueError) as e:
            logger.debug("OKX candle 解析异常: %s", e)
            return True, "OKX 数据解析异常（跳过）"
