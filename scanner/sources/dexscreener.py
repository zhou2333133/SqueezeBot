"""
DEX Screener 免费 API — 无需 API Key
用途: trending token profiles、boosted tokens、token 详情补充
文档: https://docs.dexscreener.com/api/reference
"""
import logging
import aiohttp

logger = logging.getLogger(__name__)
_BASE = "https://api.dexscreener.com"

CHAIN_ID_MAP = {
    "ethereum": "1", "bsc": "56", "solana": "501",
    "polygon":  "137", "arbitrum": "42161", "optimism": "10",
    "base":     "8453", "avalanche": "43114",
}


async def _get(session: aiohttp.ClientSession, path: str) -> dict | list | None:
    try:
        async with session.get(
            f"{_BASE}{path}", timeout=aiohttp.ClientTimeout(total=12),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.debug("DEXScreener %d %s", resp.status, path)
    except Exception as e:
        logger.debug("DEXScreener 请求异常: %s", e)
    return None


async def fetch_token_boosts(session: aiohttp.ClientSession) -> list[dict]:
    """最近被 boost 推广的 token（反映社区热度）"""
    data = await _get(session, "/token-boosts/top/v1")
    if not data:
        return []
    items = data if isinstance(data, list) else data.get("pairs", [])
    results = []
    for idx, item in enumerate(items[:50]):
        try:
            chain    = item.get("chainId", "")
            address  = item.get("tokenAddress", "").lower()
            desc     = item.get("description", "")
            links_raw = item.get("links", [])
            website  = next((x.get("url") for x in links_raw if x.get("type") == "website"), "")
            chain_id = CHAIN_ID_MAP.get(chain, "")
            if not address:
                continue
            results.append({
                "symbol":    item.get("header", address[:8]),
                "name":      item.get("description", "")[:40],
                "chain":     chain,
                "chain_id":  chain_id,
                "address":   address,
                "boost_rank": idx + 1,
                "source":    "dex_boost",
                "links": {
                    "dexscreener": item.get("url", ""),
                    "website":     website,
                },
            })
        except Exception as e:
            logger.debug("DEXScreener boost 解析: %s", e)
    return results


async def fetch_latest_profiles(session: aiohttp.ClientSession) -> list[dict]:
    """最新 token profiles（新上线的有社交媒体描述的项目）"""
    data = await _get(session, "/token-profiles/latest/v1")
    if not data:
        return []
    items = data if isinstance(data, list) else []
    results = []
    for item in items[:40]:
        try:
            chain   = item.get("chainId", "")
            address = item.get("tokenAddress", "").lower()
            chain_id = CHAIN_ID_MAP.get(chain, "")
            results.append({
                "symbol":   item.get("header", address[:8]),
                "name":     item.get("description", "")[:40],
                "chain":    chain,
                "chain_id": chain_id,
                "address":  address,
                "source":   "dex_profile",
                "links": {"dexscreener": item.get("url", "")},
            })
        except Exception as e:
            logger.debug("DEXScreener profile 解析: %s", e)
    return results


async def fetch_token_detail(session: aiohttp.ClientSession, chain: str, address: str) -> dict | None:
    """补充某个 token 的价格/成交/流动性"""
    data = await _get(session, f"/latest/dex/tokens/{address}")
    if not data:
        return None
    pairs = data.get("pairs", [])
    if not pairs:
        return None
    # 取流动性最高的 pair
    pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd") or 0), reverse=True)
    p = pairs[0]
    base = p.get("baseToken", {})
    return {
        "symbol":         base.get("symbol", ""),
        "name":           base.get("name", ""),
        "price_usd":      float(p.get("priceUsd") or 0),
        "price_change_1h":  float((p.get("priceChange") or {}).get("h1") or 0),
        "price_change_24h": float((p.get("priceChange") or {}).get("h24") or 0),
        "volume_24h":     float((p.get("volume") or {}).get("h24") or 0),
        "liquidity":      float((p.get("liquidity") or {}).get("usd") or 0),
        "market_cap":     float(p.get("marketCap") or 0),
        "links": {
            "dexscreener": p.get("url", ""),
        },
    }
