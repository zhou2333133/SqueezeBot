"""
自动策略进化引擎。

从交易记录中学习，自动调整策略参数以提升表现。

架构原则：
  - PAPER 和 LIVE 共用同一套进化后配置
  - execution_backend 只作为统计字段，不参与进化决策
  - 不允许修改 LOCKED_PARAMS（开仓金额、最大持仓数）
  - 只允许修改 PARAM_BOUNDS 中定义的参数
"""
import copy
import hashlib
import json
import logging
import os
import shutil
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR, config_manager
from persistence import append_jsonl, ensure_parent_dir, read_jsonl, safe_read_json

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────
EVOLVER_HISTORY_FILE = os.path.join(DATA_DIR, "evolver_history.jsonl")
CONFIG_BACKUP_DIR = os.path.join(DATA_DIR, "config_backups")
_ALL_STRATEGY_TAGS = ["启动型", "OI爆发", "静默建仓", "突破前夜", "早期启动"]


# ══════════════════════════════════════════════════════════════════════════════
# 1. 数据加载
# ══════════════════════════════════════════════════════════════════════════════
def load_trade_data(force: bool = False) -> list[dict]:
    """从 strategy_trades.jsonl 加载交易记录。force=True 绕过缓存。"""
    path = os.path.join(DATA_DIR, "strategy_trades.jsonl")
    return safe_read_json(path) if path.endswith(".json") else read_jsonl(path, force=force)





def load_current_config() -> dict:
    """加载当前配置快照。"""
    return dict(config_manager.settings)


# ══════════════════════════════════════════════════════════════════════════════
# 2. 策略指标统计
# ══════════════════════════════════════════════════════════════════════════════
def compute_strategy_metrics(trades: list[dict]) -> dict[str, dict]:
    """按 strategy_tag 统计策略表现。单次遍历 O(n)。"""
    by_tag: dict[str, dict] = {}
    for t in trades:
        tag = str(t.get("strategy_tag") or "")
        if not tag:
            continue
        if tag not in by_tag:
            by_tag[tag] = {"n": 0, "wins": 0, "pnl_total": 0.0, "win_pnl": 0.0, "loss_pnl": 0.0,
                           "sum_mfe": 0.0, "sum_mae": 0.0, "sum_dur": 0.0,
                           "tag_counts": defaultdict(int), "exit_reasons": defaultdict(int)}
        s = by_tag[tag]
        pnl = _f(t.get("pnl_usdt"))
        s["n"] += 1
        if pnl >= 0:
            s["wins"] += 1
        s["pnl_total"] += pnl
        if pnl >= 0:
            s["win_pnl"] += pnl
        else:
            s["loss_pnl"] += abs(pnl)
        s["sum_mfe"] += max(0, _f(t.get("mfe_pct")))
        s["sum_mae"] += min(0, _f(t.get("mae_pct")))
        s["sum_dur"] += _f(t.get("duration_sec"))
        for ft in (t.get("failure_tags") or t.get("diagnosis_tags") or []):
            if ft and ft != "good_trade":
                s["tag_counts"][ft] += 1
        r = str(t.get("close_reason") or "UNKNOWN")
        s["exit_reasons"][r] += 1

    metrics = {}
    for tag, s in by_tag.items():
        n = s["n"]
        wins = s["wins"]
        losses = n - wins
        total_pnl = s["pnl_total"]
        avg_mfe = s["sum_mfe"] / n if n > 0 else 0.0
        avg_mae = s["sum_mae"] / n if n > 0 else 0.0
        avg_dur = s["sum_dur"] / n if n > 0 else 0.0
        win_rate = wins / n if n > 0 else 0.0
        avg_win = total_pnl / wins if wins > 0 else 0.0
        avg_loss = abs(total_pnl / losses) if losses > 0 else 1.0
        expectancy = (win_rate * avg_win - (1 - win_rate) * avg_loss) if n > 0 else 0.0
        pf = s["win_pnl"] / s["loss_pnl"] if s["loss_pnl"] > 0 else (float("inf") if total_pnl > 0 else 0.0)

        metrics[tag] = {
            "total_trades": n, "wins": wins, "losses": losses,
            "win_rate": round(win_rate, 4), "pnl_total": round(total_pnl, 4),
            "avg_win": round(avg_win, 4), "avg_loss": round(avg_loss, 4),
            "expectancy": round(expectancy, 4),
            "profit_factor": round(pf, 4) if pf != float("inf") else None,
            "avg_mfe": round(avg_mfe, 4), "avg_mae": round(avg_mae, 4),
            "avg_duration_sec": round(avg_dur, 1),
            "exit_reason_distribution": dict(sorted(s["exit_reasons"].items(), key=lambda x: -x[1])),
            "failure_tags_top": [{"tag": k, "count": v} for k, v in sorted(s["tag_counts"].items(), key=lambda x: -x[1])[:5]],
        }
    return metrics


# ══════════════════════════════════════════════════════════════════════════════
# 3. 失败模式识别
# ══════════════════════════════════════════════════════════════════════════════
def detect_failure_patterns(metrics: dict[str, dict], trades: list[dict]) -> list[dict]:
    """识别各策略的失败模式。"""
    patterns: list[dict] = []
    for tag, m in metrics.items():
        if m["total_trades"] < 5:
            continue
        ft = {f["tag"]: f["count"] for f in m.get("failure_tags_top", [])}
        total = m["total_trades"]

        # 模式1: fake_breakout
        if ft.get("entry_bad", 0) / max(total, 1) > 0.2 and m["win_rate"] < 0.5:
            patterns.append({"strategy_tag": tag, "pattern": "fake_breakout",
                             "severity": "high", "detail": f"entry_bad {ft.get('entry_bad',0)}/{total}"})

        # 模式2: stop_too_tight
        if ft.get("stop_too_tight", 0) / max(total, 1) > 0.15:
            patterns.append({"strategy_tag": tag, "pattern": "stop_too_tight",
                             "severity": "medium", "detail": f"stop_too_tight {ft.get('stop_too_tight',0)}/{total}"})

        # 模式3: gave_back_profit
        if ft.get("gave_back_profit", 0) / max(total, 1) > 0.1:
            patterns.append({"strategy_tag": tag, "pattern": "gave_back_profit",
                             "severity": "medium", "detail": f"gave_back_profit {ft.get('gave_back_profit',0)}/{total}"})

        # 模式4: large_adverse_excursion
        if m["avg_mae"] < -2.0:
            patterns.append({"strategy_tag": tag, "pattern": "large_adverse_excursion",
                             "severity": "high", "detail": f"avg_mae={m['avg_mae']}%"})

        # 模式5: direction_wrong
        if ft.get("direction_wrong", 0) / max(total, 1) > 0.15:
            patterns.append({"strategy_tag": tag, "pattern": "direction_wrong",
                             "severity": "high", "detail": f"direction_wrong {ft.get('direction_wrong',0)}/{total}"})

        # 模式6: low_win_rate
        if m["win_rate"] < 0.35 and total >= 20:
            patterns.append({"strategy_tag": tag, "pattern": "low_win_rate",
                             "severity": "critical", "detail": f"win_rate={m['win_rate']}"})
    return patterns


# ══════════════════════════════════════════════════════════════════════════════
# 4. 参数修改建议生成
# ══════════════════════════════════════════════════════════════════════════════
def propose_param_updates(metrics: dict[str, dict], patterns: list[dict], current_config: dict) -> list[dict]:
    """根据统计和失败模式生成参数修改方案。"""
    proposals: list[dict] = []
    cfg = current_config

    # 策略权重/启停 proposals
    for tag, m in metrics.items():
        eng_key = _tag_key(tag)
        weight_key = f"STRATEGY_WEIGHTS.{eng_key}"
        enabled_key = f"STRATEGY_ENABLED.{eng_key}"
        if m["total_trades"] < int(cfg.get("EVOLVER_MIN_TRADES_PER_STRATEGY", 20) or 20):
            continue
        wr = m["win_rate"]
        exp = m["expectancy"]
        disable_min = int(cfg.get("EVOLVER_DISABLE_STRATEGY_MIN_TRADES", 30) or 30)
        disable_wr = float(cfg.get("EVOLVER_DISABLE_STRATEGY_WIN_RATE_BELOW", 0.30) or 0.30)
        disable_exp = float(cfg.get("EVOLVER_DISABLE_STRATEGY_EXPECTANCY_BELOW", 0.0) or 0.0)
        down_wr = float(cfg.get("EVOLVER_DOWN_WEIGHT_WIN_RATE_BELOW", 0.45) or 0.45)
        down_exp = float(cfg.get("EVOLVER_DOWN_WEIGHT_EXPECTANCY_BELOW", 0.0) or 0.0)
        up_wr = float(cfg.get("EVOLVER_UP_WEIGHT_WIN_RATE_ABOVE", 0.58) or 0.58)
        up_exp = float(cfg.get("EVOLVER_UP_WEIGHT_EXPECTANCY_ABOVE", 0.0) or 0.0)
        # 禁用
        if m["total_trades"] >= disable_min and wr < disable_wr and exp < disable_exp:
            if not _is_last_enabled_strategy(eng_key, cfg):
                proposals.append({"key": enabled_key, "old": True, "new": False,
                                  "reason": f"win_rate={wr:.2f}<{disable_wr} expectancy={exp:.2f}<{disable_exp}",
                                  "strategy_tag": tag})
        # 降权
        elif wr < down_wr and exp < down_exp:
            current_w = cfg.get(weight_key, 1.0)
            new_w = max(0.1, float(current_w) - 0.2)
            if abs(new_w - float(current_w)) > 0.01:
                proposals.append({"key": weight_key, "old": current_w, "new": new_w,
                                  "reason": f"win_rate={wr:.2f}<{down_wr} 降权", "strategy_tag": tag})
        # 升权
        elif wr >= up_wr and exp >= up_exp and eng_key != "UNKNOWN":
            current_w = cfg.get(weight_key, 1.0)
            new_w = min(2.0, float(current_w) + 0.2)
            if abs(new_w - float(current_w)) > 0.01:
                proposals.append({"key": weight_key, "old": current_w, "new": new_w,
                                  "reason": f"win_rate={wr:.2f}>={up_wr} 升权", "strategy_tag": tag})

    # 参数冷却检查：排除冷却中的参数
    try:
        from param_attribution import is_param_in_cooldown
        filtered = []
        for prop in proposals:
            key = prop.get("key", "")
            in_cd, _ = is_param_in_cooldown(key)
            if in_cd:
                prop["rejected_reason"] = "PARAM_COOLDOWN"
                continue
            filtered.append(prop)
        proposals = filtered
    except Exception:
        pass

    # shadow outcome 调权
    proposals = _apply_shadow_to_proposals(proposals, metrics, cfg)

    for p in patterns:
        tag = p["strategy_tag"]
        pattern = p["pattern"]
        m = metrics.get(tag, {})

        # PP1: fake_breakout → 提高 VOL/OI 阈值
        if pattern == "fake_breakout":
            for key, increase_by in [
                (f"STRATEGY_{_tag_key(tag)}_MIN_VOL_RATIO", 0.2),
                (f"STRATEGY_{_tag_key(tag)}_MIN_OI_CHANGE_15M", 0.3),
            ]:
                if key in cfg:
                    proposals.append(_make_proposal(key, cfg[key], cfg[key] + increase_by,
                                                     f"fake_breakout 占比高", tag))
                elif _tag_key(tag) in ["启动型", "早期启动"]:
                    proposals.append(_make_proposal(f"STRATEGY_{_tag_key(tag)}_MIN_PRICE_CHANGE_15M",
                                                     cfg.get(f"STRATEGY_{_tag_key(tag)}_MIN_PRICE_CHANGE_15M", 0.3),
                                                     cfg.get(f"STRATEGY_{_tag_key(tag)}_MIN_PRICE_CHANGE_15M", 0.3) + 0.1,
                                                     f"fake_breakout 占比高", tag))

        # PP2: stop_too_tight → 放宽 SL
        if pattern == "stop_too_tight":
            current_sl = cfg.get("SCALP_STOP_LOSS_PCT", 50.0)
            proposals.append(_make_proposal("SCALP_STOP_LOSS_PCT", current_sl, current_sl * 1.1,
                                             "stop_too_tight 过多，放宽SL", tag))

        # PP3: gave_back_profit → 收紧 trailing
        if pattern == "gave_back_profit":
            current_trail = cfg.get("SCALP_TP3_TRAIL_PCT", 8.0)
            proposals.append(_make_proposal("SCALP_TP3_TRAIL_PCT", current_trail, current_trail * 0.85,
                                             "利润回吐过多，收紧trailing", tag))

        # PP4: large_adverse_excursion → 提高入场确认
        if pattern == "large_adverse_excursion":
            for key, increase_by in [
                ("BREAKOUT_MIN_VOL_RATIO", 0.1),
                ("BREAKOUT_TAKER_MIN", 0.02),
            ]:
                if key in cfg:
                    proposals.append(_make_proposal(key, cfg[key], cfg[key] + increase_by,
                                                     f"large_adverse_excursion 改善入场", tag))

        # PP5: direction_wrong → 降权
        if pattern == "direction_wrong":
            weight_key = f"STRATEGY_WEIGHT_{_tag_key(tag)}"
            if weight_key in cfg:
                new_weight = max(0.1, cfg[weight_key] - 0.2)
                proposals.append(_make_proposal(weight_key, cfg[weight_key], new_weight,
                                                 f"direction_wrong 降权", tag))

    # 全局：胜率高策略适当升权
    for tag, m in metrics.items():
        if m["total_trades"] < 20:
            continue
        weight_key = f"STRATEGY_WEIGHT_{_tag_key(tag)}"
        if weight_key not in cfg:
            continue
        if m["win_rate"] >= 0.58 and m["expectancy"] > 0:
            new_weight = min(2.0, cfg[weight_key] + 0.1)
            if new_weight != cfg[weight_key]:
                proposals.append(_make_proposal(weight_key, cfg[weight_key], new_weight,
                                                 f"胜率{m['win_rate']:.0%}升权", tag))

    return proposals


def _tag_key(tag: str) -> str:
    """策略标签 → 配置 key 前缀（统一英文）。"""
    mapping = {"启动型": "STARTUP", "OI爆发": "OI_EXPLOSION", "静默建仓": "QUIET_ACCUM",
               "突破前夜": "PRE_BREAKOUT", "早期启动": "EARLY_START"}
    tag_up = tag.upper().strip()
    if tag_up in ("STARTUP", "OI_EXPLOSION", "QUIET_ACCUM", "PRE_BREAKOUT", "EARLY_START", "UNKNOWN"):
        return tag_up
    return mapping.get(tag, tag)


def _make_proposal(key: str, old_val: Any, new_val: Any, reason: str, strategy_tag: str = "") -> dict:
    return {
        "key": key,
        "old": old_val,
        "new": new_val,
        "reason": reason,
        "strategy_tag": strategy_tag,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5. 校验
# ══════════════════════════════════════════════════════════════════════════════
def validate_param_updates(proposals: list[dict], current_config: dict) -> tuple[list[dict], list[dict]]:
    """校验 proposals，返回 (valid, rejected)。委托 risk_guard 执行硬风控。"""
    # 首先检查原始 LOCKED_PARAMS（保持向后兼容）
    locked = getattr(config_manager, "LOCKED_PARAMS", set())
    rejected_prefix = []
    remaining = []
    for prop in proposals:
        key = prop.get("key", "")
        if key in locked:
            rejected_prefix.append({**prop, "rejected_reason": "LOCKED_PARAM"})
        else:
            remaining.append(prop)
    from risk_guard import check_proposals
    valid, rejected = check_proposals(remaining)
    rejected = rejected_prefix + rejected

    max_change_pct = float(current_config.get("EVOLVER_MAX_PARAM_CHANGE_PCT", 0.30) or 0.30)
    max_updates = int(current_config.get("EVOLVER_MAX_UPDATES_PER_RUN", 5) or 5)
    bounds = config_manager.PARAM_BOUNDS
    filtered = []

    for prop in valid:
        key = prop["key"]
        old_v = prop["old"]
        new_v = prop["new"]
        # 检查 INEFFECTIVE_PARAMS
        if hasattr(config_manager, "INEFFECTIVE_PARAMS") and key in config_manager.INEFFECTIVE_PARAMS:
            rejected.append({**prop, "rejected_reason": "INEFFECTIVE_PARAM"})
            continue
        # 检查 PARAM_BOUNDS
        if key not in bounds:
            rejected.append({**prop, "rejected_reason": "NO_BOUNDS"})
            continue
        lo, hi = bounds[key]
        pct_change = abs(new_v - old_v) / abs(old_v) if old_v != 0 else 1.0
        if pct_change > max_change_pct:
            max_delta = abs(old_v) * max_change_pct
            new_v = old_v + max_delta if new_v > old_v else old_v - max_delta
            prop["new"] = new_v
            prop["clamped"] = True
        new_v = max(lo, min(hi, new_v))
        prop["new"] = new_v
        if abs(new_v - old_v) < 0.001:
            continue
        filtered.append(prop)

    return filtered[:max_updates], rejected


# ══════════════════════════════════════════════════════════════════════════════
# 6. 配置备份
# ══════════════════════════════════════════════════════════════════════════════
def backup_config() -> str:
    """备份当前配置到 config_backups/ 目录。"""
    ensure_parent_dir(os.path.join(CONFIG_BACKUP_DIR, ".keep"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(CONFIG_BACKUP_DIR, f"settings_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(config_manager.settings), f, ensure_ascii=False, indent=2, default=str)
    logger.info("配置已备份到 %s", path)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# 7. 应用参数修改
# ══════════════════════════════════════════════════════════════════════════════
def apply_param_updates(valid_updates: list[dict]) -> list[dict]:
    """已弃用：请使用 risk_guard.apply_proposals()。

    此函数直接写 config_manager.settings + _persist()，绕过 risk_guard 风控。
    保留仅用于向后兼容，新代码必须走 risk_guard。
    """
    logger.warning("⚠️ apply_param_updates 已弃用，调用方应改用 risk_guard.apply_proposals()")
    try:
        from risk_guard import apply_proposals
        applied, _ = apply_proposals(valid_updates)
        return applied
    except Exception as e:
        logger.error("apply_param_updates 委托 risk_guard 失败: %s", e)
        # 兜底：不允许绕过风险直接写（安全失败）
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 8. policy_version 生成
# ══════════════════════════════════════════════════════════════════════════════
def write_policy_version() -> str:
    """生成新的 policy_version。"""
    prefix = str(config_manager.settings.get("EVOLVER_POLICY_VERSION_PREFIX", "auto"))
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    seq = _next_policy_seq()
    version = f"{prefix}-{ts}-{seq:03d}"
    config_manager.settings["POLICY_VERSION"] = version
    return version


def _next_policy_seq() -> int:
    """从历史记录中找下一个序号。"""
    records = read_jsonl(EVOLVER_HISTORY_FILE)
    max_seq = 0
    for r in records:
        ver = str(r.get("policy_version", ""))
        try:
            seq = int(ver.split("-")[-1])
            max_seq = max(max_seq, seq)
        except (ValueError, IndexError):
            pass
    return max_seq + 1


# ══════════════════════════════════════════════════════════════════════════════
# 9. 历史记录
# ══════════════════════════════════════════════════════════════════════════════
def append_evolver_history(record: dict) -> None:
    """记录一次进化到 evolver_history.jsonl。"""
    try:
        append_jsonl(EVOLVER_HISTORY_FILE, record)
    except Exception as e:
        logger.warning("写入 evolver_history 失败: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 10. 回滚
# ══════════════════════════════════════════════════════════════════════════════
def rollback_last_policy() -> dict[str, Any]:
    """回滚到上一版本的配置备份。"""
    if not os.path.exists(CONFIG_BACKUP_DIR):
        return {"status": "error", "message": "无备份目录"}
    backups = sorted([f for f in os.listdir(CONFIG_BACKUP_DIR) if f.endswith(".json")])
    if len(backups) < 1:
        return {"status": "error", "message": "无可用备份"}
    latest = os.path.join(CONFIG_BACKUP_DIR, backups[-1])
    try:
        with open(latest, "r", encoding="utf-8") as f:
            backup_data = json.load(f)
        # 恢复配置
        for k, v in backup_data.items():
            if k in config_manager.settings:
                config_manager.settings[k] = v
        config_manager._persist()
        logger.warning("配置已回滚到 %s", latest)
        return {"status": "success", "backup": latest, "keys_restored": len(backup_data)}
    except Exception as e:
        return {"status": "error", "message": f"回滚失败: {e}"}


# ══════════════════════════════════════════════════════════════════════════════
# 11. 完整进化流程
# ══════════════════════════════════════════════════════════════════════════════
def run_evolution_once() -> dict[str, Any]:
    """执行完整自动进化流程。返回本次进化结果。"""
    cfg = config_manager.settings
    result: dict[str, Any] = {
        "policy_version": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "auto_applied": False,
        "metrics_summary": {},
        "patterns": [],
        "applied_updates": [],
        "rejected_updates": [],
        "config_backup": "",
        "status": "skipped",
        "message": "",
    }

    try:
        # 检查是否启用
        if not cfg.get("EVOLVER_ENABLED", True):
            result["message"] = "EVOLVER_ENABLED=False"
            return result

        auto_apply = cfg.get("EVOLVER_AUTO_APPLY", True)
        min_total = int(cfg.get("EVOLVER_MIN_TOTAL_TRADES", 50) or 50)
        min_per_strategy = int(cfg.get("EVOLVER_MIN_TRADES_PER_STRATEGY", 20) or 20)

        # 1. 加载数据
        trades = load_trade_data(force=True)
        if len(trades) < min_total:
            result["message"] = f"样本不足 {len(trades)}<{min_total}"
            return result

        # 2. 计算指标
        metrics = compute_strategy_metrics(trades)
        result["metrics_summary"] = {tag: {k: v for k, v in m.items()
                                           if k in ("total_trades", "win_rate", "pnl_total", "expectancy")}
                                      for tag, m in metrics.items()}

        # 检查各策略样本是否足够
        has_enough = False
        for m in metrics.values():
            if m["total_trades"] >= min_per_strategy:
                has_enough = True
                break
        if not has_enough:
            result["message"] = f"无策略达到最少样本 {min_per_strategy}"
            return result

        # 3. 识别失败模式
        patterns = detect_failure_patterns(metrics, trades)
        result["patterns"] = patterns

        # 4. 生成 proposals
        current_config = load_current_config()
        proposals = propose_param_updates(metrics, patterns, current_config)
        if not proposals:
            result["message"] = "无参数修改建议"
            return result

        # 5. risk_guard 审核 + 写入（Evolver 只提案，Guard 批准）
        from risk_guard import apply_proposals
        applied, rejected = apply_proposals(proposals)
        result["rejected_updates"] = rejected
        result["applied_updates"] = applied

        if not applied:
            result["message"] = f"所有 proposal 被拒绝 ({len(rejected)} rejected)"
            return result

        # 6. 反事实验证
        try:
            from deprecated.proposal_validator import counterfactual_validate_proposals, write_proposal_validation_history
            cv_results = counterfactual_validate_proposals(applied, cfg)
            cv_accepted = [r for r in cv_results if r.get("validation_action") in ("ACCEPT", "WEAK_ACCEPT")]
            cv_rejected = [r for r in cv_results if r.get("validation_action") in ("REJECT", "NEED_MORE_DATA")]
            # 只保留 ACCEPT / WEAK_ACCEPT（用于记录，已由 risk_guard 写入）
            accepted_keys = {r["key"] for r in cv_accepted}
            applied = [v for v in applied if v["key"] in accepted_keys]
            for r in cv_rejected:
                rejected.append({"key": r["key"], "rejected_reason": f"COUNTERFACTUAL_{r['validation_action']}",
                                 "reason": r.get("reason", "")})
            result["counterfactual_results"] = {"accepted": len(cv_accepted), "rejected": len(cv_rejected)}
            try:
                write_proposal_validation_history(
                    result.get("evolver_run_id", ""), result.get("policy_version", ""), cv_results)
            except Exception:
                pass
        except Exception as e:
            logger.debug("反事实验证异常 (不影响): %s", e)

        # 6. 备份
        if cfg.get("EVOLVER_BACKUP_ENABLED", True):
            backup_path = backup_config()
            result["config_backup"] = backup_path

        # 7. 应用（已由 risk_guard 在第5步写入，此处仅记录）
        if auto_apply and applied:
            result["auto_applied"] = True

            # 8. 更新 policy_version
            version = write_policy_version()
            result["policy_version"] = version

            # 持久化
            config_manager._persist()
        else:
            result["applied_updates"] = applied
            result["message"] = "EVOLVER_AUTO_APPLY=False，仅生成建议未应用"
            return result

        # 9. 创建参数补丁
        try:
            from param_attribution import create_param_patches, start_tracking
            applied = result.get("applied_updates", [])
            version = result.get("policy_version", "")
            pid_list = create_param_patches(applied, version, result.get("evolver_run_id", ""))
            result["param_patches_created"] = len(pid_list)
            # 因果回溯：追踪每个补丁后续交易表现
            for i, update in enumerate(applied):
                if i < len(pid_list):
                    start_tracking(pid_list[i], update.get("key", ""),
                                   update.get("old"), update.get("new"),
                                   update.get("strategy_tag", ""))
        except Exception as e:
            logger.debug("创建 param_patches 失败: %s", e)
        # 参数补丁回滚标记
        try:
            from param_attribution import mark_patches_reverted_by_policy
            old_ver = result.get("previous_policy", "")
            if old_ver:
                rc = mark_patches_reverted_by_policy(old_ver, version)
                if rc:
                    logger.info("回滚标记 %d 个补丁", rc)
        except Exception:
            pass
        # 10. 记录历史
        append_evolver_history(result)
        result["status"] = "applied"
        result["message"] = f"已应用 {len(result['applied_updates'])} 个参数修改"
        return result

    except Exception as e:
        logger.error("Evolver 异常: %s", e, exc_info=True)
        result["status"] = "error"
        result["message"] = str(e)
        return result


# ══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════════════════

def _is_last_enabled_strategy(eng_key: str, cfg: dict) -> bool:
    """检查是否只剩这一个启用策略。"""
    try:
        from strategy_policy import get_enabled_strategies
        enabled = get_enabled_strategies(cfg)
        return len(enabled) == 1 and eng_key in enabled
    except Exception:
        return False


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


import hashlib
import json
import os
import time
from datetime import datetime, timezone


EVOLVER_STATE_FILE = os.path.join(DATA_DIR, "evolver_state.json")
POLICY_PERF_FILE = os.path.join(DATA_DIR, "policy_performance.jsonl")


def compute_config_hash(cfg=None):
    if cfg is None:
        cfg = config_manager.settings
    relevant = {}
    for key in sorted(config_manager.PARAM_BOUNDS.keys()):
        if key in cfg:
            relevant[key] = cfg[key]
    raw = json.dumps(relevant, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _default_evolver_state():
    return {
        "current_policy_version": str(config_manager.settings.get("POLICY_VERSION", "manual-v1")),
        "current_config_hash": compute_config_hash(),
        "previous_policy_version": "",
        "previous_config_hash": "",
        "last_evolver_run_at": 0.0,
        "last_evolver_run_id": "",
        "trades_since_last_policy": 0,
        "closed_trades_since_last_policy": 0,
        "pending_evaluation": False,
        "evaluation_min_trades": 30,
        "evaluation_started_at": 0.0,
        "rollback_count": 0,
        "last_rollback_at": None,
        "freeze_until": 0.0,
        "daily_rollback_count": 0,
        "last_daily_reset": 0.0,
    }


def _get_evolver_state():
    state = safe_read_json(EVOLVER_STATE_FILE)
    if not isinstance(state, dict):
        state = {}
    defaults = _default_evolver_state()
    defaults.update(state)
    return defaults


def _save_evolver_state(state):
    from persistence import atomic_write_json
    try:
        atomic_write_json(EVOLVER_STATE_FILE, state)
    except Exception as e:
        logger.warning("写入 evolver_state 失败: %s", e)


def update_evolver_state_after_trade(trade):
    try:
        state = _get_evolver_state()
        state["trades_since_last_policy"] = state.get("trades_since_last_policy", 0) + 1
        state["closed_trades_since_last_policy"] = state.get("closed_trades_since_last_policy", 0) + 1
        _save_evolver_state(state)
    except Exception as e:
        logger.debug("更新 evolver state 失败: %s", e)


def compute_policy_performance(policy_version, trades):
    ptrades = [t for t in trades if str(t.get("policy_version", "")).strip() == policy_version.strip()]
    if not ptrades:
        return {"policy_version": policy_version, "trades": 0, "decision": "NO_DATA"}
    n = len(ptrades)
    wins = [t for t in ptrades if _f(t.get("pnl_usdt")) >= 0]
    pnls = [_f(t.get("pnl_usdt")) for t in ptrades]
    win_pnls = [_f(t.get("pnl_usdt")) for t in wins]
    loss_pnls = [abs(_f(t.get("pnl_usdt"))) for t in ptrades if _f(t.get("pnl_usdt")) < 0]
    total_pnl = sum(pnls)
    win_rate = len(wins) / n if n > 0 else 0.0
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 1.0
    expectancy = (win_rate * avg_win - (1 - win_rate) * avg_loss) if n > 0 else 0.0
    pf = sum(win_pnls) / sum(loss_pnls) if sum(loss_pnls) > 0 else (float("inf") if total_pnl > 0 else 0.0)
    cum, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    mfes = [_f(t.get("mfe_pct")) for t in ptrades]
    maes = [_f(t.get("mae_pct")) for t in ptrades]
    return {
        "policy_version": policy_version,
        "config_hash": compute_config_hash(),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "trades": n, "win_rate": round(win_rate, 4), "pnl_total": round(total_pnl, 4),
        "expectancy": round(expectancy, 4),
        "profit_factor": round(pf, 4) if pf != float("inf") else None,
        "max_drawdown": round(dd, 2),
        "avg_mfe": round(sum(mfes) / n, 4) if n else 0.0,
        "avg_mae": round(sum(maes) / n, 4) if n else 0.0,
    }


def evaluate_current_policy(trades=None):
    state = _get_evolver_state()
    cfg = config_manager.settings
    cur_ver = str(state.get("current_policy_version", ""))
    pre_ver = str(state.get("previous_policy_version", ""))
    if not cur_ver:
        return {"decision": "NO_POLICY"}
    if trades is None:
        from strategy_evolver import load_trade_data
        trades = load_trade_data(force=True)
    cur_perf = compute_policy_performance(cur_ver, trades)
    cur_trades = cur_perf.get("trades", 0)
    min_trades = int(cfg.get("EVOLVER_EVAL_MIN_TRADES", 30) or 30)
    if cur_trades < min_trades:
        return {"decision": "PENDING", "trades": cur_trades, "need": min_trades}
    pre_perf = compute_policy_performance(pre_ver, trades) if pre_ver else None
    pre_wr = pre_perf.get("win_rate", 0.5) if pre_perf else 0.5
    cur_wr = cur_perf.get("win_rate", 0.0)
    cur_exp = cur_perf.get("expectancy", 0.0)
    cur_pnl = cur_perf.get("pnl_total", 0.0)
    cur_dd = cur_perf.get("max_drawdown", 0.0)
    rb_exp = float(cfg.get("EVOLVER_ROLLBACK_IF_EXPECTANCY_BELOW", 0.0) or 0.0)
    rb_pnl = float(cfg.get("EVOLVER_ROLLBACK_IF_PNL_BELOW", 0.0) or 0.0)
    rb_wr = float(cfg.get("EVOLVER_ROLLBACK_IF_WIN_RATE_DROP_PCT", 0.15) or 0.15)
    rb_dd = float(cfg.get("EVOLVER_ROLLBACK_IF_MAX_DRAWDOWN_ABOVE", 50.0) or 50.0)
    reasons = []
    if cur_exp < rb_exp:
        reasons.append(f"exp={cur_exp}<{rb_exp}")
    if cur_pnl < rb_pnl:
        reasons.append(f"pnl={cur_pnl}<{rb_pnl}")
    if pre_ver and pre_wr - cur_wr > rb_wr:
        reasons.append(f"wr_drop={pre_wr-cur_wr:.2f}>{rb_wr}")
    if cur_dd < -rb_dd:
        reasons.append(f"dd={cur_dd:.1f}%<{-rb_dd}%")
    if reasons:
        return {"decision": "ROLLBACK", "reasons": reasons, "perf": cur_perf, "prev_perf": pre_perf}
    elif cur_exp <= 0 or cur_pnl <= 0:
        return {"decision": "ADJUST", "reasons": ["mediocre"], "perf": cur_perf, "prev_perf": pre_perf}
    else:
        return {"decision": "KEEP", "reasons": ["ok"], "perf": cur_perf, "prev_perf": pre_perf}


def maybe_auto_rollback(trades=None):
    result = {"event_type": "SKIP", "rolled_back": False, "reason": "", "new_version": ""}
    cfg = config_manager.settings
    if not cfg.get("EVOLVER_AUTO_ROLLBACK_ENABLED", True):
        result["reason"] = "AUTO_ROLLBACK_DISABLED"; return result
    state = _get_evolver_state()
    now = time.time()
    if now < state.get("freeze_until", 0.0):
        result["reason"] = f"frozen_{state['freeze_until']-now:.0f}s"; return result
    daily_max = int(cfg.get("EVOLVER_MAX_ROLLBACKS_PER_DAY", 3) or 3)
    if now - state.get("last_daily_reset", 0.0) > 86400:
        state["daily_rollback_count"] = 0; state["last_daily_reset"] = now
    if state.get("daily_rollback_count", 0) >= daily_max:
        result["reason"] = f"daily_limit_{daily_max}"; return result
    ev = evaluate_current_policy(trades)
    if ev.get("decision") != "ROLLBACK":
        result["reason"] = f"decision={ev.get('decision')}"; return result
    rb = rollback_last_policy()
    if rb.get("status") != "success":
        result["reason"] = f"rb_fail:{rb.get('message')}"; return result
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    new_ver = f"rollback-auto-{ts}"
    state["previous_policy_version"] = state.get("current_policy_version", "")
    state["current_policy_version"] = new_ver
    state["current_config_hash"] = compute_config_hash()
    state["rollback_count"] = state.get("rollback_count", 0) + 1
    state["last_rollback_at"] = now
    state["daily_rollback_count"] = state.get("daily_rollback_count", 0) + 1
    state["trades_since_last_policy"] = 0
    state["pending_evaluation"] = False
    freeze_min = float(cfg.get("EVOLVER_FREEZE_AFTER_ROLLBACK_MINUTES", 120) or 120)
    state["freeze_until"] = now + freeze_min * 60
    config_manager.settings["POLICY_VERSION"] = new_ver
    config_manager._persist()
    _save_evolver_state(state)
    evt = _build_evolver_event("ROLLBACK", state, {"reason": "; ".join(ev.get("reasons", [])), "perf": ev.get("perf")})
    append_evolver_history(evt)
    if ev.get("perf"):
        try:
            append_jsonl(POLICY_PERF_FILE, {**ev["perf"], "decision": "ROLLBACK", "reason": "; ".join(ev.get("reasons", []))})
        except Exception:
            pass
    result["event_type"] = "ROLLBACK"; result["rolled_back"] = True
    result["reason"] = "; ".join(ev.get("reasons", [])); result["new_version"] = new_ver
    logger.warning("Evolver auto rollback: %s", result["reason"])
    return result


def _build_evolver_event(event_type, state, extra=None):
    evt = {
        "event_type": event_type,
        "evolver_run_id": f"evo-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "policy_version": state.get("current_policy_version", ""),
        "previous_policy_version": state.get("previous_policy_version", ""),
        "config_hash": state.get("current_config_hash", ""),
        "previous_config_hash": state.get("previous_config_hash", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "state_snapshot": {k: v for k, v in state.items() if k != "state_snapshot"},
    }
    if extra:
        evt.update(extra)
    return evt


def run_evolution_auto():
    """Enhanced trigger: checks state/pending/freeze before evolving."""
    result = {"event_type": "SKIP", "reason": ""}
    try:
        cfg = config_manager.settings
        if not cfg.get("EVOLVER_ENABLED", True):
            result["reason"] = "disabled"; return result
        state = _get_evolver_state()
        now = time.time()
        if now < state.get("freeze_until", 0.0):
            result["reason"] = "frozen"; return result
        daily_max = int(cfg.get("EVOLVER_MAX_ROLLBACKS_PER_DAY", 3) or 3)
        if now - state.get("last_daily_reset", 0.0) > 86400:
            state["daily_rollback_count"] = 0; state["last_daily_reset"] = now
        if state.get("daily_rollback_count", 0) >= daily_max:
            result["reason"] = "daily_limit"; return result
        # Check pending evaluation
        if state.get("pending_evaluation", False):
            from strategy_evolver import load_trade_data as ltd
            ev = evaluate_current_policy(ltd())
            if ev.get("decision") == "ROLLBACK":
                return maybe_auto_rollback(ltd())
            elif ev.get("decision") == "KEEP":
                state["pending_evaluation"] = False; _save_evolver_state(state)
            else:
                result["reason"] = f"pending:{ev.get('decision')}"; return result
        # Cooldown
        last_run = state.get("last_evolver_run_at", 0.0)
        min_int = float(cfg.get("EVOLVER_MIN_MINUTES_BETWEEN_UPDATES", 60) or 60)
        if now - last_run < min_int * 60:
            result["reason"] = "cooldown"; return result
        min_tr = int(cfg.get("EVOLVER_MIN_TRADES_BETWEEN_UPDATES", 30) or 30)
        if state.get("trades_since_last_policy", 0) < min_tr:
            result["reason"] = f"need_{min_tr}_trades"; return result
        # Evolve
        evo = run_evolution_once()
        if evo.get("status") == "applied":
            state["current_policy_version"] = evo.get("policy_version", "")
            state["current_config_hash"] = compute_config_hash()
            state["last_evolver_run_at"] = now
            state["last_evolver_run_id"] = f"evo-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            state["trades_since_last_policy"] = 0
            state["pending_evaluation"] = True
            state["evaluation_started_at"] = now
            _save_evolver_state(state)
            result["event_type"] = "APPLY"
            result["reason"] = evo.get("message", "")
            result["applied_updates"] = evo.get("applied_updates", [])
        else:
            result["reason"] = evo.get("message", "no_changes")
        return result
    except Exception as e:
        logger.error("Evolver auto error: %s", e, exc_info=True)
        result["reason"] = f"exception:{e}"
        return result


# ══════════════════════════════════════════════════════════════════════════════
# 17. blocked_signals 读取 + 过度过滤保护
# ══════════════════════════════════════════════════════════════════════════════
def load_blocked_signals() -> list[dict]:
    """读取 blocked_signals.jsonl。"""
    path = os.path.join(DATA_DIR, "blocked_signals.jsonl")
    return read_jsonl(path)


def compute_blocked_signal_stats(blocked: list[dict]) -> dict[str, dict]:
    """按 strategy_tag 统计被拦截信号。"""
    by_tag: dict[str, dict] = {}
    for b in blocked:
        tag = str(b.get("strategy_tag", "UNKNOWN"))
        if tag not in by_tag:
            by_tag[tag] = {"blocked_count": 0, "reasons": {}}
        by_tag[tag]["blocked_count"] += 1
        reason = str(b.get("blocked_reason", "UNKNOWN"))
        by_tag[tag]["reasons"][reason] = by_tag[tag]["reasons"].get(reason, 0) + 1
    return by_tag


def _check_over_filter_protection(state: dict, trades: list[dict], blocked: list[dict],
                                   cfg: dict) -> list[dict]:
    """检查是否过度过滤，如果是则生成恢复 proposal。"""
    proposals = []
    min_trades = int(cfg.get("EVOLVER_MIN_TRADES_AFTER_POLICY", 5) or 5)
    no_trade_minutes = float(cfg.get("EVOLVER_REENABLE_IF_NO_TRADES_MINUTES", 180) or 180)
    reenable_weight = float(cfg.get("EVOLVER_REENABLE_WEIGHT", 0.5) or 0.5)
    min_enabled = int(cfg.get("EVOLVER_MIN_ENABLED_STRATEGIES", 2) or 2)

    # 检查：新 policy 后是否交易太少
    since_policy = state.get("trades_since_last_policy", 0)
    policy_age = time.time() - state.get("evaluation_started_at", state.get("last_evolver_run_at", 0))
    if since_policy < min_trades and policy_age > no_trade_minutes * 60:
        # 可能是过度过滤
        blocked_stats = compute_blocked_signal_stats(blocked)
        total_blocked = sum(s["blocked_count"] for s in blocked_stats.values())
        if total_blocked > since_policy * 2 and since_policy < 5:
            # 恢复一个被禁用策略
            from strategy_policy import get_enabled_strategies, normalize_strategy_tag
            enabled = get_enabled_strategies(cfg)
            if len(enabled) < min_enabled:
                for tag_key in ["STARTUP", "OI_EXPLOSION", "QUIET_ACCUM", "PRE_BREAKOUT", "EARLY_START"]:
                    ekey = f"STRATEGY_ENABLED.{tag_key}"
                    wkey = f"STRATEGY_WEIGHTS.{tag_key}"
                    if not cfg.get(ekey, True) is False:
                        proposals.append({"key": ekey, "old": False, "new": True,
                                          "reason": "over_filter_protection_reenable", "strategy_tag": tag_key})
                        proposals.append({"key": wkey, "old": cfg.get(wkey, 0.0), "new": reenable_weight,
                                          "reason": "over_filter_protection_weight_reset", "strategy_tag": tag_key})
                        break
            # 升权一个低权重策略
            for tag_key in ["STARTUP", "OI_EXPLOSION", "QUIET_ACCUM", "PRE_BREAKOUT", "EARLY_START"]:
                wkey = f"STRATEGY_WEIGHTS.{tag_key}"
                current_w = float(cfg.get(wkey, 1.0))
                if current_w < 0.3:
                    proposals.append({"key": wkey, "old": current_w, "new": 0.5,
                                      "reason": "over_filter_protection_weight_raise", "strategy_tag": tag_key})
                    break
    return proposals


def _ensure_min_enabled_strategies(proposals: list[dict], cfg: dict) -> list[dict]:
    """确保不会禁用所有策略。"""
    from strategy_policy import get_enabled_strategies
    # 模拟应用 proposal 后的 enabled 状态
    temp_cfg = dict(cfg)
    for p in proposals:
        if p.get("key", "").startswith("STRATEGY_ENABLED.") and p.get("new") is False:
            temp_cfg[p["key"]] = False
    enabled = get_enabled_strategies(temp_cfg)
    min_enabled = int(cfg.get("EVOLVER_MIN_ENABLED_STRATEGIES", 2) or 2)
    if len(enabled) < min_enabled:
        # 拒绝最后一个禁用 proposal
        filtered = []
        for p in proposals:
            if p.get("key", "").startswith("STRATEGY_ENABLED.") and p.get("new") is False:
                tag = p["key"].split(".")[1]
                temp_cfg[p["key"]] = True
                if len(get_enabled_strategies(temp_cfg)) < min_enabled:
                    p["rejected_reason"] = "WOULD_DISABLE_LAST_STRATEGY"
                    continue
                temp_cfg[p["key"]] = False
            filtered.append(p)
        return filtered
    return proposals


# ══════════════════════════════════════════════════════════════════════════════
# 18. Shadow outcome 读取 + 自动调权重
# ══════════════════════════════════════════════════════════════════════════════
def load_shadow_trades() -> list[dict]:
    """读取 shadow_trades.jsonl。"""
    path = os.path.join(DATA_DIR, "shadow_trades.jsonl")
    return read_jsonl(path)


def compute_shadow_stats() -> dict[str, dict]:
    """统计 shadow outcomes 按 strategy_tag。"""
    try:
        from shadow_tracker import compute_shadow_outcomes
        return compute_shadow_outcomes()
    except Exception as e:
        logger.debug("compute_shadow_stats 失败: %s", e)
        return {}


def _apply_shadow_to_proposals(proposals: list[dict], metrics: dict, cfg: dict) -> list[dict]:
    """根据 shadow outcome 调整权重 proposals。"""
    shadow = compute_shadow_stats()
    if not shadow:
        return proposals

    by_tag = shadow.get("by_tag", {})
    for tag_key in ["STARTUP", "OI_EXPLOSION", "QUIET_ACCUM", "PRE_BREAKOUT", "EARLY_START"]:
        sdata = by_tag.get(tag_key, {})
        if sdata.get("shadow_closed", 0) < 5:
            continue
        swr = sdata.get("shadow_win_rate", 0.0)
        avg_pnl = sdata.get("avg_hypothetical_pnl_pct", 0.0)
        sl_hit_rate = sdata.get("sl_hit_rate", 0.0)
        tp1_hit = sdata.get("tp1_hit_rate", 0.0)
        avg_mfe = sdata.get("avg_mfe_pct", 0.0)
        avg_mae = sdata.get("avg_mae_pct", 0.0)
        wkey = f"STRATEGY_WEIGHTS.{tag_key}"
        ekey = f"STRATEGY_ENABLED.{tag_key}"
        current_w = float(cfg.get(wkey, 1.0))

        # 拦截正确：shadow 亏钱多
        if swr < 0.35 and avg_pnl < 0:
            if current_w > 0.5:
                proposals.append({"key": wkey, "old": current_w, "new": max(0.3, current_w - 0.2),
                                  "reason": f"shadow_swr={swr:.2f}_downweight", "strategy_tag": tag_key})
            continue

        # 过度过滤：shadow 赚钱多
        if swr > 0.55 and avg_pnl > 0:
            if not cfg.get(ekey, True):
                if not _is_last_enabled_strategy(tag_key, cfg):
                    proposals.append({"key": ekey, "old": False, "new": True,
                                      "reason": f"shadow_swr={swr:.2f}_reenable", "strategy_tag": tag_key})
            elif current_w < 1.0:
                proposals.append({"key": wkey, "old": current_w, "new": min(1.5, current_w + 0.2),
                                  "reason": f"shadow_swr={swr:.2f}_upweight", "strategy_tag": tag_key})
            continue

        # SL 率高但 MAE 小 → stop_too_tight → 适当放宽 SL
        if sl_hit_rate > 0.5 and abs(avg_mae) < 1.0:
            current_sl = float(cfg.get("SCALP_STOP_LOSS_PCT", 50.0))
            if current_sl < 60.0:
                proposals.append({"key": "SCALP_STOP_LOSS_PCT", "old": current_sl, "new": current_sl * 1.05,
                                  "reason": f"shadow_sl={sl_hit_rate:.2f}_mae={avg_mae:.2f}_loosen_sl",
                                  "strategy_tag": tag_key})

        # TP1 命中率高但 timeout 多 → 快进快出
        if tp1_hit > 0.3 and avg_mfe > 2.0:
            current_confirm = int(cfg.get("SCALP_TP_CONFIRM_TICKS", 2))
            if current_confirm > 1:
                proposals.append({"key": "SCALP_TP_CONFIRM_TICKS", "old": current_confirm, "new": 1,
                                  "reason": f"shadow_tp1={tp1_hit:.2f}_mfe={avg_mfe:.2f}_reduce_confirm",
                                  "strategy_tag": tag_key})

    return proposals
