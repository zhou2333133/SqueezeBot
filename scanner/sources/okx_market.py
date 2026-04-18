"""
OKX OnChainOS Market API
REST: /api/v6/dex/market/price-info  /api/v6/dex/market/token/search
复用 okx_client.py 的签名机制
文档: https://web3.okx.com/onchainos/dev-docs/market/market-api-introduction
"""
import logging
from urllib.parse import urlencode

import aiohttp

from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE
from okx_client import _sign, _ts          # reuse auth helpers

logger = logging.getLogger(__name__)
_BASE = "https://www.okx.com"

# chainIndex → human name
CHAIN_NAMES = {
    "1": "eth", "56": "bsc", "501": "sol", "137": "polygon",
    "42161": "arb", "10": "op", "8453": "base", "43114": "avax",
    "784": "sui", "607": "ton",
}


def _headers(path: str) -> dict:
    ts = _ts()
    return {
        "OK-ACCESS-KEY":        OKX_API_KEY,
        "OK-ACCESS-SIGN":       _sign(OKX_SECRET_KEY, ts, "GET", path),
        "OK-ACCESS-TIMESTAMP":  ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type":         "application/json",
    }


def _headers_post(path: str, body: str = "") -> dict:
    ts = _ts()
    sign_input = ts + "POST" + path + body
    from okx_client import _sign as _s
    import base64, hashlib, hmac
    sig = base64.b64encode(
        hmac.new(OKX_SECRET_KEY.encode(), sign_input.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY":        OKX_API_KEY,
        "OK-ACCESS-SIGN":       sig,
        "OK-ACCESS-TIMESTAMP":  ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type":         "application/json",
    }


def _enabled() -> bool:
    return bool(OKX_API_KEY and OKX_API_KEY != "YOUR_OKX_API_KEY"
                and OKX_SECRET_KEY and OKX_SECRET_KEY != "YOUR_OKX_SECRET_KEY")


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
    import json
    body_dict = {"chainIndex": chain_index, "tokenContractAddress": address}
    body_str  = json.dumps(body_dict)
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
                    d = data.get("data", {})
                    return {
                        "price_usd":         float(d.get("price") or 0),
                        "market_cap":        float(d.get("marketCap") or 0),
                        "liquidity":         float(d.get("liquidity") or 0),
                        "holder_count":      int(d.get("holders") or 0),
                        "price_change_5m":   float(d.get("priceChange5M") or 0),
                        "price_change_1h":   float(d.get("priceChange1H") or 0),
                        "price_change_4h":   float(d.get("priceChange4H") or 0),
                        "price_change_24h":  float(d.get("priceChange24H") or 0),
                        "volume_5m":         float(d.get("volume5M") or 0),
                        "volume_1h":         float(d.get("volume1H") or 0),
                        "volume_24h":        float(d.get("volume24H") or 0),
                        "tx_count_5m":       int(d.get("txs5M") or 0),
                        "tx_count_1h":       int(d.get("txs1H") or 0),
                        "circ_supply":       float(d.get("circSupply") or 0),
                    }
    except Exception as e:
        logger.debug("OKX price-info 异常: %s", e)
    return None


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
                "price_usd":    float(t.get("price") or 0),
                "price_change_24h": float(t.get("change24H") or 0),
                "holder_count": int(t.get("holders") or 0),
                "liquidity":    float(t.get("liquidity") or 0),
                "market_cap":   float(t.get("marketCap") or 0),
                "logo_url":     t.get("tokenLogoUrl", ""),
                "tags":         t.get("tagList", []),
                "source":       "okx_search",
            })
        except Exception as e:
            logger.debug("OKX token parse 异常: %s", e)
    return results
