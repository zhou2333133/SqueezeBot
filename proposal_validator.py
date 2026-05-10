"""
反事实提案验证器 — Evolver 参数修改的历史回溯验证。

职责：
  - 接收 Evolver 生成的 proposals
  - 用历史 trades + blocked_signals + shadow_trades 做反事实验证
  - 输出 ACCEPT / WEAK_ACCEPT / REJECT / NEED_MORE_DATA
  - 只允许 ACCEPT / WEAK_ACCEPT 进入自动应用

架构原则：
  - PAPER 和 LIVE 共用同一套验证逻辑
  - execution_backend 不能参与验证判断
"""
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from config import DATA_DIR, config_manager
from persistence import append_jsonl, read_jsonl

logger = logging.getLogger(__name__)

VALIDATION_HISTORY_FILE = os.path.join(DATA_DIR, "proposal_validation_history.jsonl")


# ══════════════════════════════════════════════════════════════════════════════
# 1. 数据加载
# ══════════════════════════════════════════════════════════════════════════════
def load_validation_dataset() -> dict:
    """加载验证所需历史数据。"""
    trades = read_jsonl(os.path.join(DATA_DIR, "strategy_trades.jsonl"))
    blocked = read_jsonl(os.path.join(DATA_DIR, "blocked_signals.jsonl"))
    shadow = read_jsonl(os.path.join(DATA_DIR, "shadow_trades.jsonl"))
    return {"trades": trades, "blocked": blocked, "shadow": shadow}


def build_counterfactual_context(trades: list[dict], blocked: list[dict], shadow: list[dict]) -> dict:
    """构建反事实上下文：按 strategy_tag + signal_id 索引。"""
    ctx = {"trades_by_tag": defaultdict(list), "blocked_by_tag": defaultdict(list), "shadow_by_tag": defaultdict(list)}
    for t in trades:
        ctx["trades_by_tag"][str(t.get("strategy_tag", "UNKNOWN"))].append(t)
    for b in blocked:
        ctx["blocked_by_tag"][str(b.get("strategy_tag", "UNKNOWN"))].append(b)
    for s in shadow:
        ctx["shadow_by_tag"][str(s.get("strategy_tag", "UNKNOWN"))].append(s)
    return ctx


# ══════════════════════════════════════════════════════════════════════════════
# 2-3. 单 proposal 验证
# ══════════════════════════════════════════════════════════════════════════════
def validate_proposal(proposal: dict, context: dict, cfg: dict) -> dict:
    """验证单个 proposal，输出 ACCEPT/REJECT/NEED_MORE_DATA。"""
    key = proposal.get("key", "")
    old_val = proposal.get("old")
    new_val = proposal.get("new")
    cfg_settings = cfg

    if key.startswith("STRATEGY_WEIGHTS."):
        return _validate_weight_proposal(proposal, context, cfg_settings)
    elif key.startswith("STRATEGY_ENABLED."):
        return _validate_enabled_proposal(proposal, context, cfg_settings)
    elif any(x in key for x in ["MIN_", "MAX_", "THRESHOLD", "LIMIT", "COOLDOWN", "_PCT", "_RATIO", "_MULT"]):
        return _validate_threshold_proposal(proposal, context, cfg_settings)
    elif any(x in key for x in ["STOP_LOSS", "TP1_", "TP2_", "TP3_", "TRAIL"]):
        return _validate_sl_tp_proposal(proposal, context, cfg_settings)
    else:
        return {"key": key, "validation_action": "NEED_MORE_DATA", "confidence": 0.0,
                "affected_count": 0, "reason": "unsupported_proposal_type"}


def validate_proposals(proposals: list[dict], context: dict, cfg: dict) -> list[dict]:
    """批量验证，返回每个 proposal 的验证结果。"""
    return [validate_proposal(p, context, cfg) for p in proposals]


# ══════════════════════════════════════════════════════════════════════════════
# 3a. STRATEGY_WEIGHTS 验证
# ══════════════════════════════════════════════════════════════════════════════
def _validate_weight_proposal(proposal: dict, context: dict, cfg: dict) -> dict:
    key = proposal["key"]
    tag_key = key.split(".")[1] if "." in key else "UNKNOWN"
    old_w = float(proposal.get("old", 1.0) or 1.0)
    new_w = float(proposal.get("new", 1.0) or 1.0)

    from strategy_policy import denormalize_strategy_tag
    cn_tag = denormalize_strategy_tag(tag_key)
    trades = context["trades_by_tag"].get(cn_tag, []) + context["trades_by_tag"].get(tag_key, [])
    shadow = context["shadow_by_tag"].get(cn_tag, []) + context["shadow_by_tag"].get(tag_key, [])
    base_required = 50.0
    filtered_wins, filtered_losses, filtered_pnl, affected = 0, 0, 0.0, 0

    for t in trades:
        raw = float(t.get("raw_score") or t.get("score") or 0)
        if raw <= 0:
            continue
        old_weighted = raw * old_w
        new_weighted = raw * new_w
        if old_weighted >= base_required and new_weighted < base_required:
            affected += 1
            pnl = float(t.get("pnl_usdt", 0) or 0)
            filtered_pnl += pnl
            if pnl >= 0:
                filtered_wins += 1
            else:
                filtered_losses += 1

    if affected >= 1:  # accept even 1 affected trade for scoring
        return _classify_validation(key, affected, filtered_wins, filtered_losses, filtered_pnl,
                                    f"weight {old_w}->{new_w}, filtered {affected} trades")

    if shadow:
        closed = [s for s in shadow if s.get("status") == "CLOSED"]
        if len(closed) >= 5:
            wins = sum(1 for s in closed if s.get("hypothetical_result") == "WIN")
            losses = len(closed) - wins
            pnl = sum(float(s.get("hypothetical_pnl_pct", 0) or 0) for s in closed)
            if losses > wins and pnl < 0:
                return {"key": key, "validation_action": "ACCEPT", "confidence": 0.6,
                        "affected_count": len(closed), "reason": f"shadow {wins}W/{losses}L pnl={pnl:.1f}%"}
            elif wins > losses and pnl > 0:
                return {"key": key, "validation_action": "REJECT", "confidence": 0.6,
                        "affected_count": len(closed), "reason": f"shadow would have won {wins}/{len(closed)}"}

    if new_w < old_w:
        tag_trades_for_pnl = [t for t in trades if isinstance(t.get("pnl_usdt"), (int, float))]
        if len(tag_trades_for_pnl) >= 10:
            total_pnl = sum(float(t.get("pnl_usdt", 0) or 0) for t in tag_trades_for_pnl)
            if total_pnl < 0:
                return {"key": key, "validation_action": "WEAK_ACCEPT", "confidence": 0.5,
                        "affected_count": len(tag_trades_for_pnl),
                        "reason": f"strategy pnl={total_pnl:.1f}<0 current weight={old_w}"}

    return {"key": key, "validation_action": "NEED_MORE_DATA", "confidence": 0.0,
            "affected_count": affected, "reason": "insufficient score data in trades"}


# ══════════════════════════════════════════════════════════════════════════════
# 3b. STRATEGY_ENABLED 验证
# ══════════════════════════════════════════════════════════════════════════════
def _validate_enabled_proposal(proposal: dict, context: dict, cfg: dict) -> dict:
    key = proposal["key"]
    tag_key = key.split(".")[1] if "." in key else "UNKNOWN"
    new_val = proposal.get("new", False)

    from strategy_policy import denormalize_strategy_tag
    cn_tag = denormalize_strategy_tag(tag_key)
    trades = context["trades_by_tag"].get(cn_tag, []) + context["trades_by_tag"].get(tag_key, [])

    if new_val is False:
        # 禁用策略 → 评估该策略历史表现
        if not trades:
            return {"key": key, "validation_action": "NEED_MORE_DATA", "confidence": 0.0,
                    "affected_count": 0, "reason": "no_trades_for_strategy"}
        total_pnl = sum(float(t.get("pnl_usdt", 0) or 0) for t in trades)
        wins = sum(1 for t in trades if float(t.get("pnl_usdt", 0) or 0) >= 0)
        n = len(trades)
        win_rate = wins / n if n > 0 else 0.0
        expectancy = total_pnl / n if n > 0 else 0.0

        if total_pnl < 0 and expectancy < 0:
            return {"key": key, "validation_action": "ACCEPT", "confidence": 0.7,
                    "affected_count": n, "would_filter_count": n,
                    "avoided_loss_count": n - wins, "missed_profit_count": wins,
                    "estimated_pnl_delta": abs(total_pnl), "reason": f"strategy pnl={total_pnl:.1f} expectancy={expectancy:.2f}"}
        elif total_pnl > 0 and expectancy > 0:
            return {"key": key, "validation_action": "REJECT", "confidence": 0.7,
                    "affected_count": n, "reason": f"strategy is profitable pnl={total_pnl:.1f}"}
        else:
            return {"key": key, "validation_action": "NEED_MORE_DATA", "confidence": 0.3,
                    "affected_count": n, "reason": f"mixed signals pnl={total_pnl:.1f}"}
    else:
        # 重新启用 → ACCEPT（启用总是安全的）
        return {"key": key, "validation_action": "ACCEPT", "confidence": 0.5,
                "affected_count": 0, "reason": "reenabling strategy is safe"}


# ══════════════════════════════════════════════════════════════════════════════
# 3c. 阈值类验证
# ══════════════════════════════════════════════════════════════════════════════
def _validate_threshold_proposal(proposal: dict, context: dict, cfg: dict) -> dict:
    key = proposal["key"]
    old_val = float(proposal.get("old", 0) or 0)
    new_val = float(proposal.get("new", 0) or 0)
    is_raising = new_val > old_val

    # 找匹配的 market_snapshot 字段
    field_map = {
        "MIN_VOL_RATIO": "volume_ratio",
        "MIN_OI_CHANGE": "oi_change",
        "TAKER_MIN": "taker_ratio",
        "MIN_PRICE_CHANGE": "price_change",
        "MAX_PRICE_CHANGE": "price_change",
        "ATR_MIN_PCT": "atr_pct",
        "ATR_MAX_PCT": "atr_pct",
        "COOLDOWN": "cooldown",
    }
    match_field = None
    for pattern, field in field_map.items():
        if pattern in key.upper():
            match_field = field
            break

    all_trades = context["trades_by_tag"].get("", [])
    for tag_trades in context["trades_by_tag"].values():
        all_trades.extend(tag_trades)

    if not match_field:
        return {"key": key, "validation_action": "NEED_MORE_DATA", "confidence": 0.0,
                "affected_count": 0, "reason": "no_matching_market_field"}

    filtered_pnl = 0.0
    filtered_wins, filtered_losses = 0, 0
    affected = 0
    missing = 0

    for t in all_trades:
        ec = t.get("entry_context", {}) or {}
        ms = ec if isinstance(ec, dict) else {}
        # 尝试多个来源
        val = float(ms.get(match_field) or t.get(match_field) or t.get(f"trigger_pct") or 0)
        if val <= 0:
            missing += 1
            continue

        if is_raising:
            if val < new_val and val >= old_val:
                affected += 1
                pnl = float(t.get("pnl_usdt", 0) or 0)
                filtered_pnl += pnl
                if pnl >= 0:
                    filtered_wins += 1
                else:
                    filtered_losses += 1
        else:
            if val > new_val and val <= old_val:
                affected += 1
                pnl = float(t.get("pnl_usdt", 0) or 0)
                filtered_pnl += pnl
                if pnl >= 0:
                    filtered_wins += 1
                else:
                    filtered_losses += 1

    return _classify_validation(key, affected, filtered_wins, filtered_losses, filtered_pnl,
                                f"field={match_field} old={old_val} new={new_val} missing={missing}")


# ══════════════════════════════════════════════════════════════════════════════
# 3d. SL/TP 类验证
# ══════════════════════════════════════════════════════════════════════════════
def _validate_sl_tp_proposal(proposal: dict, context: dict, cfg: dict) -> dict:
    key = proposal["key"]
    old_val = float(proposal.get("old", 0) or 0)
    new_val = float(proposal.get("new", 0) or 0)
    is_loosening = new_val > old_val  # 放宽 SL

    all_trades = []
    for tag_trades in context["trades_by_tag"].values():
        all_trades.extend(tag_trades)

    if is_loosening:
        stop_too_tight_count = sum(1 for t in all_trades if
                                    "stop_too_tight" in (str(t.get("failure_tags", [])) +
                                                         str(t.get("diagnosis_tags", []))))
        if stop_too_tight_count >= 5:
            return {"key": key, "validation_action": "ACCEPT", "confidence": 0.6,
                    "affected_count": stop_too_tight_count,
                    "reason": f"stop_too_tight={stop_too_tight_count} trades"}

        # 检查 mae_pct 是否在 old SL 范围内但交易仍亏损
        mae_in_range = 0
        for t in all_trades:
            mae = float(t.get("mae_pct", 0) or 0)
            if abs(mae) < old_val and float(t.get("pnl_usdt", 0) or 0) < 0:
                mae_in_range += 1
        if mae_in_range >= 5:
            return {"key": key, "validation_action": "WEAK_ACCEPT", "confidence": 0.5,
                    "affected_count": mae_in_range,
                    "reason": f"{mae_in_range} trades had MAE within old SL but still lost"}

        return {"key": key, "validation_action": "NEED_MORE_DATA", "confidence": 0.3,
                "affected_count": 0, "reason": "insufficient sl/tp data for counterfactual"}

    else:
        # 收紧 SL
        large_mae = sum(1 for t in all_trades if abs(float(t.get("mae_pct", 0) or 0)) > old_val)
        if large_mae >= 5:
            return {"key": key, "validation_action": "ACCEPT", "confidence": 0.6,
                    "affected_count": large_mae,
                    "reason": f"{large_mae} trades exceeded current SL"}
        return {"key": key, "validation_action": "NEED_MORE_DATA", "confidence": 0.3,
                "affected_count": 0, "reason": "insufficient data for tightening"}


# ══════════════════════════════════════════════════════════════════════════════
# 4. 辅助函数
# ══════════════════════════════════════════════════════════════════════════════
def _classify_validation(key: str, affected: int, filtered_wins: int, filtered_losses: int,
                          filtered_pnl: float, reason: str) -> dict:
    cfg = config_manager.settings
    min_affected = int(cfg.get("EVOLVER_COUNTERFACTUAL_MIN_AFFECTED", 5) or 5)
    min_pnl_delta = float(cfg.get("EVOLVER_COUNTERFACTUAL_ACCEPT_MIN_PNL_DELTA", 0.0) or 0.0)
    min_confidence = float(cfg.get("EVOLVER_COUNTERFACTUAL_ACCEPT_MIN_CONFIDENCE", 0.55) or 0.55)

    if affected < min_affected:
        return {"key": key, "validation_action": "NEED_MORE_DATA", "confidence": 0.0,
                "affected_count": affected, "reason": f"affected={affected}<{min_affected}"}

    avoided_losses = filtered_losses
    missed_profits = filtered_wins
    pnl_delta = abs(filtered_pnl) if filtered_pnl < 0 else -abs(filtered_pnl)
    # PnL 为负 → 这些被过滤的交易总体亏损 → 过滤正确
    if filtered_pnl < 0 and avoided_losses > missed_profits:
        confidence = min(0.9, 0.5 + avoided_losses / max(affected, 1) * 0.3)
        if pnl_delta >= min_pnl_delta and confidence >= min_confidence:
            return {"key": key, "validation_action": "ACCEPT", "confidence": round(confidence, 2),
                    "affected_count": affected, "would_filter_count": affected,
                    "avoided_loss_count": avoided_losses, "missed_profit_count": missed_profits,
                    "estimated_pnl_delta": round(abs(filtered_pnl), 4), "reason": reason}
        else:
            return {"key": key, "validation_action": "WEAK_ACCEPT", "confidence": round(confidence, 2),
                    "affected_count": affected, "reason": reason}
    # PnL 为正 → 这些被过滤的交易总体盈利 → 过滤过度
    elif filtered_pnl > 0 and missed_profits > avoided_losses:
        return {"key": key, "validation_action": "REJECT", "confidence": round(min(0.8, 0.5 + missed_profits / max(affected, 1) * 0.3), 2),
                "affected_count": affected, "reason": reason}
    else:
        return {"key": key, "validation_action": "NEED_MORE_DATA", "confidence": 0.3,
                "affected_count": affected, "reason": reason}


# ══════════════════════════════════════════════════════════════════════════════
# 5. 结果写入
# ══════════════════════════════════════════════════════════════════════════════
def write_proposal_validation_history(evolver_run_id: str, policy_version: str, results: list[dict]) -> None:
    """写入 proposal_validation_history.jsonl。"""
    try:
        record = {
            "evolver_run_id": evolver_run_id,
            "policy_version": policy_version,
            "created_at": time.time(),
            "results": results,
        }
        append_jsonl(VALIDATION_HISTORY_FILE, record)
    except Exception as e:
        logger.warning("写入 validation history 失败: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 6. 批量验证入口
# ══════════════════════════════════════════════════════════════════════════════
def counterfactual_validate_proposals(proposals: list[dict], cfg: dict | None = None) -> list[dict]:
    """批量反事实验证，返回验证结果。"""
    if cfg is None:
        cfg = config_manager.settings
    if not cfg.get("EVOLVER_COUNTERFACTUAL_VALIDATION_ENABLED", True):
        return [{"key": p.get("key", ""), "validation_action": "ACCEPT", "confidence": 1.0,
                 "reason": "counterfactual validation disabled"} for p in proposals]
    dataset = load_validation_dataset()
    context = build_counterfactual_context(**dataset)
    return validate_proposals(proposals, context, cfg)
