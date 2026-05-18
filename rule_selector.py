"""
规则选择器 — 软判断当前币种+市场状态+规则是否适合交易。

职责：
  - 查询 learning_memory 中同一 state_key 下规则的历史表现
  - 冷启动保护：样本不足时不拦截
  - 只返回 allow/block/confidence/memory_score，不改订单参数
  - 所有决策写入 data/rule_selector_events.jsonl

集成位置：
  _execute_entry → rule_selector.should_trade() → risk_guard.check_open_order()

冷启动策略：
  样本 < 10：只记录，不拦截
  样本 10-30：只 warn / weak block 高风险组合
  样本 > 30：允许正式 block score < 0.3 的组合
"""
import json
import logging
import os
import time

from config import DATA_DIR
from persistence import append_jsonl

logger = logging.getLogger(__name__)

EVENTS_FILE = os.path.join(DATA_DIR, "rule_selector_events.jsonl")

# 冷启动阈值
COLD_START_MIN = 10        # < 10 笔：不拦截
WARM_START_MIN = 30        # 10-30 笔：仅 weak block
BLOCK_SCORE = 0.3          # score < 0.3 视为明显亏损组合

# ── 当日快速拦截（rapid block）─────────────────────────────────────────
# 同币当天 2 笔 SL 后自动 ban 到次日 00:00
_RAPID_BLOCKS: dict[str, dict] = {}  # symbol → {sl_count, blocked_until, date}


def _is_enabled(key: str, default: bool = True) -> bool:
    try:
        from config import config_manager
        return bool(config_manager.settings.get(key, default))
    except Exception:
        return default


def record_rapid_block(symbol: str, close_reason: str, pnl: float) -> None:
    """平仓时调用：记录 SL 次数，达到阈值时标记当日 ban。"""
    if not _is_enabled("RAPID_BLOCK_ENABLED", True):
        return
    import time
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    symbol = str(symbol or "").upper()

    if symbol not in _RAPID_BLOCKS:
        _RAPID_BLOCKS[symbol] = {"sl_count": 0, "entry_bad_count": 0, "date": today}

    rb = _RAPID_BLOCKS[symbol]

    # 跨天重置（含 blocked_until，否则第二天不会重新 ban）
    if rb["date"] != today:
        rb["sl_count"] = 0
        rb["entry_bad_count"] = 0
        rb["blocked_until"] = 0.0
        rb["date"] = today

    # 统计 SL（只统计明确的止损，不统计 SL_保本/SL_软保本 等）
    _sl_reason = (close_reason or "").upper().strip()
    if _sl_reason in ("SL", "STOP_LOSS", "结构止损", "WATERFALL_STOP"):
        rb["sl_count"] += 1

    # 达到阈值 SL → ban 到次日 00:00
    try:
        from config import config_manager
        _rapid_sl_min = int(config_manager.settings.get("RAPID_BLOCK_SL_COUNT", 2) or 2)
    except Exception:
        _rapid_sl_min = 2
    if rb["sl_count"] >= _rapid_sl_min and not rb.get("blocked_until"):
        tomorrow = datetime.now().replace(hour=0, minute=0, second=0) + __import__('datetime').timedelta(days=1)
        rb["blocked_until"] = tomorrow.timestamp()
        logger.warning("⚡ [%s] 当日 %d 笔 SL，快速拦截至 %s", symbol, rb["sl_count"], tomorrow.strftime("%Y-%m-%d"))


def is_rapid_blocked(symbol: str) -> dict:
    """开仓前调用：返回 {blocked, reason}。"""
    import time
    symbol = str(symbol or "").upper()
    rb = _RAPID_BLOCKS.get(symbol)
    if not rb:
        return {"blocked": False, "reason": ""}
    blocked_until = rb.get("blocked_until", 0)
    if blocked_until and time.time() < blocked_until:
        return {"blocked": True, "reason": f"当日 {rb.get('sl_count',0)} 笔 SL，快速拦截中"}
    return {"blocked": False, "reason": ""}


# ── 方向连续亏损暂停（#2: LONG 连亏暂停 60 分钟）────────────────────────
_LONG_PAUSE: dict[str, dict] = {}  # symbol → {streak, pause_until}


def record_direction_result(symbol: str, direction: str, pnl: float) -> None:
    """平仓时调用：记录方向盈亏，2 笔 LONG 亏损后暂停 LONG 60 分钟。"""
    if not _is_enabled("LONG_PAUSE_ENABLED", True):
        return
    import time
    symbol = str(symbol or "").upper()
    direction = str(direction or "").upper()

    if direction not in ("LONG",):
        return  # 只跟踪 LONG

    if symbol not in _LONG_PAUSE:
        _LONG_PAUSE[symbol] = {"streak": 0, "pause_until": 0.0}

    lp = _LONG_PAUSE[symbol]

    # 过期清除
    if lp["pause_until"] and time.time() >= lp["pause_until"]:
        lp["streak"] = 0
        lp["pause_until"] = 0.0

    if pnl >= 0:
        # LONG 盈利 → 重置计数，解除暂停
        lp["streak"] = 0
        lp["pause_until"] = 0.0
    else:
        # LONG 亏损 → 累计
        lp["streak"] += 1
        try:
            from config import config_manager
            _loss_min = int(config_manager.settings.get("LONG_PAUSE_LOSS_COUNT", 2) or 2)
            _pause_min = int(config_manager.settings.get("LONG_PAUSE_MINUTES", 60) or 60)
        except Exception:
            _loss_min, _pause_min = 2, 60
        if lp["streak"] >= _loss_min and lp["pause_until"] == 0.0:
            lp["pause_until"] = time.time() + _pause_min * 60
            logger.warning("⚡ [%s] LONG 连续 %d 笔亏损，暂停 %d 分钟", symbol, lp["streak"], _pause_min)


def is_long_paused(symbol: str) -> dict:
    """开仓前调用：返回 {paused, until, reason}。"""
    import time
    symbol = str(symbol or "").upper()
    lp = _LONG_PAUSE.get(symbol)
    if not lp:
        return {"paused": False, "until": 0, "reason": ""}
    if lp.get("pause_until", 0) and time.time() < lp["pause_until"]:
        remain = int(lp["pause_until"] - time.time())
        return {"paused": True, "until": lp["pause_until"],
                "reason": f"LONG 连续 {lp.get('streak',0)} 笔亏损，剩余 {remain//60} 分钟"}
    return {"paused": False, "until": 0, "reason": ""}


def should_trade(symbol: str, strategy_tag: str, signal_type: str,
                 state_key: str,
                 candidate_source: str = "",
                 direction: str = "",
                 cfg: dict | None = None) -> dict:
    """判断是否允许交易。

    参数：
      symbol:          币种
      strategy_tag:    策略标签 (如 "STARTUP", "OI_EXPLOSION")
      signal_type:     信号类型 (如 "breakout", "squeeze", "continuation")
      state_key:       市场状态键 (由 market_state.classify() 生成)
      candidate_source: 候选来源 (yaobi_scanner / yaobi_shared / manual_watchlist 等)
      cfg:             配置（未使用，预留给未来扩展）

    返回：
      allow:            True/False
      reason:           原因（block 时必填）
      confidence:       判断置信度 (0~1)
      memory_score:     历史评分 (0~1)
      sample_count:     样本数
      suggested_action: "allow" | "warn" | "block"
                       allow=True 时 action 为 allow 或 warn
                       allow=False 时 action 为 block
    """
    result = {
        "allow": True,
        "reason": "",
        "confidence": 1.0,
        "memory_score": 0.0,
        "sample_count": 0,
        "suggested_action": "allow",
    }

    # ── 当日快速拦截检查 ──────────────────────────────────────────────
    try:
        _rb = is_rapid_blocked(symbol)
        if _rb.get("blocked"):
            result["allow"] = False
            result["reason"] = _rb.get("reason", "rapid_block")
            result["suggested_action"] = "block"
            _log_event(symbol, strategy_tag, state_key, 0, 0.0, "block", result["reason"])
            return result
    except Exception:
        pass

    # ── LONG 连续亏损暂停检查 ──────────────────────────────────────────
    try:
        _dir = str(direction or "").upper()
        if _dir == "LONG":
            _lp = is_long_paused(symbol)
            if _lp.get("paused"):
                result["allow"] = False
                result["reason"] = _lp.get("reason", "long_pause")
                result["suggested_action"] = "block"
                _log_event(symbol, strategy_tag, state_key, 0, 0.0, "block", result["reason"])
                return result
    except Exception:
        pass

    try:
        rule_id = f"{strategy_tag}__{signal_type}" if strategy_tag and signal_type else (strategy_tag or signal_type)

        # ── 5 级降维查询 ─────────────────────────────────────────────────
        from learning_memory import (get_rule_stats, get_rule_stats_by_source,
                                     get_rule_stats_by_source_state)
        levels = []

        # Level 1: candidate_source + symbol + state_key + rule_id
        if candidate_source:
            s = get_rule_stats_by_source(candidate_source, symbol, state_key, rule_id)
            levels.append(("L1", s.get("trades", 0), s.get("score", 0.0), s))

        # Level 2: symbol + state_key + rule_id
        s = get_rule_stats(symbol, state_key, rule_id)
        l2_n = s.get("trades", 0)
        if l2_n > 0:
            levels.append(("L2", l2_n, s.get("score", 0.0), s))

        # Level 3: candidate_source + state_key + rule_id (跨币种)
        if candidate_source:
            s = get_rule_stats_by_source_state(candidate_source, state_key, rule_id)
            l3_n = s.get("trades", 0)
            if l3_n > 0:
                levels.append(("L3", l3_n, s.get("score", 0.0), s))

        # Level 4: state_key + rule_id (跨币种跨来源)
        from learning_memory import get_state_summary
        l4_rules = get_state_summary(state_key, min_trades=1)
        l4 = next((x for x in l4_rules if x.get("rule_id") == rule_id), None)
        if l4:
            l4_n = l4.get("trades", 0)
            levels.append(("L4", l4_n, l4.get("score", 0.0), l4))

        # Level 5: rule_id only (全部状态)
        if rule_id:
            from learning_memory import get_rule_stats_by_rule
            s5 = get_rule_stats_by_rule(rule_id)
            l5_n = s5.get("trades", 0)
            if l5_n > 0:
                levels.append(("L5", l5_n, s5.get("score", 0.0), s5))

        # ── 决策：逐层检查 ──────────────────────────────────────────────
        # 先用最细粒度层做判断，细粒度不足时参考粗粒度
        best_level = levels[0] if levels else ("L0", 0, 0.0, {})
        best_name, best_n, best_score, _ = best_level

        result["sample_count"] = best_n
        result["memory_score"] = best_score

        # 冷启动：L1/L2 样本不足时，参考粗粒度做 warn 但不 block
        if best_n < COLD_START_MIN:
            # 检查粗粒度是否有大量亏损样本
            coarse_warn = False
            coarse_detail = ("", 0, 0.0)
            for lname, ln, lscore, _ in levels[1:]:
                if lname in ("L3", "L4") and ln >= 20 and lscore < BLOCK_SCORE:
                    coarse_warn = True
                    coarse_detail = (lname, ln, lscore)
                    break
            if coarse_warn:
                result["confidence"] = 0.4
                result["suggested_action"] = "warn"
                _cname, _cn, _cs = coarse_detail
                result["reason"] = (f"fine_n={best_n}<10, coarse_{_cname}_n={_cn} "
                                    f"score={_cs:.2f} — 背景亏损，建议关注")
                _log_event(symbol, strategy_tag, state_key, best_n, best_score,
                           "warn", result["reason"])
            else:
                result["confidence"] = round(best_n / COLD_START_MIN, 2)
                result["suggested_action"] = "allow"
            return result

        # warm / block 决策：按层级决定权限
        can_block = best_name in ("L1", "L2")  # 只有细粒度可以做 block
        can_warn = best_name in ("L1", "L2", "L3")
        is_low_score = best_score < BLOCK_SCORE

        if best_n >= WARM_START_MIN and is_low_score and can_block:
            # L1/L2 足量 + 低分 → block
            result["allow"] = False
            result["confidence"] = 0.8
            result["suggested_action"] = "block"
            result["reason"] = (f"{best_name}: {rule_id} score={best_score:.2f} "
                                f"n={best_n} in state={state_key}")
            _log_event(symbol, strategy_tag, state_key, best_n, best_score,
                       "block", result["reason"])
            return result

        if best_n >= COLD_START_MIN and is_low_score and can_warn:
            # L1/L2 样本够但不够 block，或 L3 低分 → warn
            result["confidence"] = 0.5
            result["suggested_action"] = "warn"
            result["reason"] = (f"{best_name}: {rule_id} score={best_score:.2f} "
                                f"n={best_n} — 低分预警")
            _log_event(symbol, strategy_tag, state_key, best_n, best_score,
                       "warn", result["reason"])
            return result

        # 粗粒度(L4/L5)低分：只记录，不 warn（纯背景参考）
        if best_name in ("L4", "L5") and is_low_score:
            result["confidence"] = 0.3
            result["suggested_action"] = "allow"
            return result

        # 正常允许
        result["confidence"] = min(0.9, 0.5 + best_score * 0.5)
        result["suggested_action"] = "allow"
        if best_n >= COLD_START_MIN:
            _log_event(symbol, strategy_tag, state_key, best_n, best_score,
                       "allow", f"score={best_score:.2f} n={best_n}")

    except Exception as e:
        logger.debug("rule_selector.should_trade error (不拦截): %s", e)
        result["reason"] = f"rule_selector_error: {e}"

    return result


def _log_event(symbol: str, strategy_tag: str, state_key: str,
               sample_count: int, memory_score: float,
               decision: str, reason: str) -> None:
    """写入决策日志。"""
    try:
        append_jsonl(EVENTS_FILE, {
            "ts": time.time(),
            "symbol": symbol,
            "strategy_tag": strategy_tag,
            "state_key": state_key,
            "sample_count": sample_count,
            "memory_score": memory_score,
            "decision": decision,
            "reason": reason,
        })
    except Exception as e:
        logger.debug("rule_selector log error: %s", e)
