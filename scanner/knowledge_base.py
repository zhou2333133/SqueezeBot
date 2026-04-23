"""Local feedback store for scanner and scalp review loops.

The store is intentionally small and deterministic. It records compact trade
outcomes so the optional AI review can fetch recent lessons without carrying a
full chat history or raw logs into every request.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from config import DATA_DIR

_ROOT = os.path.join(DATA_DIR, "ai_knowledge")
_TRADES_FILE = os.path.join(_ROOT, "trade_feedback.jsonl")
_MAX_READ_ROWS = 800


def _ensure_root() -> None:
    os.makedirs(_ROOT, exist_ok=True)


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _compact_trade(trade: dict) -> dict:
    ctx = trade.get("entry_context") or {}
    return {
        "ts": trade.get("exit_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": str(trade.get("symbol", "")).upper(),
        "direction": trade.get("direction", ""),
        "signal": trade.get("signal_label") or trade.get("signal") or "",
        "close_reason": trade.get("close_reason", ""),
        "pnl_usdt": round(_as_float(trade.get("pnl_usdt")), 4),
        "net_r": round(_as_float(trade.get("net_r")), 4),
        "mfe_pct": round(_as_float(trade.get("mfe_pct")), 4),
        "mae_pct": round(_as_float(trade.get("mae_pct")), 4),
        "post_exit_mfe_pct": round(_as_float(trade.get("post_exit_mfe_pct")), 4),
        "diagnosis": trade.get("trade_diagnosis", ""),
        "tags": list(trade.get("diagnosis_tags", []) or [])[:6],
        "market_state": trade.get("market_state", ""),
        "entry": {
            "pre_3m": round(_as_float(ctx.get("pre_entry_3m_pct")), 4),
            "pre_5m": round(_as_float(ctx.get("pre_entry_5m_pct")), 4),
            "pre_15m": round(_as_float(ctx.get("pre_entry_15m_pct")), 4),
            "ema20_dev": round(_as_float(ctx.get("ema20_deviation_pct")), 4),
            "atr": round(_as_float(ctx.get("atr_pct")), 4),
            "taker": round(_as_float(ctx.get("current_taker_ratio")), 4),
            "oi_3m": round(_as_float(ctx.get("oi_change_3m_pct")), 4),
            "yaobi_action": ctx.get("yaobi_decision_action", ""),
            "opportunity_action": ctx.get("yaobi_opportunity_action", ""),
            "opportunity_score": ctx.get("yaobi_opportunity_score", 0),
        },
    }


def record_trade_feedback(trade: dict) -> None:
    if not trade or not trade.get("symbol"):
        return
    _ensure_root()
    row = _compact_trade(trade)
    with open(_TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_rows() -> list[dict]:
    if not os.path.exists(_TRADES_FILE):
        return []
    rows: list[dict] = []
    with open(_TRADES_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()[-_MAX_READ_ROWS:]
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def relevant_lessons(symbol: str, limit: int = 5) -> list[dict]:
    sym = str(symbol or "").upper().replace("USDT", "")
    if not sym:
        return []
    rows = [
        row for row in _read_rows()
        if str(row.get("symbol", "")).upper().replace("USDT", "") == sym
    ]
    return rows[-limit:]


def knowledge_status() -> dict:
    rows = _read_rows()
    symbols = {str(row.get("symbol", "")).upper() for row in rows if row.get("symbol")}
    return {
        "enabled": True,
        "path": _TRADES_FILE,
        "rows": len(rows),
        "symbols": len(symbols),
        "updated_at": rows[-1].get("ts") if rows else "",
    }
