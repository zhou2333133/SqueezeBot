"""
OKX OnChainOS Market API
REST: /api/v6/dex/market/price-info  /api/v6/dex/market/token/search
文档: https://web3.okx.com/onchainos/dev-docs/market/market-api-introduction
"""
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

import aiohttp

from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE

logger = logging.getLogger(__name__)
_BASE = "https://web3.okx.com"

# chainIndex → human name
CHAIN_NAMES = {
    "1": "eth", "56": "bsc", "501": "sol", "137": "polygon",
    "42161": "arb", "10": "op", "8453": "base", "43114": "avax",
    "784": "sui", "607": "ton",
}


def _sign(secret: str, ts: str, method: str, path: str, body: str = "") -> str:
    msg = ts + method.upper() + path + body
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


def _ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime(f"%Y-%m-%dT%H:%M:%S.{now.microsecond // 1000:03d}Z")


def _headers(path: str) -> dict:
    ts = _ts()
    h = {
        "OK-ACCESS-KEY":       OKX_API_KEY,
        "OK-ACCESS-SIGN":      _sign(OKX_SECRET_KEY, ts, "GET", path),
        "OK-ACCESS-TIMESTAMP": ts,
        "Content-Type":        "application/json",
    }
    if OKX_PASSPHRASE:
        h["OK-ACCESS-PASSPHRASE"] = OKX_PASSPHRASE
    return h


def _headers_post(path: str, body: str = "") -> dict:
    ts = _ts()
    h = {
        "OK-ACCESS-KEY":       OKX_API_KEY,
        "OK-ACCESS-SIGN":      _sign(OKX_SECRET_KEY, ts, "POST", path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "Content-Type":        "application/json",
    }
    if OKX_PASSPHRASE:
        h["OK-ACCESS-PASSPHRASE"] = OKX_PASSPHRASE
    return h


def _enabled() -> bool:
    return bool(OKX_API_KEY and OKX_API_KEY != "YOUR_OKX_API_KEY"
                and OKX_SECRET_KEY and OKX_SECRET_KEY != "YOUR_OKX_SECRET_KEY"
                and OKX_PASSPHRASE)


def credentials_status() -> dict:
    return {
        "enabled":        _enabled(),
        "api_key_set":    bool(OKX_API_KEY and OKX_API_KEY != "YOUR_OKX_API_KEY"),
        "secret_set":     bool(OKX_SECRET_KEY and OKX_SECRET_KEY != "YOUR_OKX_SECRET_KEY"),
        "passphrase_set": bool(OKX_PASSPHRASE),
        "base_url":       _BASE,
    }


def _json_body(data) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _first_data(data: dict) -> dict:
    payload = data.get("data", {})
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else {}
    return payload if isinstance(payload, dict) else {}


def _float_any(d: dict, *keys: str) -> float:
    for key in keys:
        val = d.get(key)
        if val is not None and val != "":
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return 0.0


def _int_any(d: dict, *keys: str) -> int:
    for key in keys:
        val = d.get(key)
        if val is not None and val != "":
            try:
                return int(float(val))
            except (TypeError, ValueError):
                pass
    return 0


async def search_tokens(
    session: aiohttp.ClientSession,
    keyword: str,
    chains: str = "1,56,501,137,42161",
    limit: int = 20,
) -> list[dict]:
    """搜索链上 token（名称/符号/地址）"""
    if not _enabled():
        return []
    params = {"chains": chains, "search": keyword, "limit": limit}
    path   = f"/api/v6/dex/market/token/search?{urlencode(params)}"
    try:
        async with session.get(
            _BASE + path, headers=_headers(path),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == "0":
                    return _parse_token_list(data.get("data", []))
    except Exception as e:
        logger.debug("OKX token search 异常: %s", e)
    return []


async def get_token_price_info(
    session: aiohttp.ClientSession,
    chain_index: str,
    address: str,
) -> dict | None:
    """获取 token 价格/市值/持仓人数/成交量（OKX 最全的链上数据）"""
    if not _enabled():
        return None
    body_dict = [{"chainIndex": str(chain_index), "tokenContractAddress": address}]
    body_str  = _json_body(body_dict)
    path      = "/api/v6/dex/market/price-info"
    try:
        async with session.post(
            _BASE + path,
            data=body_str,
            headers=_headers_post(path, body_str),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == "0":
                    d = _first_data(data)
                    return {
                        "price_usd":         _float_any(d, "price"),
                        "market_cap":        _float_any(d, "marketCap", "marketCapUsd"),
                        "liquidity":         _float_any(d, "liquidity", "liquidityUsd"),
                        "holder_count":      _int_any(d, "holders", "holderCount"),
                        "price_change_5m":   _float_any(d, "priceChange5M", "priceChange5m"),
                        "price_change_1h":   _float_any(d, "priceChange1H", "priceChange1h"),
                        "price_change_4h":   _float_any(d, "priceChange4H", "priceChange4h"),
                        "price_change_24h":  _float_any(d, "priceChange24H", "priceChange24h", "change24H"),
                        "volume_5m":         _float_any(d, "volume5M", "volume5m"),
                        "volume_1h":         _float_any(d, "volume1H", "volume1h"),
                        "volume_24h":        _float_any(d, "volume24H", "volume24h"),
                        "tx_count_5m":       _int_any(d, "txs5M", "txs5m", "txCount5M"),
                        "tx_count_1h":       _int_any(d, "txs1H", "txs1h", "txCount1H"),
                        "circ_supply":       _float_any(d, "circSupply", "circulatingSupply"),
                    }
                logger.debug("OKX price-info code=%s msg=%s", data.get("code"), data.get("msg") or data.get("message"))
            else:
                logger.debug("OKX price-info HTTP %s: %s", resp.status, (await resp.text())[:200])
    except Exception as e:
        logger.debug("OKX price-info 异常: %s", e)
    return None


async def get_token_trades(
    session: aiohttp.ClientSession,
    chain_index: str,
    token_address: str,
    limit: int = 100,
) -> dict:
    """获取近期成交记录，返回大单(>$5K)占比，用于识别机构吸筹。"""
    if not _enabled():
        return {"large_trade_pct": 0.0, "total_usd": 0.0, "trade_count": 0}
    body_dict = {
        "chainIndex":           chain_index,
        "tokenContractAddress": token_address,
        "limit":                str(limit),
    }
    body_str = _json_body(body_dict)
    path     = "/api/v5/dex/aggregator/trade-records"
    try:
        async with session.post(
            _BASE + path,
            data=body_str,
            headers=_headers_post(path, body_str),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == "0":
                    payload = data.get("data", {})
                    if isinstance(payload, list):
                        trades = payload
                    elif isinstance(payload, dict):
                        trades = payload.get("tradeList", []) or payload.get("list", [])
                    else:
                        trades = []
                    if not trades:
                        return {"large_trade_pct": 0.0, "total_usd": 0.0, "trade_count": 0}
                    total_usd   = sum(_float_any(t, "usdValue", "volumeUsd", "amountUsd") for t in trades)
                    large_usd   = sum(_float_any(t, "usdValue", "volumeUsd", "amountUsd") for t in trades
                                      if _float_any(t, "usdValue", "volumeUsd", "amountUsd") >= 5000)
                    large_pct   = large_usd / total_usd if total_usd > 0 else 0.0
                    return {
                        "large_trade_pct": round(large_pct, 4),
                        "total_usd":       round(total_usd, 2),
                        "trade_count":     len(trades),
                    }
    except Exception as e:
        logger.debug("OKX trade-records 异常: %s", e)
    return {"large_trade_pct": 0.0, "total_usd": 0.0, "trade_count": 0}


async def diagnose(session: aiohttp.ClientSession) -> dict:
    status = credentials_status()
    if not status["enabled"]:
        return {**status, "ok": False, "reason": "missing_okx_credentials"}

    search = await search_tokens(session, "weth", chains="1,10", limit=3)
    price = await get_token_price_info(
        session,
        "1",
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    )
    return {
        **status,
        "ok": bool(search or price),
        "search_count": len(search),
        "price_info_ok": bool(price),
    }


def _parse_token_list(items: list) -> list[dict]:
    results = []
    for t in items:
        try:
            chain = CHAIN_NAMES.get(str(t.get("chainIndex", "")), "")
            results.append({
                "symbol":       t.get("tokenSymbol", ""),
                "name":         t.get("tokenName", ""),
                "chain":        chain,
                "chain_id":     str(t.get("chainIndex", "")),
                "address":      (t.get("tokenContractAddress") or "").lower(),
                "price_usd":    _float_any(t, "price"),
                "price_change_24h": _float_any(t, "change24H", "priceChange24H"),
                "holder_count": _int_any(t, "holders", "holderCount"),
                "liquidity":    _float_any(t, "liquidity"),
                "market_cap":   _float_any(t, "marketCap", "marketCapUsd"),
                "logo_url":     t.get("tokenLogoUrl", ""),
                "tags":         t.get("tagList", []),
                "source":       "okx_search",
            })
        except Exception as e:
            logger.debug("OKX token parse 异常: %s", e)
    return results
