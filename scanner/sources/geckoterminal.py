"""
GeckoTerminal 免费 API — 无需 API Key
用途: 抓取趋势池子、新上池子 → 发现妖币早期活跃
文档: https://api.geckoterminal.com/docs
"""
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_BASE = "https://api.geckoterminal.com/api/v2"
_HEADERS = {"Accept": "application/json;version=20230302"}

# GeckoTerminal network slug → chainIndex (OKX)
CHAIN_MAP = {
    "eth":         "1",
    "bsc":         "56",
    "solana":      "501",
    "polygon_pos": "137",
    "arbitrum":    "42161",
    "optimism":    "10",
    "base":        "8453",
    "avax":        "43114",
    "sui-network": "784",
    "ton":         "607",
}


async def _get(session: aiohttp.ClientSession, path: str, params: dict | None = None) -> dict | None:
    try:
        async with session.get(
            f"{_BASE}{path}", params=params, headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.debug("GeckoTerminal %d %s", resp.status, path)
    except Exception as e:
        logger.debug("GeckoTerminal 请求异常: %s", e)
    return None


async def fetch_trending_pools(session: aiohttp.ClientSession, page: int = 1) -> list[dict]:
    """全网趋势池子 (24H 交易量 + 涨幅加权)"""
    data = await _get(session, "/networks/trending_pools",
                      {"include": "base_token,dex", "page": page})
    if not data:
        return []
    return _parse_pools(data.get("data", []), source="gecko_trending")


async def fetch_new_pools(session: aiohttp.ClientSession, network: str = "eth") -> list[dict]:
    """某链最新上线池子"""
    data = await _get(session, f"/networks/{network}/new_pools",
                      {"include": "base_token,dex"})
    if not data:
        return []
    return _parse_pools(data.get("data", []), source="gecko_new")


async def fetch_top_pools(session: aiohttp.ClientSession, network: str = "eth", page: int = 1) -> list[dict]:
    """某链 top 池子 (by volume)"""
    data = await _get(session, f"/networks/{network}/pools",
                      {"include": "base_token,dex", "page": page, "sort": "h24_volume_usd_desc"})
    if not data:
        return []
    return _parse_pools(data.get("data", []), source="gecko_top")


def _parse_pools(pool_list: list, source: str) -> list[dict]:
    results = []
    for pool in pool_list:
        try:
            attr = pool.get("attributes", {})
            rel  = pool.get("relationships", {})

            base_token = rel.get("base_token", {}).get("data", {})
            network    = pool.get("relationships", {}).get("network", {}).get("data", {}).get("id", "")

            included = pool.get("_included_base_token", {})

            price_usd    = float(attr.get("base_token_price_usd") or 0)
            vol_24h      = float(attr.get("volume_usd", {}).get("h24") or 0)
            change_24h   = float(attr.get("price_change_percentage", {}).get("h24") or 0)
            liquidity    = float(attr.get("reserve_in_usd") or 0)
            name         = attr.get("name", "")
            address      = attr.get("address", "")

            # Extract base token address from pool name (TOKEN / WETH etc.)
            base_addr = base_token.get("id", "").split("_")[-1] if base_token else ""
            symbol_part = name.split(" / ")[0].strip() if " / " in name else name

            chain_id = CHAIN_MAP.get(network, "")
            if not chain_id:
                continue

            results.append({
                "symbol":        symbol_part,
                "name":          symbol_part,
                "chain":         network,
                "chain_id":      chain_id,
                "address":       base_addr.lower(),
                "pool_address":  address,
                "price_usd":     price_usd,
                "price_change_24h": change_24h,
                "volume_24h":    vol_24h,
                "liquidity":     liquidity,
                "source":        source,
                "links": {
                    "gecko": f"https://www.geckoterminal.com/{network}/pools/{address}",
                },
            })
        except Exception as e:
            logger.debug("GeckoTerminal pool 解析异常: %s", e)
    return results
