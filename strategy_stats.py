"""
策略统计：从 strategy_trades.jsonl 聚合胜率/PnL/持仓数。

统一 schema（PAPER 和 LIVE 共用）：
  symbol, direction, strategy_tag, execution_mode (PAPER/LIVE),
  entry_time, exit_time, entry_price, exit_price,
  pnl_usdt, pnl_pct, close_reason, signal_label, market_state, ts

提供：
  record_trade(trade)    → 追加一条成交记录到 JSONL
  get_dashboard(mode)    → 按 strategy_tag 聚合 Dashboard 数据（支持 mode=paper/live/all）
  get_trades(limit, mode) → 返回最近 N 条成交记录
"""
import json
import logging
import os
import time
from collections import defaultdict

from config import DATA_DIR

logger = logging.getLogger(__name__)

TRADES_FILE = os.path.join(DATA_DIR, "strategy_trades.jsonl")

_ALL_TAGS = ["启动型", "OI爆发", "静默建仓", "突破前夜", "早期启动", ""]


def record_trade(trade: dict) -> None:
    """追加一条成交记录到 JSONL。PAPER/LIVE 共用同一入口。"""
    execution_mode = str(trade.get("execution_mode") or "UNKNOWN").upper()
    if execution_mode not in ("PAPER", "LIVE"):
        execution_mode = "PAPER" if trade.get("paper") else "LIVE"
    row = {
        "symbol":         str(trade.get("symbol", "")),
        "strategy_tag":   str(trade.get("strategy_tag") or ""),
        "direction":      str(trade.get("direction", "")),
        "execution_mode": execution_mode,
        "entry_time":     str(trade.get("entry_time", "")),
        "exit_time":      str(trade.get("exit_time", "")),
        "entry_price":    _f(trade.get("entry_price")),
        "exit_price":     _f(trade.get("exit_price")),
        "pnl_usdt":       _f(trade.get("pnl_usdt")),
        "pnl_pct":        _f(trade.get("pnl_pct")),
        "close_reason":   str(trade.get("close_reason", "")),
        "signal_label":   str(trade.get("signal_label", "")),
        "market_state":   str(trade.get("market_state", "")),
        "paper":          bool(trade.get("paper", True)),
        "ts":             time.time(),
    }
    try:
        os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
        with open(TRADES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("策略成交记录写入失败: %s", e)


def get_dashboard(mode: str = "all") -> dict:
    """按 strategy_tag 聚合。mode=paper/live/all 过滤 execution_mode。"""
    trades = _load_trades(mode_filter=mode)
    by_tag: dict[str, dict] = {}
    for tag in _ALL_TAGS:
        by_tag[tag] = _empty_stats()
    for t in trades:
        tag = str(t.get("strategy_tag") or "")
        if tag not in by_tag:
            tag = ""
        _accumulate(by_tag[tag], t)
    result = {}
    for tag, st in by_tag.items():
        label = tag if tag else "未分类"
        total = st["total"]
        wins = st["wins"]
        st["win_rate"] = round(wins / total, 4) if total > 0 else 0.0
        st["loss_rate"] = round((total - wins) / total, 4) if total > 0 else 0.0
        st["pnl_24h"] = round(st["pnl_24h"], 4)
        st["pnl_7d"] = round(st["pnl_7d"], 4)
        st["avg_pnl"] = round(st["pnl_all"] / total, 4) if total > 0 else 0.0
        st["total_pnl"] = round(st["pnl_all"], 4)
        reasons = sorted(st["failure_reasons"].items(), key=lambda x: -x[1])[:3]
        st["top_failures"] = [{"reason": r, "count": c} for r, c in reasons]
        del st["failure_reasons"]
        del st["pnl_all"]
        result[label] = st
    return {
        "updated_at": time.time(),
        "mode": mode,
        "strategies": result,
    }


def get_trades(limit: int = 50, mode: str = "all") -> list[dict]:
    """返回最近 N 条成交记录。mode=paper/live/all 过滤。"""
    trades = _load_trades(mode_filter=mode)
    return trades[-limit:]


def _empty_stats() -> dict:
    return {
        "total": 0, "wins": 0, "losses": 0,
        "pnl_all": 0.0, "pnl_24h": 0.0, "pnl_7d": 0.0,
        "failure_reasons": defaultdict(int),
    }


def _accumulate(st: dict, trade: dict) -> None:
    st["total"] += 1
    pnl = _f(trade.get("pnl_usdt"))
    if pnl >= 0:
        st["wins"] += 1
    else:
        st["losses"] += 1
    st["pnl_all"] += pnl
    ts = _f(trade.get("ts"))
    now = time.time()
    if now - ts < 86400:
        st["pnl_24h"] += pnl
    if now - ts < 86400 * 7:
        st["pnl_7d"] += pnl
    reason = str(trade.get("close_reason", ""))
    if reason and pnl < 0:
        st["failure_reasons"][reason] += 1


def _load_trades(mode_filter: str = "all") -> list[dict]:
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        trades = []
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                trade = json.loads(line)
                if _mode_match(trade, mode_filter):
                    trades.append(trade)
        return trades
    except Exception as e:
        logger.warning("策略成交记录加载失败: %s", e)
        return []


def _mode_match(trade: dict, mode_filter: str) -> bool:
    if mode_filter == "all":
        return True
    em = str(trade.get("execution_mode", "")).upper()
    return em == mode_filter.upper()


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
