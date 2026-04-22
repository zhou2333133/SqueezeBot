"""
OKX Onchain OS Market API adapter.

官方 Market 能力:
- price-info: 批量查询 token 交易信息，最多 100 个地址
- token/search: 按符号或合约搜索 token
- token/hot-token: Trending / X-mentioned 热门榜
- token/advanced-info: 风险、持仓集中度、开发者行为
- token/holder: 前 100 持有人与标签过滤
- trades: 最新链上成交活动
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from config import config_manager, configured_okx_credentials, okx_credentials_status
from scanner.provider_metrics import (
    provider_metrics_snapshot,
    record_provider_call,
    record_provider_skip,
)

logger = logging.getLogger(__name__)
_BASE = "https://web3.okx.com"

# chainIndex -> human name
CHAIN_NAMES = {
    "1": "eth", "56": "bsc", "501": "sol", "137": "polygon",
    "42161": "arb", "10": "op", "8453": "base", "43114": "avax",
    "784": "sui", "607": "ton",
}

CHAIN_IDS = {v: k for k, v in CHAIN_NAMES.items()}
CHAIN_IDS.update({
    "ethereum": "1",
    "bnb": "56",
    "binance": "56",
    "solana": "501",
    "arbitrum": "42161",
    "optimism": "10",
    "avalanche": "43114",
})

_cred_cursor = 0
_request_lock = asyncio.Lock()
_last_request_at: dict[str, float] = {}
_endpoint_failures: dict[str, int] = {}
_endpoint_backoff_until: dict[str, float] = {}
_unsupported_chain_backoff_until: dict[str, float] = {}
_ENDPOINT_BACKOFF_SECONDS = 600
_UNSUPPORTED_CHAIN_BACKOFF_SECONDS = 3600
_ENDPOINT_FAILURE_LIMIT = 3


def _sign(secret: str, ts: str, method: str, path: str, body: str = "") -> str:
    msg = ts + method.upper() + path + body
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


def _ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime(f"%Y-%m-%dT%H:%M:%S.{now.microsecond // 1000:03d}Z")


def _enabled() -> bool:
    return bool(configured_okx_credentials())


def _normalize_address(chain_id: str, address: str) -> str:
    address = str(address or "").strip()
    # OKX 文档只要求 EVM 链地址小写；Solana/Sui/TON 等地址大小写可能有语义。
    if str(chain_id) not in ("501", "784", "607"):
        return address.lower()
    return address


def _next_credential() -> dict | None:
    global _cred_cursor
    creds = configured_okx_credentials()
    if not creds:
        return None
    cred = creds[_cred_cursor % len(creds)]
    _cred_cursor += 1
    return cred


async def _throttle(cred: dict) -> None:
    min_interval = float(config_manager.settings.get("OKX_MIN_REQUEST_INTERVAL", 0.20))
    key = cred["key"]
    async with _request_lock:
        now = time.monotonic()
        last = _last_request_at.get(key, 0.0)
        wait = min_interval - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at[key] = time.monotonic()


def _headers(cred: dict, method: str, path: str, body: str = "") -> dict:
    ts = _ts()
    return {
        "OK-ACCESS-KEY": cred["key"],
        "OK-ACCESS-SIGN": _sign(cred["secret"], ts, method, path, body),
        "OK-ACCESS-PASSPHRASE": cred["passphrase"],
        "OK-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


def credentials_status() -> dict:
    return {
        **okx_credentials_status(),
        "min_request_interval": config_manager.settings.get("OKX_MIN_REQUEST_INTERVAL", 0.20),
        "metrics": provider_metrics_snapshot("okx"),
    }


def _json_body(data) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _query_first(path: str, key: str) -> str:
    try:
        values = parse_qs(urlsplit(path).query).get(key, [])
        return str(values[0]) if values else ""
    except Exception:
        return ""


def _looks_like_unsupported_chain(reason: str) -> bool:
    text = (reason or "").lower()
    return "unsupported" in text or "not support" in text or "not supported" in text


def _register_endpoint_result(endpoint: str, ok: bool, status: int | str, reason: str, chain: str = "") -> None:
    if ok:
        _endpoint_failures.pop(endpoint, None)
        _endpoint_backoff_until.pop(endpoint, None)
        return

    reason_text = str(reason or "")
    if chain and _looks_like_unsupported_chain(reason_text):
        _unsupported_chain_backoff_until[f"{endpoint}:{chain}"] = time.time() + _UNSUPPORTED_CHAIN_BACKOFF_SECONDS
        return

    failures = _endpoint_failures.get(endpoint, 0) + 1
    _endpoint_failures[endpoint] = failures
    if failures < _ENDPOINT_FAILURE_LIMIT:
        return
    if str(status) in {"400", "401", "403", "404", "422", "429", "502"}:
        _endpoint_backoff_until[endpoint] = time.time() + _ENDPOINT_BACKOFF_SECONDS


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    body: str = "",
    endpoint: str,
    expected_items: int = 0,
) -> dict | list | None:
    now = time.time()
    chain = _query_first(path, "chainIndex")
    chain_backoff_key = f"{endpoint}:{chain}" if chain else ""
    if chain_backoff_key and _unsupported_chain_backoff_until.get(chain_backoff_key, 0.0) > now:
        record_provider_skip("okx", endpoint, f"unsupported_chain_backoff:{chain}", items=expected_items)
        return None
    if _endpoint_backoff_until.get(endpoint, 0.0) > now:
        record_provider_skip("okx", endpoint, "endpoint_backoff", items=expected_items)
        return None

    cred = _next_credential()
    if not cred:
        record_provider_skip("okx", endpoint, "missing_okx_credentials", items=expected_items)
        return None

    await _throttle(cred)
    try:
        kwargs = {
            "headers": _headers(cred, method, path, body),
            "timeout": aiohttp.ClientTimeout(total=12),
        }
        if body:
            kwargs["data"] = body
        async with session.request(method, _BASE + path, **kwargs) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except Exception:
                data = {}
            ok = resp.status == 200 and isinstance(data, dict) and data.get("code") == "0"
            items = 0
            if ok:
                payload = data.get("data")
                items = len(payload) if isinstance(payload, list) else (1 if payload else 0)
            reason = (data.get("msg") or data.get("message") or "") if isinstance(data, dict) else ""
            error = "" if ok else text[:160]
            record_provider_call(
                "okx",
                endpoint,
                ok,
                status=resp.status,
                reason=reason,
                error=error,
                items=items,
            )
            _register_endpoint_result(endpoint, ok, resp.status, f"{reason} {error}", chain)
            if ok:
                return data
            logger.debug("OKX %s failed status=%s body=%s", endpoint, resp.status, text[:200])
    except Exception as e:
        record_provider_call("okx", endpoint, False, status="exception", error=f"{type(e).__name__}: {e}")
        _register_endpoint_result(endpoint, False, "exception", f"{type(e).__name__}: {e}", chain)
        logger.debug("OKX %s 异常: %s", endpoint, e)
    return None


def _data_list(data: dict | list | None) -> list[dict]:
    if not isinstance(data, dict):
        return []
    payload = data.get("data", [])
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("tokenList", "tokens", "list", "items", "records", "result"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [x for x in rows if isinstance(x, dict)]
        return [payload]
    return []


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


def _pct_any(d: dict, *keys: str) -> float:
    return _float_any(d, *keys)


def _parse_price_info(d: dict) -> dict:
    return {
        "chain_id":          str(d.get("chainIndex", "")),
        "address":           _normalize_address(str(d.get("chainIndex", "")), d.get("tokenContractAddress") or ""),
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
        "volume_4h":         _float_any(d, "volume4H", "volume4h"),
        "volume_24h":        _float_any(d, "volume24H", "volume24h"),
        "tx_count_5m":       _int_any(d, "txs5M", "txs5m", "txCount5M"),
        "tx_count_1h":       _int_any(d, "txs1H", "txs1h", "txCount1H"),
        "tx_count_4h":       _int_any(d, "txs4H", "txs4h", "txCount4H"),
        "tx_count_24h":      _int_any(d, "txs24H", "txs24h", "txCount24H"),
        "circ_supply":       _float_any(d, "circSupply", "circulatingSupply"),
    }


async def search_tokens(
    session: aiohttp.ClientSession,
    keyword: str,
    chains: str = "1,56,501,137,42161,8453",
    limit: int = 20,
) -> list[dict]:
    """搜索链上 token（名称/符号/地址），OKX 文档最大返回 100 条。"""
    if not config_manager.settings.get("YAOBI_OKX_ENABLED", True):
        record_provider_skip("okx", "token/search", "YAOBI_OKX_ENABLED=false")
        return []
    keyword = (keyword or "").strip()
    if not keyword:
        record_provider_skip("okx", "token/search", "empty_keyword")
        return []
    params = {"chains": chains, "search": keyword, "limit": max(1, min(int(limit), 100))}
    path = f"/api/v6/dex/market/token/search?{urlencode(params)}"
    data = await _request_json(session, "GET", path, endpoint="token/search")
    return _parse_token_list(_data_list(data), source="okx_search")


async def get_hot_tokens(
    session: aiohttp.ClientSession,
    chain_index: str = "",
    limit: int = 50,
    ranking_type: str = "4",
    timeframe: str = "2",
) -> list[dict]:
    """读取 OKX 热门代币榜，作为独立候选来源。"""
    if not config_manager.settings.get("YAOBI_OKX_ENABLED", True):
        record_provider_skip("okx", "token/hot-token", "YAOBI_OKX_ENABLED=false")
        return []
    if not config_manager.settings.get("YAOBI_OKX_HOT_ENABLED", True):
        record_provider_skip("okx", "token/hot-token", "YAOBI_OKX_HOT_ENABLED=false")
        return []
    params = {
        "rankingType": ranking_type,
        "rankingTimeFrame": timeframe,
        "riskFilter": "true",
        "stableTokenFilter": "true",
        "limit": max(1, min(int(limit), 100)),
    }
    if chain_index:
        params["chainIndex"] = str(chain_index)
    path = f"/api/v6/dex/market/token/hot-token?{urlencode(params)}"
    data = await _request_json(session, "GET", path, endpoint="token/hot-token")
    return _parse_token_list(_data_list(data), source="okx_hot")


async def get_token_price_info_batch(
    session: aiohttp.ClientSession,
    items: list[dict],
) -> dict[str, dict]:
    """批量获取 price-info，返回 key=chain:address。官方支持最多 100 个地址。"""
    if not config_manager.settings.get("YAOBI_OKX_ENABLED", True):
        record_provider_skip("okx", "price-info", "YAOBI_OKX_ENABLED=false", items=len(items))
        return {}
    if not items:
        return {}

    limit = int(config_manager.settings.get("YAOBI_OKX_PRICE_BATCH_SIZE", 100))
    limit = max(1, min(limit, 100))
    results: dict[str, dict] = {}
    path = "/api/v6/dex/market/price-info"
    seen: set[str] = set()
    payload: list[dict] = []
    for item in items:
        chain = str(item.get("chainIndex") or item.get("chain_id") or "").strip()
        address = _normalize_address(chain, item.get("tokenContractAddress") or item.get("address") or "")
        if not chain or not address:
            continue
        key = f"{chain}:{address}"
        if key in seen:
            continue
        seen.add(key)
        payload.append({"chainIndex": chain, "tokenContractAddress": address})

    for i in range(0, len(payload), limit):
        chunk = payload[i:i + limit]
        body = _json_body(chunk)
        data = await _request_json(
            session,
            "POST",
            path,
            body=body,
            endpoint="price-info",
            expected_items=len(chunk),
        )
        for row in _data_list(data):
            parsed = _parse_price_info(row)
            key = f"{parsed.get('chain_id')}:{parsed.get('address')}"
            results[key] = parsed
    return results


async def get_token_price_info(
    session: aiohttp.ClientSession,
    chain_index: str,
    address: str,
) -> dict | None:
    """兼容旧调用：单 token price-info。"""
    data = await get_token_price_info_batch(
        session,
        [{"chainIndex": str(chain_index), "tokenContractAddress": address}],
    )
    return data.get(f"{chain_index}:{_normalize_address(str(chain_index), address)}")


async def get_token_advanced_info(
    session: aiohttp.ClientSession,
    chain_index: str,
    token_address: str,
) -> dict:
    """获取风险标签、Top10/开发者持仓、LP 销毁比例。"""
    if not config_manager.settings.get("YAOBI_OKX_ENABLED", True):
        record_provider_skip("okx", "token/advanced-info", "YAOBI_OKX_ENABLED=false")
        return {}
    params = {"chainIndex": str(chain_index), "tokenContractAddress": token_address}
    path = f"/api/v6/dex/market/token/advanced-info?{urlencode(params)}"
    data = await _request_json(session, "GET", path, endpoint="token/advanced-info")
    row = _first_data(data) if isinstance(data, dict) else {}
    if not row:
        return {}
    tags = row.get("tokenTags") or []
    return {
        "risk_control_level": _int_any(row, "riskControlLevel"),
        "token_tags": tags if isinstance(tags, list) else [],
        "top10_hold_pct": _pct_any(row, "top10HoldPercent"),
        "dev_hold_pct": _pct_any(row, "devHoldingPercent"),
        "bundle_hold_pct": _pct_any(row, "bundleHoldingPercent"),
        "suspicious_hold_pct": _pct_any(row, "suspiciousHoldingPercent"),
        "sniper_hold_pct": _pct_any(row, "sniperHoldingPercent"),
        "lp_burned_pct": _pct_any(row, "lpBurnedPercent"),
        "dev_rug_pull_count": _int_any(row, "devRugPullTokenCount"),
        "dev_create_count": _int_any(row, "devCreateTokenCount"),
        "create_time": row.get("createTime", ""),
    }


async def get_token_holders(
    session: aiohttp.ClientSession,
    chain_index: str,
    token_address: str,
    *,
    tag_filter: str = "",
    limit: int = 20,
) -> dict:
    """获取前持有人，默认最多 20 条，避免消耗过大。"""
    if not config_manager.settings.get("YAOBI_OKX_ENABLED", True):
        record_provider_skip("okx", "token/holder", "YAOBI_OKX_ENABLED=false")
        return {"holders": [], "top_hold_pct": 0.0}
    params = {
        "chainIndex": str(chain_index),
        "tokenContractAddress": token_address,
        "limit": max(1, min(int(limit), 100)),
    }
    if tag_filter:
        params["tagFilter"] = tag_filter
    path = f"/api/v6/dex/market/token/holder?{urlencode(params)}"
    data = await _request_json(session, "GET", path, endpoint="token/holder")
    holders = _data_list(data)
    top_hold = sum(_float_any(h, "holdPercent") for h in holders[:10])
    return {
        "holders": holders,
        "top_hold_pct": round(top_hold, 4),
        "smart_money_count": len(holders) if tag_filter == "3" else 0,
    }


async def get_token_trades(
    session: aiohttp.ClientSession,
    chain_index: str,
    token_address: str,
    limit: int = 100,
) -> dict:
    """获取近期成交记录，返回大单(>$5K)占比和买卖方向。"""
    if not config_manager.settings.get("YAOBI_OKX_ENABLED", True):
        record_provider_skip("okx", "trades", "YAOBI_OKX_ENABLED=false")
        return {"large_trade_pct": 0.0, "total_usd": 0.0, "trade_count": 0}
    params = {
        "chainIndex": str(chain_index),
        "tokenContractAddress": token_address,
        "limit": max(1, min(int(limit), 500)),
    }
    path = f"/api/v6/dex/market/trades?{urlencode(params)}"
    data = await _request_json(session, "GET", path, endpoint="trades")
    trades = _data_list(data)
    if not trades:
        return {"large_trade_pct": 0.0, "total_usd": 0.0, "trade_count": 0}

    total_usd = sum(_float_any(t, "volume", "usdValue", "volumeUsd", "amountUsd") for t in trades)
    large_usd = sum(
        _float_any(t, "volume", "usdValue", "volumeUsd", "amountUsd")
        for t in trades
        if _float_any(t, "volume", "usdValue", "volumeUsd", "amountUsd") >= 5000
    )
    buy_usd = sum(_float_any(t, "volume") for t in trades if str(t.get("type", "")).lower() == "buy")
    sell_usd = sum(_float_any(t, "volume") for t in trades if str(t.get("type", "")).lower() == "sell")
    large_pct = large_usd / total_usd if total_usd > 0 else 0.0
    buy_ratio = buy_usd / (buy_usd + sell_usd) if (buy_usd + sell_usd) > 0 else 0.0
    return {
        "large_trade_pct": round(large_pct, 4),
        "total_usd": round(total_usd, 2),
        "trade_count": len(trades),
        "buy_usd": round(buy_usd, 2),
        "sell_usd": round(sell_usd, 2),
        "buy_ratio": round(buy_ratio, 4),
    }


async def diagnose(session: aiohttp.ClientSession) -> dict:
    status = credentials_status()
    if not status["enabled"]:
        return {**status, "ok": False, "reason": "missing_okx_credentials"}

    search, hot = await asyncio.gather(
        search_tokens(session, "weth", chains="1,10", limit=3),
        get_hot_tokens(session, chain_index="501", limit=3),
        return_exceptions=True,
    )
    price = await get_token_price_info(
        session,
        "1",
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    )
    search_count = len(search) if isinstance(search, list) else 0
    hot_count = len(hot) if isinstance(hot, list) else 0
    return {
        **credentials_status(),
        "ok": bool(search_count or hot_count or price),
        "search_count": search_count,
        "hot_count": hot_count,
        "price_info_ok": bool(price),
    }


def _parse_token_list(items: list, source: str = "okx_search") -> list[dict]:
    results = []
    for t in items:
        try:
            chain_id = str(t.get("chainIndex", ""))
            chain = CHAIN_NAMES.get(chain_id, chain_id)
            tag_list = t.get("tagList", [])
            if isinstance(tag_list, dict):
                tag_list = [k for k, v in tag_list.items() if v]
            elif not isinstance(tag_list, list):
                tag_list = []
            results.append({
                "symbol":       t.get("tokenSymbol", ""),
                "name":         t.get("tokenName", t.get("tokenSymbol", "")),
                "chain":        chain,
                "chain_id":     chain_id,
                "address":      _normalize_address(chain_id, t.get("tokenContractAddress") or ""),
                "price_usd":    _float_any(t, "price"),
                "price_change_1h": _float_any(t, "priceChange1H", "priceChange1h"),
                "price_change_4h": _float_any(t, "priceChange4H", "priceChange4h"),
                "price_change_24h": _float_any(t, "change", "change24H", "priceChange24H"),
                "holder_count": _int_any(t, "holders", "holderCount"),
                "liquidity":    _float_any(t, "liquidity"),
                "market_cap":   _float_any(t, "marketCap", "marketCapUsd"),
                "volume_24h":   _float_any(t, "volume", "volume24H", "volume24h"),
                "logo_url":     t.get("tokenLogoUrl", ""),
                "tags":         tag_list,
                "links":        {"okx": t.get("explorerUrl", "")} if t.get("explorerUrl") else {},
                "source":       source,
            })
        except Exception as e:
            logger.debug("OKX token parse 异常: %s", e)
    return results
