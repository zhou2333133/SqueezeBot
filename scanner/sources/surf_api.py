"""Surf API helpers.

Official docs:
- Data API base: https://api.asksurf.ai/gateway
- News feed: GET /v1/news/feed
- Chat: POST /v1/chat/completions through the gateway base.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import aiohttp

from config import next_surf_api_key
from scanner.provider_metrics import record_provider_call, record_provider_skip

SURF_BASE_URL = os.getenv("SURF_API_BASE_URL", "https://api.asksurf.ai/gateway").rstrip("/")
PROJECT_ALIASES = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "bnb",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "AVAX": "avalanche",
    "ARB": "arbitrum",
    "OP": "optimism",
    "SUI": "sui",
    "AAVE": "aave",
    "ORDI": "ordi",
    "PEPE": "pepe",
    "SHIB": "shiba inu",
}


def project_terms(symbol: str, name: str = "") -> list[str]:
    base = (symbol or "").upper().replace("USDT", "").replace("1000", "").strip()
    raw_name = (name or "").strip()
    terms = [base.lower()] if base else []
    if base in PROJECT_ALIASES:
        terms.append(PROJECT_ALIASES[base])
    if raw_name and raw_name.upper() not in {base, f"{base}USDT"}:
        terms.append(raw_name.lower())
    out: list[str] = []
    for term in terms:
        if term and term not in out:
            out.append(term)
    return out


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _items(data: Any) -> list[dict]:
    if not isinstance(data, dict):
        return []
    rows = data.get("data", [])
    if isinstance(rows, list):
        return [x for x in rows if isinstance(x, dict)]
    return []


def _news_item_text(item: dict) -> str:
    parts = [
        str(item.get("title") or ""),
        str(item.get("summary") or ""),
        str(item.get("project_name") or ""),
        str(item.get("source") or ""),
    ]
    return " ".join(x for x in parts if x).strip()


def normalize_news_items(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in items:
        out.append({
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "summary": item.get("summary", ""),
            "source": item.get("source", ""),
            "project_name": item.get("project_name", ""),
            "published_at": item.get("published_at", 0),
            "url": item.get("url", ""),
            "text": _news_item_text(item),
        })
    return out


def news_matches_symbol(item: dict, symbol: str, name: str = "") -> bool:
    text = str(item.get("text") or "").lower()
    project = str(item.get("project_name") or "").lower()
    for term in project_terms(symbol, name):
        if not term:
            continue
        if project == term or term in text:
            return True
    return False


async def fetch_news_feed(
    session: aiohttp.ClientSession,
    projects: list[str] | None = None,
    limit: int = 50,
    sort_by: str = "recency",
    timeout_sec: float = 8.0,
) -> list[dict]:
    api_key = next_surf_api_key()
    endpoint = "news/feed"
    if not api_key:
        record_provider_skip("surf", endpoint, "missing_surf_api_key", items=len(projects or []))
        return []

    params: dict[str, str | int] = {
        "limit": max(1, min(int(limit), 50)),
        "offset": 0,
        "sort_by": sort_by,
    }
    clean_projects = [p.strip().lower() for p in projects or [] if p and p.strip()]
    if clean_projects:
        params["project"] = ",".join(clean_projects)

    url = f"{SURF_BASE_URL}/v1/news/feed?{urlencode(params)}"
    try:
        async with session.get(
            url,
            headers=_headers(api_key),
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as resp:
            if resp.status != 200:
                record_provider_call("surf", endpoint, False, status=resp.status)
                return []
            data = await resp.json()
            items = normalize_news_items(_items(data))
            record_provider_call("surf", endpoint, True, status=resp.status, items=len(items))
            return items
    except Exception as e:
        record_provider_call("surf", endpoint, False, status="exception", error=f"{type(e).__name__}: {e}")
        return []


async def search_news(
    session: aiohttp.ClientSession,
    query: str,
    timeout_sec: float = 8.0,
) -> list[dict]:
    api_key = next_surf_api_key()
    endpoint = "search/news"
    query = (query or "").strip()
    if not query:
        return []
    if not api_key:
        record_provider_skip("surf", endpoint, "missing_surf_api_key")
        return []

    url = f"{SURF_BASE_URL}/v1/search/news?{urlencode({'q': query})}"
    try:
        async with session.get(
            url,
            headers=_headers(api_key),
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as resp:
            if resp.status != 200:
                record_provider_call("surf", endpoint, False, status=resp.status)
                return []
            data = await resp.json()
            items = normalize_news_items(_items(data))
            record_provider_call("surf", endpoint, True, status=resp.status, items=len(items))
            return items
    except Exception as e:
        record_provider_call("surf", endpoint, False, status="exception", error=f"{type(e).__name__}: {e}")
        return []


async def chat_completion(
    session: aiohttp.ClientSession,
    prompt: str,
    model: str = "surf-1.5-instant",
    timeout_sec: float = 30.0,
    reasoning_effort: str = "low",
) -> tuple[bool, str, int | str]:
    api_key = next_surf_api_key()
    endpoint = "chat/completions"
    if not api_key:
        record_provider_skip("surf", endpoint, "missing_surf_api_key")
        return False, "", "missing_surf_api_key"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "reasoning_effort": reasoning_effort,
        "ability": ["search", "market_analysis"],
    }
    try:
        async with session.post(
            f"{SURF_BASE_URL}/v1/chat/completions",
            json=payload,
            headers=_headers(api_key),
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as resp:
            if resp.status != 200:
                record_provider_call("surf", endpoint, False, status=resp.status)
                return False, "", resp.status
            data = await resp.json()
            record_provider_call("surf", endpoint, True, status=resp.status, items=1)
            text = data["choices"][0]["message"]["content"].strip()
            return True, text, resp.status
    except Exception as e:
        record_provider_call("surf", endpoint, False, status="exception", error=f"{type(e).__name__}: {e}")
        return False, "", f"{type(e).__name__}: {e}"
