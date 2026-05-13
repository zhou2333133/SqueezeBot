"""
学习记忆 — 按币种/市场状态/规则维护经验。

Schema:
  symbols[BTCUSDT][state_key][rule_id] = {
    trades, wins, losses, win_rate, avg_pnl, total_pnl,
    max_drawdown, last_20_win_rate, last_20_avg_pnl,
    score, updated_at
  }

规则：
  - state_key 由 market_state.classify() 生成
  - rule_id 格式: "{strategy_tag}__{signal_type}"
  - score 综合 win_rate + avg_pnl + 样本量，用于 rule_selector 排序
"""
import json
import logging
import os
import time
from collections import defaultdict, deque

from config import DATA_DIR
from persistence import atomic_write_json, safe_read_json

logger = logging.getLogger(__name__)

MEMORY_FILE = os.path.join(DATA_DIR, "learning_memory.json")


# ══════════════════════════════════════════════════════════════════════════════
# 持久化
# ══════════════════════════════════════════════════════════════════════════════

def _load() -> dict:
    data = safe_read_json(MEMORY_FILE)
    if isinstance(data, dict):
        return data
    return _empty()


def _save(data: dict) -> None:
    atomic_write_json(MEMORY_FILE, data)


def _empty() -> dict:
    return {
        "symbols": {},      # symbol → state_key → rule_id → stats
        "strategies": {},   # tag → stats (旧兼容)
        "patterns": {},     # pattern → count (旧兼容)
        "_by_state": {},    # state_key → rule_id → stats (跨币种聚合)
        "updated_at": 0.0,
        "version": 2,       # schema 版本
    }


# ══════════════════════════════════════════════════════════════════════════════
# 统计容器
# ══════════════════════════════════════════════════════════════════════════════

def _empty_stats() -> dict:
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_pnl": 0.0,
        "total_pnl": 0.0,
        "max_drawdown": 0.0,
        "last_20_pnls": [],    # 最近 20 笔盈亏 (用于滚动计算)
        "last_20_win_rate": 0.0,
        "last_20_avg_pnl": 0.0,
        "score": 0.0,
        "updated_at": 0.0,
    }


def _compute_score(stats: dict) -> float:
    """综合评分 0~1。因子：胜率、平均盈亏、样本量。"""
    n = stats.get("trades", 0)
    if n < 3:
        return 0.0
    wr = stats.get("win_rate", 0.0)
    avg = stats.get("avg_pnl", 0.0)
    # 样本置信：前 30 笔逐步增加权重
    sample_factor = min(n / 30.0, 1.0)
    # 胜率贡献：53% 以上为正，45% 以下为负
    wr_score = (wr - 0.45) / 0.15  # 0.45→0.0, 0.53→0.53, 0.60→1.0
    wr_score = max(-0.5, min(1.0, wr_score))
    # 盈亏贡献：正值加分
    pnl_score = max(-0.5, min(1.0, avg / 2.0)) if avg else 0.0
    score = (wr_score * 0.6 + pnl_score * 0.4) * (0.5 + 0.5 * sample_factor)
    return round(max(0.0, min(1.0, score)), 4)


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
        state_key = str(trade.get("_state_key", "")) or "unknown"
        signal_type = str(trade.get("_signal_type", "")) or tag
        rule_id = f"{tag}__{signal_type}" if tag and signal_type else tag or signal_type
        # 候选来源（第 1 层标识）
        features = trade.get("_features", {})
        candidate_source = str(
            trade.get("candidate_source") or
            features.get("candidate_source") or
            "unknown"
        )
        # K 线模式（旧兼容用）
        pattern = features.get("patterns", []) if isinstance(features, dict) else []

        if not symbol:
            return

        # ── 新 schema：candidate_source × symbol × state_key × rule_id ──
        _by_source = mem.setdefault("by_source", {})
        if candidate_source not in _by_source:
            _by_source[candidate_source] = {}
        if symbol not in _by_source[candidate_source]:
            _by_source[candidate_source][symbol] = {}
        if state_key not in _by_source[candidate_source][symbol]:
            _by_source[candidate_source][symbol][state_key] = {}
        if rule_id not in _by_source[candidate_source][symbol][state_key]:
            _by_source[candidate_source][symbol][state_key][rule_id] = _empty_stats()
        _update_stats(_by_source[candidate_source][symbol][state_key][rule_id], pnl)

        # ── 旧兼容：symbol → state_key → rule_id ────────────────────────
        if symbol not in mem["symbols"]:
            mem["symbols"][symbol] = {}
        if state_key not in mem["symbols"][symbol]:
            mem["symbols"][symbol][state_key] = {}
        if rule_id not in mem["symbols"][symbol][state_key]:
            mem["symbols"][symbol][state_key][rule_id] = _empty_stats()
        _update_stats(mem["symbols"][symbol][state_key][rule_id], pnl)

        # 跨币种聚合 (by state_key)
        _by_state = mem.setdefault("_by_state", {})
        if state_key not in _by_state:
            _by_state[state_key] = {}
        if rule_id not in _by_state[state_key]:
            _by_state[state_key][rule_id] = _empty_stats()
        _update_stats(_by_state[state_key][rule_id], pnl)

        # 跨来源+币种聚合 (by rule_id only, 全部来源)
        _by_rule = mem.setdefault("by_rule", {})
        if rule_id not in _by_rule:
            _by_rule[rule_id] = _empty_stats()
        _update_stats(_by_rule[rule_id], pnl)

        # ── 旧 schema 兼容 ──────────────────────────────────────────────
        # 旧格式：按币种聚合（平铺）
        _update_legacy_symbol(mem, symbol, tag, pnl, pattern)
        _update_legacy_strategy(mem, tag, pnl, pattern)

        mem["updated_at"] = time.time()
        _save(mem)
    except Exception as e:
        logger.debug("learning_memory.record_trade error: %s", e)


def _update_stats(stats: dict, pnl: float) -> None:
    """更新单个统计容器。"""
    stats["trades"] += 1
    stats["total_pnl"] = round(stats.get("total_pnl", 0.0) + pnl, 4)
    if pnl >= 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    stats["win_rate"] = round(stats["wins"] / max(stats["trades"], 1), 4)
    stats["avg_pnl"] = round(stats["total_pnl"] / max(stats["trades"], 1), 4)
    if pnl < stats.get("max_drawdown", 0.0):
        stats["max_drawdown"] = round(pnl, 4)

    # 滚动最近 20 笔
    last20 = stats.setdefault("last_20_pnls", [])
    last20.append(pnl)
    if len(last20) > 20:
        last20.pop(0)
    n20 = len(last20)
    if n20 > 0:
        wins20 = sum(1 for p in last20 if p >= 0)
        stats["last_20_win_rate"] = round(wins20 / n20, 4)
        stats["last_20_avg_pnl"] = round(sum(last20) / n20, 4)

    stats["score"] = _compute_score(stats)
    stats["updated_at"] = time.time()


def _update_legacy_symbol(mem: dict, symbol: str, tag: str, pnl: float,
                          pattern: list) -> None:
    """旧 schema 兼容：平铺按币种统计。"""
    if "symbols" not in mem:
        return
    sym_data = mem.setdefault("_legacy_symbols", {}).setdefault(symbol, {
        "trades": 0, "wins": 0, "pnl": 0.0,
        "patterns": {}, "strategies": {},
    })
    sym_data["trades"] += 1
    if pnl >= 0:
        sym_data["wins"] += 1
    sym_data["pnl"] = round(sym_data.get("pnl", 0.0) + pnl, 4)
    for p in (pattern if isinstance(pattern, list) else [str(pattern)]):
        sym_data["patterns"][str(p)] = sym_data["patterns"].get(str(p), 0) + 1
    sym_data["strategies"][tag] = sym_data["strategies"].get(tag, 0) + 1


def _update_legacy_strategy(mem: dict, tag: str, pnl: float,
                            pattern: list) -> None:
    """旧 schema 兼容：按策略统计。"""
    if not tag:
        return
    strat = mem.setdefault("strategies", {}).setdefault(tag, {
        "trades": 0, "wins": 0, "pnl": 0.0, "patterns": {},
    })
    strat["trades"] += 1
    if pnl >= 0:
        strat["wins"] += 1
    strat["pnl"] = round(strat.get("pnl", 0.0) + pnl, 4)
    for p in (pattern if isinstance(pattern, list) else [str(pattern)]):
        strat["patterns"][str(p)] = strat["patterns"].get(str(p), 0) + 1


# ══════════════════════════════════════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════════════════════════════════════

def get_symbol_memory(symbol: str) -> dict:
    """返回币种记忆（新 schema: symbol→state_key→rule_id）。"""
    mem = _load()
    return mem.get("symbols", {}).get(symbol, {})


def get_rule_stats(symbol: str, state_key: str, rule_id: str) -> dict:
    """查询特定场景下某规则的表现。"""
    mem = _load()
    return (mem.get("symbols", {})
            .get(symbol, {})
            .get(state_key, {})
            .get(rule_id, {}))


def get_rule_stats_by_source(candidate_source: str, symbol: str, state_key: str, rule_id: str) -> dict:
    """按候选来源查询规则表现（新 schema: by_source→source→symbol→state_key→rule_id）。"""
    mem = _load()
    return (mem.get("by_source", {})
            .get(candidate_source, {})
            .get(symbol, {})
            .get(state_key, {})
            .get(rule_id, {}))


def get_state_summary(state_key: str, min_trades: int = 5) -> list[dict]:
    """返回某市场状态下所有规则表现，按 score 降序。"""
    mem = _load()
    rules = mem.get("_by_state", {}).get(state_key, {})
    result = []
    for rule_id, stats in rules.items():
        if stats.get("trades", 0) >= min_trades:
            result.append({
                "rule_id": rule_id,
                **{k: stats[k] for k in ("trades", "wins", "losses", "win_rate",
                                          "avg_pnl", "score") if k in stats},
            })
    return sorted(result, key=lambda x: -x.get("score", 0))


def get_weak_rules(symbol: str, state_key: str, min_trades: int = 5) -> list[dict]:
    """返回特定币种+市场状态下表现差的规则。"""
    mem = _load()
    rules = (mem.get("symbols", {})
             .get(symbol, {})
             .get(state_key, {}))
    result = []
    for rule_id, stats in rules.items():
        n = stats.get("trades", 0)
        if n >= min_trades and stats.get("score", 0.5) < 0.3:
            result.append({
                "rule_id": rule_id,
                "trades": n,
                "score": stats.get("score", 0),
                "win_rate": stats.get("win_rate", 0),
                "avg_pnl": stats.get("avg_pnl", 0),
            })
    return sorted(result, key=lambda x: x["score"])


def get_state_keys(symbol: str = "") -> list[str]:
    """返回所有已知的 state_key，可选过滤币种。"""
    mem = _load()
    if symbol:
        return list(mem.get("symbols", {}).get(symbol, {}).keys())
    return list(mem.get("_by_state", {}).keys())


# ── 旧兼容查询 ─────────────────────────────────────────────────────────────

def get_strategy_memory(tag: str) -> dict:
    mem = _load()
    return mem.get("strategies", {}).get(tag, {})


def get_all_symbols() -> dict:
    mem = _load()
    return mem.get("_legacy_symbols", mem.get("symbols", {}))


def get_all_strategies() -> dict:
    return _load().get("strategies", {})


def get_weak_symbols(min_trades: int = 3, max_win_rate: float = 0.35) -> list[dict]:
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
    mem = _load()
    all_counts: dict[str, int] = {}
    for sdata in mem.get("_legacy_symbols", {}).values():
        for pat, cnt in sdata.get("patterns", {}).items():
            all_counts[pat] = all_counts.get(pat, 0) + cnt
    result = [{"pattern": p, "count": c} for p, c in all_counts.items()
              if c >= min_occurrences]
    return sorted(result, key=lambda x: -x["count"])


def get_memory_summary() -> dict:
    mem = _load()
    # 汇总（兼容新旧 schema）
    symbols = mem.get("symbols", {})
    total_trades = 0
    total_rules = 0
    state_count = 0
    for sym, sym_data in symbols.items():
        if not isinstance(sym_data, dict):
            continue
        # 旧格式：sym_data = {trades, wins, ...} — 跳过
        if "trades" in sym_data and not any(k.startswith("trend_") for k in sym_data):
            continue
        # 新格式：sym_data = {state_key: {rule_id: stats}}
        state_count += len(sym_data)
        for state_key, rules in sym_data.items():
            if isinstance(rules, dict):
                total_rules += len(rules)
                for rule_id, stats in rules.items():
                    if isinstance(stats, dict):
                        total_trades += stats.get("trades", 0)

    return {
        "total_symbols": len(symbols),
        "total_state_keys": state_count,
        "total_rules": total_rules,
        "total_trades": total_trades,
        "weak_symbols": get_weak_symbols(),
        "strong_strategies": get_strong_strategies(),
        "problem_patterns": get_problem_patterns(),
        "version": mem.get("version", 1),
    }
