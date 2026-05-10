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
from persistence import append_jsonl, ensure_parent_dir, safe_read_json

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────
EVOLVER_HISTORY_FILE = os.path.join(DATA_DIR, "evolver_history.jsonl")
CONFIG_BACKUP_DIR = os.path.join(DATA_DIR, "config_backups")
_ALL_STRATEGY_TAGS = ["启动型", "OI爆发", "静默建仓", "突破前夜", "早期启动"]


# ══════════════════════════════════════════════════════════════════════════════
# 1. 数据加载
# ══════════════════════════════════════════════════════════════════════════════
def load_trade_data() -> list[dict]:
    """从 strategy_trades.jsonl 加载交易记录。"""
    path = os.path.join(DATA_DIR, "strategy_trades.jsonl")
    return safe_read_json(path) if path.endswith(".json") else _read_jsonl(path)


def _read_jsonl(path: str) -> list[dict]:
    """读取 JSONL 文件。"""
    if not os.path.exists(path):
        return []
    try:
        result = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    result.append(json.loads(line))
        return result
    except Exception as e:
        logger.warning("读取 %s 失败: %s", path, e)
        return []


def load_current_config() -> dict:
    """加载当前配置快照。"""
    return dict(config_manager.settings)


# ══════════════════════════════════════════════════════════════════════════════
# 2. 策略指标统计
# ══════════════════════════════════════════════════════════════════════════════
def compute_strategy_metrics(trades: list[dict]) -> dict[str, dict]:
    """按 strategy_tag 统计策略表现。"""
    by_tag: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        tag = str(t.get("strategy_tag") or "")
        if tag:
            by_tag[tag].append(t)

    metrics: dict[str, dict] = {}
    for tag, tag_trades in by_tag.items():
        n = len(tag_trades)
        wins = [t for t in tag_trades if _f(t.get("pnl_usdt")) >= 0]
        losses = [t for t in tag_trades if _f(t.get("pnl_usdt")) < 0]
        win_count = len(wins)
        loss_count = len(losses)
        pnls = [_f(t.get("pnl_usdt")) for t in tag_trades]
        total_pnl = sum(pnls)
        win_pnls = [_f(t.get("pnl_usdt")) for t in wins]
        loss_pnls = [_f(t.get("pnl_usdt")) for t in losses]
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
        avg_loss = abs(sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 1.0
        expectancy = (win_count / n * avg_win - loss_count / n * avg_loss) if n > 0 else 0.0 if n else 0.0
        profit_factor = sum(win_pnls) / abs(sum(loss_pnls)) if sum(loss_pnls) != 0 else float("inf") if sum(win_pnls) > 0 else 0.0

        # Failure tags aggregate
        all_tags: list[str] = []
        for t in tag_trades:
            ft = t.get("failure_tags") or t.get("diagnosis_tags") or []
            all_tags.extend(ft if isinstance(ft, list) else [])
        tag_counts = defaultdict(int)
        for tg in all_tags:
            if tg and tg != "good_trade":
                tag_counts[tg] += 1

        # Exit reason distribution
        exit_reasons = defaultdict(int)
        for t in tag_trades:
            r = str(t.get("close_reason") or "UNKNOWN")
            exit_reasons[r] += 1

        metrics[tag] = {
            "total_trades": n,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round(win_count / n, 4) if n > 0 else 0.0,
            "pnl_total": round(total_pnl, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "expectancy": round(expectancy, 4),
            "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
            "avg_mfe": round(sum(_f(t.get("mfe_pct")) for t in tag_trades) / n, 4) if n > 0 else 0.0,
            "avg_mae": round(sum(_f(t.get("mae_pct")) for t in tag_trades) / n, 4) if n > 0 else 0.0,
            "avg_duration_sec": round(sum(_f(t.get("duration_sec")) for t in tag_trades) / n, 1) if n > 0 else 0.0,
            "exit_reason_distribution": dict(sorted(exit_reasons.items(), key=lambda x: -x[1])),
            "failure_tags_top": [{"tag": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda x: -x[1])[:5]],
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
    """策略标签 → 配置 key 前缀。"""
    mapping = {"启动型": "启动型", "OI爆发": "OI爆发", "静默建仓": "静默建仓",
               "突破前夜": "突破前夜", "早期启动": "早期启动"}
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
    """校验 proposals，返回 (valid, rejected)。"""
    locked = config_manager.LOCKED_PARAMS
    bounds = config_manager.PARAM_BOUNDS
    max_change_pct = float(current_config.get("EVOLVER_MAX_PARAM_CHANGE_PCT", 0.30) or 0.30)
    max_updates = int(current_config.get("EVOLVER_MAX_UPDATES_PER_RUN", 5) or 5)

    valid: list[dict] = []
    rejected: list[dict] = []

    for prop in proposals:
        key = prop["key"]
        old = prop["old"]
        new = prop["new"]

        # 检查 LOCKED_PARAMS
        if key in locked:
            rejected.append({**prop, "rejected_reason": "LOCKED_PARAM"})
            continue

        # 检查 PARAM_BOUNDS
        if key not in bounds:
            rejected.append({**prop, "rejected_reason": "NO_BOUNDS"})
            continue

        # 检查变化幅度
        lo, hi = bounds[key]
        pct_change = abs(new - old) / abs(old) if old != 0 else 1.0
        if pct_change > max_change_pct:
            # clamp 到最大允许值
            max_delta = abs(old) * max_change_pct
            if new > old:
                new = old + max_delta
            else:
                new = old - max_delta
            prop["new"] = new
            prop["clamped"] = True

        # clamp 到 PARAM_BOUNDS 范围
        new = max(lo, min(hi, new))
        prop["new"] = new

        if abs(new - old) < 0.001:
            continue  # 无实际变化，跳过

        valid.append(prop)

    # 限制每轮修改数量
    valid = valid[:max_updates]
    return valid, rejected


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
    """自动写入新配置。"""
    applied: list[dict] = []
    for update in valid_updates:
        key = update["key"]
        new_val = update["new"]
        old_val = config_manager.settings.get(key)
        if old_val is not None:
            # 类型转换（保留原类型）
            typ = type(old_val)
            try:
                converted = typ(new_val)
            except (ValueError, TypeError):
                converted = new_val
            config_manager.settings[key] = converted
            applied.append({
                "key": key, "old": old_val, "new": converted,
                "reason": update.get("reason", ""),
                "change_pct": round(abs(converted - old_val) / abs(old_val), 4) if old_val != 0 else None,
            })
    if applied:
        config_manager._persist()
        logger.info("Evolver 自动应用了 %d 个参数修改", len(applied))
    return applied


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
    records = _read_jsonl(EVOLVER_HISTORY_FILE)
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
        trades = load_trade_data()
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

        # 5. 校验
        valid, rejected = validate_param_updates(proposals, current_config)
        result["rejected_updates"] = rejected

        if not valid:
            result["message"] = f"所有 proposal 被拒绝 ({len(rejected)} rejected)"
            return result

        # 6. 备份
        if cfg.get("EVOLVER_BACKUP_ENABLED", True):
            backup_path = backup_config()
            result["config_backup"] = backup_path

        # 7. 应用
        if auto_apply:
            applied = apply_param_updates(valid)
            result["applied_updates"] = applied
            result["auto_applied"] = True

            # 8. 更新 policy_version
            version = write_policy_version()
            result["policy_version"] = version

            # 持久化
            config_manager._persist()
        else:
            result["applied_updates"] = valid
            result["message"] = "EVOLVER_AUTO_APPLY=False，仅生成建议未应用"
            return result

        # 9. 记录历史
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
def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
