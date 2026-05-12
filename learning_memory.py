"""
学习记忆 — 按币种/策略/形态维护经验。
"""
import json
import logging
import os
import time
from collections import defaultdict

from config import DATA_DIR
from persistence import atomic_write_json, safe_read_json

logger = logging.getLogger(__name__)

MEMORY_FILE = os.path.join(DATA_DIR, "learning_memory.json")


def _load() -> dict:
    data = safe_read_json(MEMORY_FILE)
    if isinstance(data, dict):
        return data
    return _empty()


def _save(data: dict) -> None:
    atomic_write_json(MEMORY_FILE, data)


def _empty() -> dict:
    return {"symbols": {}, "strategies": {}, "patterns": {}, "updated_at": 0.0}


# ══════════════════════════════════════════════════════════════════════════════
# 记录一笔成交
# ══════════════════════════════════════════════════════════════════════════════
def record_trade(trade: dict) -> None:
    """用成交记录更新记忆。"""
    try:
        mem = _load()
        symbol = str(trade.get("symbol", ""))
        tag = str(trade.get("strategy_tag", ""))
        pnl = float(trade.get("pnl_usdt", 0) or 0)
        features = trade.get("_features", trade.get("entry_context", {}))
        if isinstance(features, dict) and features:
            pattern = features.get("patterns", features.get("kline_pattern", []))
            sig_type = features.get("signal_type", tag)
        else:
            pattern = []
            sig_type = tag

        # 按币种记忆
        if symbol not in mem["symbols"]:
            mem["symbols"][symbol] = {"trades": 0, "wins": 0, "pnl": 0.0,
                                       "patterns": defaultdict(int), "strategies": defaultdict(int)}
        s = mem["symbols"][symbol]
        s["trades"] += 1
        if pnl >= 0:
            s["wins"] += 1
        s["pnl"] += pnl
        for p in (pattern if isinstance(pattern, list) else [str(pattern)]):
            s["patterns"][str(p)] = s["patterns"].get(str(p), 0) + 1
        s["strategies"][tag] = s["strategies"].get(tag, 0) + 1

        # 按策略记忆
        if tag not in mem["strategies"]:
            mem["strategies"][tag] = {"trades": 0, "wins": 0, "pnl": 0.0,
                                       "patterns": defaultdict(int)}
        st = mem["strategies"][tag]
        st["trades"] += 1
        if pnl >= 0:
            st["wins"] += 1
        st["pnl"] += pnl
        for p in (pattern if isinstance(pattern, list) else [str(pattern)]):
            st["patterns"][str(p)] = st["patterns"].get(str(p), 0) + 1

        mem["updated_at"] = time.time()
        _save(mem)
    except Exception as e:
        logger.debug("learning_memory.record_trade error: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 查询记忆
# ══════════════════════════════════════════════════════════════════════════════
def get_symbol_memory(symbol: str) -> dict:
    mem = _load()
    return mem.get("symbols", {}).get(symbol, {})


def get_strategy_memory(tag: str) -> dict:
    mem = _load()
    return mem.get("strategies", {}).get(tag, {})


def get_all_symbols() -> dict:
    return _load().get("symbols", {})


def get_all_strategies() -> dict:
    return _load().get("strategies", {})


def get_weak_symbols(min_trades: int = 3, max_win_rate: float = 0.35) -> list[dict]:
    """返回弱币：交易足够多但胜率低的。"""
    result = []
    for sym, data in get_all_symbols().items():
        if data.get("trades", 0) >= min_trades:
            wr = data["wins"] / data["trades"]
            if wr <= max_win_rate:
                result.append({"symbol": sym, "win_rate": round(wr, 2),
                               "trades": data["trades"], "pnl": round(data["pnl"], 2)})
    return sorted(result, key=lambda x: x["win_rate"])


def get_strong_strategies(min_trades: int = 5) -> list[dict]:
    result = []
    for tag, data in get_all_strategies().items():
        if data.get("trades", 0) >= min_trades:
            wr = data["wins"] / data["trades"]
            if wr >= 0.55:
                result.append({"strategy": tag, "win_rate": round(wr, 2),
                               "trades": data["trades"], "pnl": round(data["pnl"], 2)})
    return sorted(result, key=lambda x: -x["win_rate"])


def get_problem_patterns(min_occurrences: int = 3) -> list[dict]:
    """返回亏钱模式。"""
    mem = _load()
    all_counts = defaultdict(int)
    all_losses = defaultdict(float)
    for sdata in mem.get("symbols", {}).values():
        for pat, cnt in sdata.get("patterns", {}).items():
            all_counts[pat] += cnt
    # 简单版本：模式出现次数（后续可用 LLM 做深入分析）
    result = [{"pattern": p, "count": c} for p, c in all_counts.items() if c >= min_occurrences]
    return sorted(result, key=lambda x: -x["count"])


def get_memory_summary() -> dict:
    """返回记忆摘要，供 AI 报告使用。"""
    mem = _load()
    symbols = mem.get("symbols", {})
    strategies = mem.get("strategies", {})
    return {
        "total_symbols": len(symbols),
        "total_trades": sum(s.get("trades", 0) for s in symbols.values()),
        "weak_symbols": get_weak_symbols(),
        "strong_strategies": get_strong_strategies(),
        "problem_patterns": get_problem_patterns(),
        "symbol_count": len(symbols),
        "strategy_count": len(strategies),
    }
