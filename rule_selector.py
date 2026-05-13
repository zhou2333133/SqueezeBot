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

        from learning_memory import get_rule_stats
        # 优先查按来源聚合的数据，兜底查全量
        if candidate_source:
            from learning_memory import get_rule_stats_by_source
            stats = get_rule_stats_by_source(candidate_source, symbol, state_key, rule_id)
        if not candidate_source or stats.get("trades", 0) == 0:
            stats = get_rule_stats(symbol, state_key, rule_id)
        n = stats.get("trades", 0)
        score = stats.get("score", 0.0)

        result["sample_count"] = n
        result["memory_score"] = score

        if n == 0:
            # 全新组合 → 允许但不记录事件（避免日志噪音）
            result["confidence"] = 0.3
            return result

        # ── 冷启动保护 ──────────────────────────────────────────────────
        if n < COLD_START_MIN:
            # 样本不足：只记录，不拦截
            result["confidence"] = round(n / COLD_START_MIN, 2)
            result["suggested_action"] = "allow"
            _log_event(symbol, strategy_tag, state_key, n, score, "allow",
                       f"cold_start_n={n}")
            return result

        if n < WARM_START_MIN and score < BLOCK_SCORE:
            # 弱过滤：仍 allow，但标记 warn
            result["confidence"] = 0.5
            result["suggested_action"] = "warn"
            result["reason"] = f"warm: {rule_id} score={score:.2f} n={n}"
            _log_event(symbol, strategy_tag, state_key, n, score, "warn",
                       result["reason"])
            return result

        # ── 正式判断 ─────────────────────────────────────────────────────
        if n >= WARM_START_MIN and score < BLOCK_SCORE:
            result["allow"] = False
            result["confidence"] = 0.8
            result["suggested_action"] = "block"
            result["reason"] = (f"{rule_id} score={score:.2f} < {BLOCK_SCORE} "
                                f"in state={state_key} (n={n})")
            _log_event(symbol, strategy_tag, state_key, n, score, "block",
                       result["reason"])
            return result

        # 正常允许
        result["confidence"] = min(0.9, 0.5 + score * 0.5)
        result["suggested_action"] = "allow"
        if n >= COLD_START_MIN:
            _log_event(symbol, strategy_tag, state_key, n, score, "allow",
                       f"score={score:.2f} n={n}")

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
