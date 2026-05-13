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


def should_trade(symbol: str, strategy_tag: str, signal_type: str,
                 state_key: str,
                 candidate_source: str = "",
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
