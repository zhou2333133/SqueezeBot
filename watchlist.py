from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Iterable

from config import DATA_DIR

logger = logging.getLogger(__name__)

WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
VALID_STATUSES = {"观察", "等待确认", "允许交易", "禁止交易", "只看不做"}
BLOCK_STATUSES = {"禁止交易", "只看不做"}

_cache: dict[str, dict] | None = None
_cache_mtime: float | None = None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_symbol(symbol: str | None) -> str:
    value = str(symbol or "").strip().upper().replace("$", "")
    return "".join(ch for ch in value if ch.isalnum())


def symbol_variants(symbol: str | None) -> set[str]:
    norm = normalize_symbol(symbol)
    if not norm:
        return set()
    variants = {norm}
    if norm.endswith("USDT"):
        variants.add(norm[:-4])
    else:
        variants.add(f"{norm}USDT")
    return variants


def _load(force: bool = False) -> dict[str, dict]:
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(WATCHLIST_FILE)
    except OSError:
        mtime = None

    if not force and _cache is not None and _cache_mtime == mtime:
        return _cache

    rows: dict[str, dict] = {}
    if mtime is not None:
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("items", raw) if isinstance(raw, dict) else raw
            if isinstance(items, dict):
                iterable: Iterable[dict] = items.values()
            elif isinstance(items, list):
                iterable = items
            else:
                iterable = []
            for item in iterable:
                if not isinstance(item, dict):
                    continue
                sym = normalize_symbol(item.get("symbol"))
                if sym:
                    item["symbol"] = sym
                    rows[sym] = item
        except Exception as e:
            logger.warning("关注池读取失败，使用空列表: %s", e)

    _cache = rows
    _cache_mtime = mtime
    return rows


def _save(rows: dict[str, dict]) -> None:
    global _cache, _cache_mtime
    os.makedirs(os.path.dirname(WATCHLIST_FILE) or DATA_DIR, exist_ok=True)
    payload = {"updated_at": _now(), "items": rows}
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _cache = rows
    try:
        _cache_mtime = os.path.getmtime(WATCHLIST_FILE)
    except OSError:
        _cache_mtime = time.time()


def _candidate_index(candidates: Iterable[dict] | None) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        for variant in symbol_variants(c.get("symbol")):
            index[variant] = c
    return index


def list_watch_items(candidates: Iterable[dict] | None = None) -> list[dict]:
    rows = _load()
    cidx = _candidate_index(candidates)
    out: list[dict] = []
    for item in rows.values():
        row = dict(item)
        match = next((cidx[v] for v in symbol_variants(row.get("symbol")) if v in cidx), None)
        if match:
            row.update({
                "last_seen_price": match.get("price_usd") or row.get("last_seen_price", 0),
                "latest_score": match.get("score", row.get("latest_score", 0)),
                "latest_anomaly_score": match.get("anomaly_score", row.get("latest_anomaly_score", 0)),
                "latest_oi_grade": match.get("oi_trend_grade", row.get("latest_oi_grade", "")),
                "latest_oi_7d_change_pct": match.get("oi_change_7d_pct", row.get("latest_oi_7d_change_pct", 0)),
                "latest_decision_action": match.get("decision_action", row.get("latest_decision_action", "")),
                "latest_decision_note": match.get("decision_note", row.get("latest_decision_note", "")),
            })
        out.append(row)

    status_rank = {"禁止交易": 0, "等待确认": 1, "允许交易": 2, "观察": 3, "只看不做": 4}
    return sorted(
        out,
        key=lambda x: (
            status_rank.get(x.get("status", "观察"), 9),
            str(x.get("updated_at", "")),
        ),
        reverse=True,
    )


def get_watch_item(symbol: str | None) -> dict | None:
    rows = _load()
    for variant in symbol_variants(symbol):
        if variant in rows:
            return dict(rows[variant])
    return None


def upsert_watch_item(
    symbol: str,
    *,
    status: str = "观察",
    reason: str = "",
    manual_note: str = "",
    ban_trade: bool | None = None,
    watch_until: str = "",
    source: str = "manual",
) -> dict:
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("symbol required")

    status = status if status in VALID_STATUSES else "观察"
    if ban_trade is None:
        ban_trade = status in BLOCK_STATUSES

    rows = dict(_load())
    existing_key = next((v for v in symbol_variants(sym) if v in rows), sym)
    existing = rows.get(existing_key, {})
    sym = existing.get("symbol", sym)
    now = _now()
    row = {
        "symbol": sym,
        "status": status,
        "reason": reason.strip() or existing.get("reason", ""),
        "manual_note": manual_note.strip() or existing.get("manual_note", ""),
        "ban_trade": bool(ban_trade),
        "watch_until": watch_until.strip() or existing.get("watch_until", ""),
        "source": source.strip() or existing.get("source", "manual"),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
        "last_seen_price": existing.get("last_seen_price", 0),
        "latest_score": existing.get("latest_score", 0),
        "latest_anomaly_score": existing.get("latest_anomaly_score", 0),
        "latest_oi_grade": existing.get("latest_oi_grade", ""),
        "latest_decision_action": existing.get("latest_decision_action", ""),
    }
    rows[sym] = row
    _save(rows)
    return dict(row)


def remove_watch_item(symbol: str) -> bool:
    rows = dict(_load())
    removed = False
    for variant in list(symbol_variants(symbol)):
        if variant in rows:
            rows.pop(variant, None)
            removed = True
    if removed:
        _save(rows)
    return removed


def is_symbol_blocked(symbol: str | None) -> bool:
    item = get_watch_item(symbol)
    if not item:
        return False
    return bool(item.get("ban_trade")) or item.get("status") in BLOCK_STATUSES
