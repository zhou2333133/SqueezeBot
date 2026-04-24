"""Optional AI review gateway for the yaobi opportunity queue.

The gateway sends only a compact Top-N payload. It does not include prior chat
history, raw logs, or secrets, and it never returns a direct order instruction.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from config import (
    ANTHROPIC_API_KEY,
    DATA_DIR,
    GEMINI_API_KEY,
    OPENAI_API_KEY,
    ai_credentials_status,
    config_manager,
)
from scanner.knowledge_base import relevant_lessons, relevant_lesson_stats
from scanner.provider_metrics import record_provider_call, record_provider_skip

_ROOT = os.path.join(DATA_DIR, "ai_knowledge")
_CACHE_FILE = os.path.join(_ROOT, "ai_cache.json")
_USAGE_FILE = os.path.join(_ROOT, "ai_usage.json")
_ENDPOINT = "opportunity_review"
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
    return {
        "date": today,
        "calls": int(row.get("calls", 0) or 0),
        "estimated_usd": round(float(row.get("estimated_usd", 0.0) or 0.0), 4),
        "tokens": int(row.get("tokens", 0) or 0),
        "last_call_ts": float(meta.get("last_call_ts", 0) or 0),
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
        },
        "cache_ttl_min": cfg.get("YAOBI_AI_CACHE_TTL_MINUTES", 30),
        "min_interval_min": cfg.get("YAOBI_AI_MIN_INTERVAL_MINUTES", 15),
        "daily_cap_usd": cap,
        "usage": usage,
        "budget_left_usd": round(max(0.0, cap - usage["estimated_usd"]), 4) if cap > 0 else None,
    }


def _compact_candidate(c: Any) -> dict:
    symbol = getattr(c, "symbol", "") or ""
    rule_action = str(getattr(c, "opportunity_action", "") or "")
    rule_bias = "NEUTRAL"
    if rule_action == "WATCH_LONG":
        rule_bias = "LONG"
    elif rule_action == "WATCH_SHORT":
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
        "okx_buy_ratio": round(float(getattr(c, "okx_buy_ratio", 0) or 0), 3),
        "okx_large_trade_pct": round(float(getattr(c, "okx_large_trade_pct", 0) or 0), 3),
        "okx_risk_level": getattr(c, "okx_risk_level", 0),
        "sentiment": getattr(c, "sentiment_label", ""),
        "surf_news_sentiment": getattr(c, "surf_news_sentiment", ""),
        "surf_news_titles": list(getattr(c, "surf_news_titles", []) or [])[:2],
        "tags": list(getattr(c, "anomaly_tags", []) or [])[:6],
        "signals": list(getattr(c, "signals", []) or [])[:8],
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
        symbol = str(row.get("symbol", "")).upper().replace("USDT", "")
        if not symbol:
            continue
        action = str(row.get("action", "OBSERVE")).upper()
        permission = str(row.get("permission", "OBSERVE")).upper()
        result.append({
            "symbol": symbol,
            "action": action if action in {"WATCH_LONG", "WATCH_SHORT", "OBSERVE", "BLOCK"} else "OBSERVE",
            "permission": permission if permission in {"ALLOW_IF_1M_SIGNAL", "OBSERVE", "BLOCK"} else "OBSERVE",
            "confidence": max(0, min(100, int(row.get("confidence", 0) or 0))),
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
        "rule": "Do not place orders. Rank candidates for a 1m scalp bot. Execution still requires local 1m signal.",
        "output_schema": {
            "opportunities": [{
                "symbol": "BASE",
                "action": "WATCH_LONG|WATCH_SHORT|OBSERVE|BLOCK",
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
    if usage["last_call_ts"] and time.time() - usage["last_call_ts"] < min_interval:
        record_provider_skip("ai", _ENDPOINT, "min_interval", items=len(rows))
        return {"items": [], "status": status | {"last_reason": "min_interval"}}
    daily_cap = float(cfg.get("YAOBI_AI_DAILY_USD_CAP", 3.0) or 0.0)
    if daily_cap > 0 and usage["estimated_usd"] >= daily_cap:
        record_provider_skip("ai", _ENDPOINT, "daily_budget_exhausted", items=len(rows))
        return {"items": [], "status": status | {"last_reason": "budget_exhausted"}}

    providers = [
        p.strip().lower()
        for p in str(cfg.get("YAOBI_AI_PROVIDER_PRIORITY", "gemini,openai,anthropic")).split(",")
        if p.strip()
    ]
    system_prompt = (
        "You are a crypto market analyst for a short-term 1-minute scalp bot. "
        "Use only the provided compact data. Return strict JSON only. "
        "Never recommend immediate execution; use ALLOW_IF_1M_SIGNAL at most."
    )
    max_output = int(cfg.get("YAOBI_AI_MAX_OUTPUT_TOKENS", 1200) or 1200)
    last_error = ""
    for provider in providers:
        try:
            if provider == "openai" and OPENAI_API_KEY:
                text, token_count = await _call_openai(session, system_prompt, raw_payload, max_output)
            elif provider == "gemini" and GEMINI_API_KEY:
                text, token_count = await _call_gemini(session, system_prompt, raw_payload, max_output)
            elif provider == "anthropic" and ANTHROPIC_API_KEY:
                text, token_count = await _call_anthropic(session, system_prompt, raw_payload, max_output)
            else:
                record_provider_skip(provider or "ai", _ENDPOINT, "provider_key_missing", items=len(rows))
                continue
            items = _normalize_ai_items(_extract_json_payload(text))
            output_limit = int(cfg.get("YAOBI_AI_TOP_OUTPUT", 6) or 6)
            items = items[:output_limit]
            _cache_put(cache_key, items)
            estimated = max(0.001, token_count / 1_000_000 * 2.0)
            _record_usage(token_count, estimated)
            record_provider_call(provider, _ENDPOINT, True, status="200", items=len(rows))
            return {
                "items": [dict(x, provider=provider, cached=False) for x in items],
                "status": provider_status() | {"last_reason": "ok", "last_provider": provider},
            }
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
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
    primary = str(config_manager.settings.get("YAOBI_AI_MODEL_GEMINI", "gemini-2.5-flash") or "").strip()
    fallbacks = [primary, "gemini-2.5-flash", "gemini-3-flash-preview", "gemini-2.0-flash"]
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


async def _call_gemini(session: aiohttp.ClientSession, system_prompt: str, payload: str, max_output: int) -> tuple[str, int]:
    api_key = os.getenv("GEMINI_API_KEY", GEMINI_API_KEY)
    last_error = ""
    for model in _gemini_model_candidates():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = _gemini_request_body(system_prompt, payload, max_output)
        async with session.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status == 404:
                last_error = f"gemini_http_404[{model}]: {str(data)[:160]}"
                continue
            if resp.status >= 300:
                raise RuntimeError(f"gemini_http_{resp.status}[{model}]: {str(data)[:160]}")
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            if not _is_valid_json_text(text):
                excerpt = str(text or "").replace("\n", " ")[:180]
                last_error = f"gemini_non_json[{model}]: {excerpt or 'empty_response'}"
                continue
            usage = data.get("usageMetadata") or {}
            tokens = int(usage.get("totalTokenCount") or (_estimate_tokens(payload) + _estimate_tokens(text)))
            return text, tokens
    raise RuntimeError(last_error or "gemini_model_unavailable")


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
