"""Optional AI review gateway for the yaobi opportunity queue.

The gateway sends only a compact Top-N payload. It does not include prior chat
history, raw logs, or secrets, and it never returns a direct order instruction.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from config import (
    ANTHROPIC_API_KEY,
    DATA_DIR,
    DEEPSEEK_API_KEY,
    GEMINI_API_KEY,
    MINIMAX_API_KEY,
    OPENAI_API_KEY,
    ai_credentials_status,
    config_manager,
)
from scanner.knowledge_base import relevant_lessons, relevant_lesson_stats
from scanner.provider_metrics import record_provider_call, record_provider_skip

logger = logging.getLogger(__name__)

# ── SSL 上下文（修复 Gemini 在 Windows 上偶发 SSLV3_ALERT_HANDSHAKE_FAILURE）─────
# 优先用 certifi 的 CA bundle，更新更频繁；不可用时回退到系统默认。
_SSL_CONTEXT: ssl.SSLContext | None = None
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - certifi 未安装时的兜底
    try:
        _SSL_CONTEXT = ssl.create_default_context()
    except Exception:
        _SSL_CONTEXT = None

# ── 单 provider 失败 backoff（避免 SSL/网络问题反复刷日志、浪费配额）─────────
# {provider: (backoff_until_ts, consecutive_failures)}
_PROVIDER_BACKOFF: dict[str, tuple[float, int]] = {}
_BACKOFF_BASE_SEC = 60.0    # 第一次失败后冷却 60s
_BACKOFF_MAX_SEC = 1800.0   # 封顶 30min
_BACKOFF_RESET_OK = True    # 成功一次即清空

_NETWORK_ERROR_KEYS = (
    "ssl",
    "handshake",
    "clientconnector",
    "connectionerror",
    "timeout",
    "cannot connect",
    "name resolution",
    "getaddrinfo",
)


def _provider_in_backoff(provider: str) -> tuple[bool, float]:
    """返回 (是否处于 backoff, 距离解除秒数)。"""
    info = _PROVIDER_BACKOFF.get(provider)
    if not info:
        return False, 0.0
    until, _ = info
    now = time.time()
    if now >= until:
        return False, 0.0
    return True, until - now


def _record_provider_failure(provider: str, exc: Exception) -> None:
    """对网络/SSL 类失败按指数退避；其他错误只记 1 次（避免误判）。"""
    msg = f"{type(exc).__name__}: {exc}".lower()
    is_network = any(k in msg for k in _NETWORK_ERROR_KEYS)
    info = _PROVIDER_BACKOFF.get(provider) or (0.0, 0)
    fails = info[1] + (1 if is_network else 0)
    if not is_network:
        # 非网络问题（如 HTTP 4xx/5xx）不触发长 backoff，只记 1 分钟避免连续 hammer
        until = time.time() + 60
    else:
        backoff = min(_BACKOFF_BASE_SEC * (2 ** max(0, fails - 1)), _BACKOFF_MAX_SEC)
        until = time.time() + backoff
    _PROVIDER_BACKOFF[provider] = (until, fails)
    if is_network:
        logger.warning(
            "🌐 [%s] AI 调用网络/SSL 失败#%d (%s)，进入 %.0fs backoff",
            provider, fails, type(exc).__name__, until - time.time(),
        )


def _clear_provider_backoff(provider: str) -> None:
    if _BACKOFF_RESET_OK:
        _PROVIDER_BACKOFF.pop(provider, None)

_ROOT = os.path.join(DATA_DIR, "ai_knowledge")
_CACHE_FILE = os.path.join(_ROOT, "ai_cache.json")
_USAGE_FILE = os.path.join(_ROOT, "ai_usage.json")
_ENDPOINT = "opportunity_review"
_LATEST_SUCCESS_KEY = "_latest_success"
_GEMINI_OPPORTUNITY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {"type": "string"},
                    "permission": {"type": "string"},
                    "confidence": {"type": "integer"},
                    "market_stage": {"type": "string"},
                    "playbook_type": {"type": "string"},
                    "trade_permission": {"type": "string"},
                    "opportunity_score": {"type": "integer"},
                    "risk_score": {"type": "integer"},
                    "summary": {"type": "string"},
                    "reasons": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "required_confirmation": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["symbol", "action", "permission", "confidence"],
            },
        }
    },
    "required": ["opportunities"],
}


def _ensure_root() -> None:
    os.makedirs(_ROOT, exist_ok=True)


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
    _ensure_root()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _usage_snapshot() -> dict:
    usage = _load_json(_USAGE_FILE, {})
    today = _today_key()
    row = usage.get(today, {"calls": 0, "estimated_usd": 0.0, "tokens": 0})
    meta = usage.get("_meta", {})
    latest = usage.get(_LATEST_SUCCESS_KEY, {})
    return {
        "date": today,
        "calls": int(row.get("calls", 0) or 0),
        "estimated_usd": round(float(row.get("estimated_usd", 0.0) or 0.0), 4),
        "tokens": int(row.get("tokens", 0) or 0),
        "last_call_ts": float(meta.get("last_call_ts", 0) or 0),
        "last_success_ts": float(latest.get("ts", 0) or 0),
        "last_success_provider": str(latest.get("provider", "") or ""),
    }


def _record_usage(tokens: int, estimated_usd: float) -> None:
    usage = _load_json(_USAGE_FILE, {})
    today = _today_key()
    row = usage.setdefault(today, {"calls": 0, "estimated_usd": 0.0, "tokens": 0})
    row["calls"] = int(row.get("calls", 0) or 0) + 1
    row["estimated_usd"] = float(row.get("estimated_usd", 0.0) or 0.0) + float(estimated_usd)
    row["tokens"] = int(row.get("tokens", 0) or 0) + int(tokens)
    usage["_meta"] = {"last_call_ts": time.time()}
    _save_json(_USAGE_FILE, usage)


def _save_latest_success(provider: str, items: list[dict]) -> None:
    usage = _load_json(_USAGE_FILE, {})
    usage[_LATEST_SUCCESS_KEY] = {
        "ts": time.time(),
        "provider": provider,
        "items": items,
    }
    _save_json(_USAGE_FILE, usage)


def _load_latest_success(ttl_sec: float, symbols: set[str]) -> dict | None:
    usage = _load_json(_USAGE_FILE, {})
    latest = usage.get(_LATEST_SUCCESS_KEY)
    if not isinstance(latest, dict):
        return None
    ts = float(latest.get("ts", 0) or 0)
    if not ts or time.time() - ts > ttl_sec:
        return None
    rows = latest.get("items")
    if not isinstance(rows, list):
        return None
    filtered = []
    for row in rows:
        symbol = _symbol_key(row.get("symbol", ""))
        if not symbol or symbol not in symbols:
            continue
        filtered.append(dict(row, cached=True, reused=True, provider=latest.get("provider", "")))
    if not filtered:
        return None
    return {
        "provider": str(latest.get("provider", "") or ""),
        "items": filtered,
        "ts": ts,
    }


def provider_status() -> dict:
    cfg = config_manager.settings
    usage = _usage_snapshot()
    cap = float(cfg.get("YAOBI_AI_DAILY_USD_CAP", 3.0) or 0.0)
    return {
        "enabled": bool(cfg.get("YAOBI_AI_ENABLED", False)),
        "credentials": ai_credentials_status(),
        "provider_priority": cfg.get("YAOBI_AI_PROVIDER_PRIORITY", ""),
        "models": {
            "openai": cfg.get("YAOBI_AI_MODEL_OPENAI", "gpt-4o-mini"),
            "gemini": cfg.get("YAOBI_AI_MODEL_GEMINI", "gemini-2.5-flash"),
            "anthropic": cfg.get("YAOBI_AI_MODEL_ANTHROPIC", "claude-3-5-haiku-latest"),
            "deepseek": cfg.get("YAOBI_AI_MODEL_DEEPSEEK", "deepseek-v4-flash"),
            "minimax": cfg.get("YAOBI_AI_MODEL_MINIMAX", "minimax-m1"),
        },
        "cache_ttl_min": cfg.get("YAOBI_AI_CACHE_TTL_MINUTES", 30),
        "min_interval_min": cfg.get("YAOBI_AI_MIN_INTERVAL_MINUTES", 15),
        "daily_cap_usd": cap,
        "usage": usage,
        "budget_left_usd": round(max(0.0, cap - usage["estimated_usd"]), 4) if cap > 0 else None,
    }


def _compact_candidate(c: Any) -> dict:
    symbol = getattr(c, "symbol", "") or ""
    rule_action = _normalize_action(str(getattr(c, "opportunity_action", "") or ""))
    rule_bias = "NEUTRAL"
    if rule_action.startswith("WATCH_LONG"):
        rule_bias = "LONG"
    elif rule_action.startswith("WATCH_SHORT"):
        rule_bias = "SHORT"
    return {
        "symbol": symbol,
        "score": getattr(c, "score", 0),
        "anomaly": getattr(c, "anomaly_score", 0),
        "decision": getattr(c, "decision_action", ""),
        "rule_action": rule_action or "OBSERVE",
        "rule_bias": rule_bias,
        "rule_score": getattr(c, "opportunity_score", 0),
        "price_1h": round(float(getattr(c, "price_change_1h", 0) or 0), 3),
        "price_4h": round(float(getattr(c, "price_change_4h", 0) or 0), 3),
        "price_24h": round(float(getattr(c, "price_change_24h", 0) or 0), 3),
        "oi_5m": round(float(getattr(c, "oi_change_5m_pct", 0) or 0), 3),
        "oi_15m": round(float(getattr(c, "oi_change_15m_pct", 0) or 0), 3),
        "oi_24h": round(float(getattr(c, "oi_change_24h_pct", 0) or 0), 3),
        "volume_5m_ratio": round(float(getattr(c, "volume_5m_ratio", 0) or 0), 3),
        "volume_ratio": round(float(getattr(c, "volume_ratio", 0) or 0), 3),
        "taker_buy_5m": round(float(getattr(c, "taker_buy_ratio_5m", 0.5) or 0.5), 3),
        "funding_pct": round(float(getattr(c, "funding_rate_pct", 0) or 0), 5),
        "long_account_pct": round(float(getattr(c, "long_account_pct", 0) or 0), 2),
        "retail_short_pct": round(float(getattr(c, "retail_short_pct", 0) or 0), 2),
        "top_trader_long_pct": round(float(getattr(c, "top_trader_long_pct", 0) or 0), 2),
        "liq_5m_usd": round(float(getattr(c, "liquidation_5m_usd", 0) or 0), 2),
        "liq_15m_usd": round(float(getattr(c, "liquidation_15m_usd", 0) or 0), 2),
        "contract_activity": getattr(c, "contract_activity_score", 0),
        "market_stage": getattr(c, "market_stage", ""),
        "playbook_type": getattr(c, "playbook_type", ""),
        "trade_permission": getattr(c, "trade_permission", ""),
        "risk_score": getattr(c, "risk_score", 0),
        "chip_score": getattr(c, "chip_score", 0),
        "control_score": getattr(c, "control_score", 0),
        "distribution_score": getattr(c, "distribution_score", 0),
        "early_wallet_layout": bool(getattr(c, "early_wallet_layout", False)),
        "case_similarity": getattr(c, "case_similarity", {}) or {},
        "okx_buy_ratio": round(float(getattr(c, "okx_buy_ratio", 0) or 0), 3),
        "okx_large_trade_pct": round(float(getattr(c, "okx_large_trade_pct", 0) or 0), 3),
        "okx_risk_level": getattr(c, "okx_risk_level", 0),
        "sentiment": getattr(c, "sentiment_label", ""),
        "surf_news_sentiment": getattr(c, "surf_news_sentiment", ""),
        "surf_news_titles": list(getattr(c, "surf_news_titles", []) or [])[:2],
        "tags": list(getattr(c, "anomaly_tags", []) or [])[:6],
        "signals": list(getattr(c, "signals", []) or [])[:8],
        "intelligence_reasons": list(getattr(c, "intelligence_reasons", []) or [])[:4],
        "intelligence_risks": list(getattr(c, "intelligence_risks", []) or [])[:4],
        "lesson_stats": relevant_lesson_stats(symbol, limit=24),
        "lessons": relevant_lessons(symbol, limit=3),
    }


def _extract_json_payload(text: str):
    raw = str(text or "").strip()
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") or candidate.startswith("["):
                raw = candidate
                break
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start_obj = raw.find("{")
    start_arr = raw.find("[")
    starts = [x for x in (start_obj, start_arr) if x >= 0]
    if not starts:
        raise ValueError("no_json_found")
    start = min(starts)
    end = max(raw.rfind("}"), raw.rfind("]"))
    if end <= start:
        raise ValueError("json_bounds_invalid")
    return json.loads(raw[start:end + 1])


def _is_valid_json_text(text: str) -> bool:
    try:
        _extract_json_payload(text)
        return True
    except Exception:
        return False


def _symbol_key(symbol: str) -> str:
    return str(symbol or "").upper().replace("USDT", "")


def _normalize_action(action: str) -> str:
    raw = str(action or "").upper()
    aliases = {
        "WATCH_LONG": "WATCH_LONG_CONTINUATION",
        "WATCH_SHORT": "WATCH_SHORT_CONTINUATION",
    }
    return aliases.get(raw, raw)


def _bounded_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(100, int(float(value if value is not None else default))))
    except (TypeError, ValueError):
        return default


def _normalize_ai_items(payload) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get("opportunities") or payload.get("items") or payload.get("data") or []
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    result: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = _symbol_key(row.get("symbol", ""))
        if not symbol:
            continue
        action = _normalize_action(str(row.get("action", "OBSERVE")).upper())
        permission = str(row.get("permission", "OBSERVE")).upper()
        trade_permission = str(row.get("trade_permission", row.get("permission", "OBSERVE")) or "OBSERVE").upper()
        result.append({
            "symbol": symbol,
            "action": action if action in {
                "WATCH_LONG_CONTINUATION",
                "WATCH_SHORT_CONTINUATION",
                "WATCH_LONG_FADE",
                "WATCH_SHORT_FADE",
                "OBSERVE",
                "BLOCK",
            } else "OBSERVE",
            "permission": permission if permission in {"ALLOW_IF_1M_SIGNAL", "OBSERVE", "BLOCK"} else "OBSERVE",
            "confidence": _bounded_int(row.get("confidence", 0)),
            "market_stage": str(row.get("market_stage", "") or "")[:80],
            "playbook_type": str(row.get("playbook_type", "") or "")[:80],
            "trade_permission": trade_permission if trade_permission in {"AMBUSH_WATCH", "WATCH_CONFIRMATION", "OBSERVE", "BLOCK"} else "OBSERVE",
            "opportunity_score": _bounded_int(row.get("opportunity_score", 0)),
            "risk_score": _bounded_int(row.get("risk_score", 0)),
            "summary": str(row.get("summary", ""))[:240],
            "reasons": list(row.get("reasons", []) or [])[:5],
            "risks": list(row.get("risks", []) or [])[:5],
            "required_confirmation": list(row.get("required_confirmation", []) or [])[:5],
        })
    return result


def _cache_key(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str, ttl_sec: float) -> list[dict] | None:
    cache = _load_json(_CACHE_FILE, {})
    row = cache.get(key)
    if not row:
        return None
    if time.time() - float(row.get("ts", 0) or 0) > ttl_sec:
        return None
    items = row.get("items")
    return items if isinstance(items, list) else None


def _cache_put(key: str, items: list[dict]) -> None:
    cache = _load_json(_CACHE_FILE, {})
    cache[key] = {"ts": time.time(), "items": items}
    if len(cache) > 80:
        old_keys = sorted(cache, key=lambda k: cache[k].get("ts", 0))[:-60]
        for old_key in old_keys:
            cache.pop(old_key, None)
    _save_json(_CACHE_FILE, cache)


async def analyze_opportunities(session: aiohttp.ClientSession, candidates: list[Any]) -> dict:
    cfg = config_manager.settings
    status = provider_status()
    if not cfg.get("YAOBI_AI_ENABLED", False):
        record_provider_skip("ai", _ENDPOINT, "yaobi_ai_disabled", items=len(candidates))
        return {"items": [], "status": status | {"last_reason": "disabled"}}
    if not ai_credentials_status().get("enabled"):
        record_provider_skip("ai", _ENDPOINT, "missing_ai_api_key", items=len(candidates))
        return {"items": [], "status": status | {"last_reason": "missing_key"}}

    max_symbols = int(cfg.get("YAOBI_AI_MAX_SYMBOLS_PER_RUN", 12) or 12)
    rows = [_compact_candidate(c) for c in candidates[:max_symbols]]
    if not rows:
        return {"items": [], "status": status | {"last_reason": "empty"}}

    payload = {
        "task": "short_term_crypto_opportunity_review",
        "rule": (
            "Do not place orders or answer buy/sell. Decide the 15-60 minute market stage and playbook only. "
            "OI/CVD/taker flow are confirmation signals, not standalone entry triggers. "
            "Do not wait for or ask for 1m candles; the local bot handles exact 1m entry timing."
        ),
        "output_schema": {
            "opportunities": [{
                "symbol": "BASE",
                "market_stage": "accumulation_before_oi|real_breakout|bull_trap|mm_control|distribution|dead|neutral",
                "playbook_type": "ambush_watch|oi_confirmation|avoid_chasing_oi|thin_book_control|avoid_distribution|block_high_risk|observe",
                "trade_permission": "AMBUSH_WATCH|WATCH_CONFIRMATION|OBSERVE|BLOCK",
                "opportunity_score": 0,
                "risk_score": 0,
                "action": "WATCH_LONG_CONTINUATION|WATCH_SHORT_CONTINUATION|WATCH_LONG_FADE|WATCH_SHORT_FADE|OBSERVE|BLOCK",
                "permission": "ALLOW_IF_1M_SIGNAL|OBSERVE|BLOCK",
                "confidence": 0,
                "summary": "short reason",
                "reasons": ["..."],
                "risks": ["..."],
                "required_confirmation": ["1m breakout/squeeze", "taker/OI still agrees"],
            }]
        },
        "candidates": rows,
    }
    raw_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    max_input_tokens = int(cfg.get("YAOBI_AI_MAX_INPUT_TOKENS", 8000) or 8000)
    if _estimate_tokens(raw_payload) > max_input_tokens:
        while rows and _estimate_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) > max_input_tokens:
            rows.pop()
            payload["candidates"] = rows
        raw_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if not rows:
        record_provider_skip("ai", _ENDPOINT, "payload_too_large")
        return {"items": [], "status": status | {"last_reason": "payload_too_large"}}

    cache_key = _cache_key(payload)
    cached = _cache_get(cache_key, float(cfg.get("YAOBI_AI_CACHE_TTL_MINUTES", 30) or 30) * 60)
    if cached is not None:
        record_provider_skip("ai", _ENDPOINT, "cache_hit", items=len(rows))
        return {"items": [dict(x, cached=True) for x in cached], "status": status | {"last_reason": "cache_hit"}}

    usage = _usage_snapshot()
    min_interval = float(cfg.get("YAOBI_AI_MIN_INTERVAL_MINUTES", 15) or 15) * 60
    cache_ttl = float(cfg.get("YAOBI_AI_CACHE_TTL_MINUTES", 30) or 30) * 60
    if usage["last_call_ts"] and time.time() - usage["last_call_ts"] < min_interval:
        record_provider_skip("ai", _ENDPOINT, "min_interval", items=len(rows))
        reused = _load_latest_success(cache_ttl, {_symbol_key(x["symbol"]) for x in rows})
        if reused is not None:
            return {
                "items": reused["items"],
                "status": status | {"last_reason": "min_interval_reuse", "last_provider": reused["provider"]},
            }
        return {"items": [], "status": status | {"last_reason": "min_interval"}}
    daily_cap = float(cfg.get("YAOBI_AI_DAILY_USD_CAP", 3.0) or 0.0)
    if daily_cap > 0 and usage["estimated_usd"] >= daily_cap:
        record_provider_skip("ai", _ENDPOINT, "daily_budget_exhausted", items=len(rows))
        return {"items": [], "status": status | {"last_reason": "budget_exhausted"}}

    providers = [
        p.strip().lower()
        for p in str(cfg.get("YAOBI_AI_PROVIDER_PRIORITY", "gemini")).split(",")
        if p.strip()
    ]
    system_prompt = (
        "You are a crypto market-structure analyst for a short-term 1-minute scalp bot. "
        "Use only the provided compact 5m/15m/24h flow, OI, funding, liquidation, sentiment, OKX holder-structure and lesson data. "
        "Return strict JSON only. "
        "Your job is to classify the market stage and choose the playbook for the next 15-60 minutes, not the exact entry tick. "
        "Do not output buy/sell recommendations. "
        "OI/CVD/taker flow are confirmation signals; do not treat OI expansion alone as a tradable entry. "
        "Use AMBUSH_WATCH when on-chain accumulation appears early but OI has not confirmed; keep permission OBSERVE in that case. "
        "Do not downgrade a candidate to OBSERVE just because no 1m signal is supplied; 1m execution is handled locally after your permission. "
        "Use ALLOW_IF_1M_SIGNAL when the higher-timeframe playbook is tradable but still needs local 1m confirmation. "
        "WATCH_LONG_CONTINUATION and WATCH_SHORT_CONTINUATION mean trend-following continuation or pullback-entry playbooks. "
        "WATCH_LONG_FADE and WATCH_SHORT_FADE mean short holding-period counter-regime reversal scalps, only when the move looks stretched and local flow is fading; these should not be breakout-chasing permissions."
    )
    max_output = int(cfg.get("YAOBI_AI_MAX_OUTPUT_TOKENS", 1200) or 1200)
    last_error = ""
    for provider in providers:
        # backoff 检查：上次失败仍在冷却期内则跳过，避免反复 hammer SSL 失败的 endpoint
        in_backoff, remain = _provider_in_backoff(provider)
        if in_backoff:
            record_provider_skip(provider, _ENDPOINT, f"backoff_{int(remain)}s", items=len(rows))
            continue
        try:
            if provider == "openai" and OPENAI_API_KEY:
                text, token_count = await _call_openai(session, system_prompt, raw_payload, max_output)
            elif provider == "gemini" and GEMINI_API_KEY:
                text, token_count = await _call_gemini(system_prompt, raw_payload, max_output)
            elif provider == "anthropic" and ANTHROPIC_API_KEY:
                text, token_count = await _call_anthropic(session, system_prompt, raw_payload, max_output)
            elif provider == "deepseek" and (os.getenv("DEEPSEEK_API_KEY") or DEEPSEEK_API_KEY):
                text, token_count = await _call_deepseek(session, system_prompt, raw_payload, max_output)
            elif provider == "minimax" and (os.getenv("MINIMAX_API_KEY") or MINIMAX_API_KEY):
                text, token_count = await _call_minimax(session, system_prompt, raw_payload, max_output)
            else:
                record_provider_skip(provider or "ai", _ENDPOINT, "provider_key_missing", items=len(rows))
                continue
            items = _normalize_ai_items(_extract_json_payload(text))
            output_limit = int(cfg.get("YAOBI_AI_TOP_OUTPUT", 6) or 6)
            items = items[:output_limit]
            _cache_put(cache_key, items)
            estimated = max(0.001, token_count / 1_000_000 * 2.0)
            _record_usage(token_count, estimated)
            _save_latest_success(provider, items)
            _clear_provider_backoff(provider)
            record_provider_call(provider, _ENDPOINT, True, status="200", items=len(rows))
            return {
                "items": [dict(x, provider=provider, cached=False) for x in items],
                "status": provider_status() | {"last_reason": "ok", "last_provider": provider},
            }
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            _record_provider_failure(provider, e)
            record_provider_call(provider or "ai", _ENDPOINT, False, status="exception", error=last_error, items=len(rows))
            continue
    return {"items": [], "status": provider_status() | {"last_reason": "all_failed", "last_error": last_error}}


async def _call_openai(session: aiohttp.ClientSession, system_prompt: str, payload: str, max_output: int) -> tuple[str, int]:
    model = config_manager.settings.get("YAOBI_AI_MODEL_OPENAI", "gpt-4o-mini")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": payload},
        ],
        "temperature": 0.1,
        "max_tokens": max_output,
    }
    async with session.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json(content_type=None)
        if resp.status >= 300:
            raise RuntimeError(f"openai_http_{resp.status}: {str(data)[:160]}")
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or (_estimate_tokens(payload) + _estimate_tokens(text)))
        return text, tokens


def _gemini_model_candidates() -> list[str]:
    # gemini-3.1-pro-preview 实测 89% 失败 (finish=MAX_TOKENS, 截断 JSON)，移除。
    # 3-flash-preview 也类似不稳定。只保留 2.5 系列稳定模型。
    primary = str(config_manager.settings.get("YAOBI_AI_MODEL_GEMINI", "gemini-2.5-flash") or "").strip()
    fallbacks = [
        primary,
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
    ]
    result: list[str] = []
    seen: set[str] = set()
    for model in fallbacks:
        if model and model not in seen:
            result.append(model)
            seen.add(model)
    return result


def _gemini_request_body(system_prompt: str, payload: str, max_output: int) -> dict:
    return {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": payload}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": max_output,
            "responseMimeType": "application/json",
            "responseJsonSchema": _GEMINI_OPPORTUNITY_JSON_SCHEMA,
        },
    }


async def _call_gemini(system_prompt: str, payload: str, max_output: int) -> tuple[str, int]:
    """Gemini 调用使用独立 session + certifi SSL 上下文。

    背景：generativelanguage.googleapis.com 在某些 Windows / 代理环境下，
    用 Python 默认 CA bundle 时会触发 SSLV3_ALERT_HANDSHAKE_FAILURE。
    显式指定 certifi CA + 启用 trust_env (HTTPS_PROXY 等代理) 可显著降低发生率。
    """
    api_key = os.getenv("GEMINI_API_KEY", GEMINI_API_KEY)
    # 低于 2k 时 Gemini 很容易 finish=MAX_TOKENS 并截断 JSON；调用层保守抬高
    # AI 终审输出上限，不改变 helper 的语义，也避免截断型失败继续堆高。
    max_output = max(int(max_output or 0), 2048)
    last_error = ""
    connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT is not None else None
    async with aiohttp.ClientSession(
        connector=connector,
        trust_env=True,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as gem_session:
        for model in _gemini_model_candidates():
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            body = _gemini_request_body(system_prompt, payload, max_output)
            try:
                for attempt in range(2):
                    async with gem_session.post(
                        url,
                        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                        json=body,
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status == 404:
                            last_error = f"gemini_http_404[{model}]: {str(data)[:160]}"
                            break
                        if resp.status in {429, 500, 502, 503, 504} and attempt == 0:
                            last_error = f"gemini_http_{resp.status}[{model}]: transient_retry"
                            await asyncio.sleep(1.0)
                            continue
                        if resp.status >= 300:
                            raise RuntimeError(f"gemini_http_{resp.status}[{model}]: {str(data)[:160]}")
                        text, finish_info = _parse_gemini_response(data, model)
                        if text is None:
                            last_error = finish_info  # 已经是可读字符串
                            break
                        if not _is_valid_json_text(text):
                            excerpt = str(text or "").replace("\n", " ")[:180]
                            last_error = f"gemini_non_json[{model}]: {finish_info} | {excerpt or 'empty_response'}"
                            break
                        usage = data.get("usageMetadata") or {}
                        tokens = int(usage.get("totalTokenCount") or (_estimate_tokens(payload) + _estimate_tokens(text)))
                        return text, tokens
            except (aiohttp.ClientConnectorSSLError, aiohttp.ClientConnectorError, ssl.SSLError) as exc:
                # 网络 / SSL 类失败：往上抛由 analyze_opportunities 触发 backoff
                raise RuntimeError(f"gemini_network[{model}]: {type(exc).__name__}: {exc}")
    raise RuntimeError(last_error or "gemini_model_unavailable")


def _parse_gemini_response(data: dict, model: str) -> tuple[str | None, str]:
    """从 Gemini generateContent 响应中健壮地抽出文本。

    返回 (text, info)：
      - 成功：(text, finishReason 描述)
      - 失败：(None, 可读的失败原因字符串，包含 finishReason / blockReason / 摘要)

    应对的真实 case（线上日志触发过）：
      - data["candidates"] 为空（被 promptFeedback.blockReason 拦下）
      - candidates[0].content 字段缺失
      - candidates[0].content.parts 字段缺失或为空（finishReason=MAX_TOKENS/SAFETY/RECITATION）
      - parts 里 text 为空字符串
    """
    prompt_feedback = data.get("promptFeedback") or {}
    block_reason = str(prompt_feedback.get("blockReason", "") or "")
    candidates_list = data.get("candidates") or []
    if not candidates_list:
        return None, f"gemini_no_candidates[{model}]: prompt_block={block_reason or '-'}"
    cand = candidates_list[0] if isinstance(candidates_list[0], dict) else {}
    finish = str(cand.get("finishReason", "") or "")
    content = cand.get("content") if isinstance(cand.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    if not parts:
        # 安全过滤 / 配额截断 / 模型直接放弃，都会落到这里
        return None, f"gemini_empty_parts[{model}]: finish={finish or '-'} block={block_reason or '-'}"
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        return None, f"gemini_blank_text[{model}]: finish={finish or '-'} parts={len(parts)}"
    # 即使 finish=MAX_TOKENS 文本仍可能是合法 JSON 的前缀，留给上层 _is_valid_json_text 判断
    return text, f"finish={finish or 'STOP'}"


async def _call_anthropic(session: aiohttp.ClientSession, system_prompt: str, payload: str, max_output: int) -> tuple[str, int]:
    model = config_manager.settings.get("YAOBI_AI_MODEL_ANTHROPIC", "claude-3-5-haiku-latest")
    body = {
        "model": model,
        "max_tokens": max_output,
        "temperature": 0.1,
        "system": system_prompt,
        "messages": [{"role": "user", "content": payload}],
    }
    async with session.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json(content_type=None)
        if resp.status >= 300:
            raise RuntimeError(f"anthropic_http_{resp.status}: {str(data)[:160]}")
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage") or {}
        tokens = int((usage.get("input_tokens") or _estimate_tokens(payload)) + (usage.get("output_tokens") or _estimate_tokens(text)))
        return text, tokens


async def _call_deepseek(session: aiohttp.ClientSession, system_prompt: str, payload: str, max_output: int) -> tuple[str, int]:
    api_key = os.getenv("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
    last_error = ""
    for model, thinking_enabled in _deepseek_model_candidates():
        body = _deepseek_request_body(system_prompt, payload, max_output, model=model, thinking_enabled=thinking_enabled)
        for attempt in range(2):
            async with session.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status in {429, 500, 502, 503, 504} and attempt == 0:
                    last_error = f"deepseek_http_{resp.status}[{model}]: transient_retry"
                    await asyncio.sleep(1.0)
                    continue
                if resp.status >= 300:
                    raise RuntimeError(f"deepseek_http_{resp.status}[{model}]: {str(data)[:160]}")

                text, finish_info = _parse_deepseek_response(data, model)
                if text is None:
                    last_error = finish_info
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    break
                if not _is_valid_json_text(text):
                    excerpt = str(text or "").replace("\n", " ")[:180]
                    last_error = f"deepseek_non_json[{model}]: {finish_info} | {excerpt or 'empty_response'}"
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    break
                usage = data.get("usage") or {}
                tokens = int(usage.get("total_tokens") or (_estimate_tokens(payload) + _estimate_tokens(text)))
                return text, tokens
    raise RuntimeError(last_error or "deepseek_model_unavailable")


async def _call_minimax(session: aiohttp.ClientSession, system_prompt: str, payload: str, max_output: int) -> tuple[str, int]:
    """调用 MiniMax M2.7（OpenAI 兼容接口）。"""
    api_key = os.getenv("MINIMAX_API_KEY") or MINIMAX_API_KEY
    model = str(config_manager.settings.get("YAOBI_AI_MODEL_MINIMAX", "MiniMax-M2.7") or "MiniMax-M2.7")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": payload},
        ],
        "temperature": 0.1,
        "max_tokens": max_output,
        "response_format": {"type": "json_object"},
    }
    async with session.post(
        "https://api.minimax.chat/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json(content_type=None)
        if resp.status >= 300:
            raise RuntimeError(f"minimax_http_{resp.status}[{model}]: {str(data)[:160]}")
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or (_estimate_tokens(payload) + _estimate_tokens(text)))
        return text, tokens


def _deepseek_model_candidates() -> list[tuple[str, bool]]:
    primary_raw = str(config_manager.settings.get("YAOBI_AI_MODEL_DEEPSEEK", "deepseek-v4-flash") or "").strip()
    aliases = {
        # Official docs keep these aliases temporarily, but the API now advertises
        # deepseek-v4-flash/pro. Normalize so new configs avoid deprecated IDs.
        "deepseek-chat": ("deepseek-v4-flash", False),
        "deepseek-reasoner": ("deepseek-v4-flash", True),
    }
    primary = aliases.get(primary_raw, (primary_raw or "deepseek-v4-flash", False))
    fallbacks = [
        primary,
        ("deepseek-v4-flash", False),
        ("deepseek-v4-pro", False),
    ]
    result: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()
    for model, thinking_enabled in fallbacks:
        key = (model, bool(thinking_enabled))
        if model and key not in seen:
            result.append(key)
            seen.add(key)
    return result


def _deepseek_request_body(
    system_prompt: str,
    payload: str,
    max_output: int,
    *,
    model: str,
    thinking_enabled: bool = False,
) -> dict:
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    system_prompt
                    + " Return a single valid JSON object only, with key \"opportunities\". "
                    + "Do not use markdown."
                ),
            },
            {"role": "user", "content": payload},
        ],
        "max_tokens": max(max_output, 2048),
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
    }
    if thinking_enabled:
        body["reasoning_effort"] = "high"
    else:
        body["temperature"] = 0.1
    return body


def _parse_deepseek_response(data: dict, model: str) -> tuple[str | None, str]:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None, f"deepseek_no_choices[{model}]"
    choice = choices[0]
    finish = str(choice.get("finish_reason", "") or "")
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = str(message.get("content", "") or "")
    if not text.strip():
        return None, f"deepseek_blank_content[{model}]: finish={finish or '-'}"
    if finish in {"content_filter", "insufficient_system_resource"}:
        return None, f"deepseek_finish_{finish}[{model}]"
    return text, f"finish={finish or 'stop'}"
async def call_single_coin_judge(payload: dict, prompt: str, *, timeout: int | None = None) -> dict:
    """单币判单路由：复用现有 provider/fallback/cache/metrics。"""
    cfg = config_manager.settings
    providers = [
        p.strip().lower()
        for p in str(cfg.get("YAOBI_AI_PROVIDER_PRIORITY", "gemini")).split(",")
        if p.strip()
    ]
    max_output = int(cfg.get("YAOBI_AI_MAX_OUTPUT_TOKENS", 1200) or 1200)
    timeout_sec = timeout or 20
    system_prompt = prompt
    try:
        raw_payload = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        raw_payload = str(payload)
    import aiohttp
    async with aiohttp.ClientSession(trust_env=True) as session:
        for provider in providers:
            in_backoff, remain = _provider_in_backoff(provider)
            if in_backoff:
                record_provider_skip(provider, "single_coin_judge", f"backoff_{int(remain)}s")
                continue
            try:
                text, token_count = "", 0
                if provider == "openai" and OPENAI_API_KEY:
                    text, token_count = await _call_openai(session, system_prompt, raw_payload, max_output)
                elif provider == "gemini" and GEMINI_API_KEY:
                    text, token_count = await _call_gemini(system_prompt, raw_payload, max_output)
                elif provider == "anthropic" and ANTHROPIC_API_KEY:
                    text, token_count = await _call_anthropic(session, system_prompt, raw_payload, max_output)
                elif provider == "deepseek" and (os.getenv("DEEPSEEK_API_KEY") or DEEPSEEK_API_KEY):
                    text, token_count = await _call_deepseek(session, system_prompt, raw_payload, max_output)
                elif provider == "minimax" and (os.getenv("MINIMAX_API_KEY") or MINIMAX_API_KEY):
                    text, token_count = await _call_minimax(session, system_prompt, raw_payload, max_output)
                else:
                    record_provider_skip(provider, "single_coin_judge", "key_missing")
                    continue
                if text:
                    estimated = max(0.001, token_count / 1_000_000 * 2.0)
                    _record_usage(token_count, estimated)
                    _clear_provider_backoff(provider)
                    record_provider_call(provider, "single_coin_judge", True, status="200")
                    return {"text": text, "provider": provider}
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                _record_provider_failure(provider, e)
                record_provider_call(provider, "single_coin_judge", False, status="exception", error=last_error)
                continue
    return {"error": "all_providers_failed", "text": ""}
