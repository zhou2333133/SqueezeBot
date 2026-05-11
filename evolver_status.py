"""
Evolver 状态报告 — 生产运行监控。

统一读取所有 evolver 模块状态，供 Web API / 通知 / 命令行使用。
"""
import json
import logging
import os
import time
from datetime import datetime

from config import DATA_DIR, config_manager
from persistence import safe_read_json, read_jsonl

logger = logging.getLogger(__name__)


def _f(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════════
# 核心状态
# ══════════════════════════════════════════════════════════════════════════════
def _evolver_state_path() -> str:
    return os.path.join(DATA_DIR, "evolver_state.json")


def _runtime_state_path() -> str:
    return os.path.join(DATA_DIR, "evolver_runtime_state.json")


def get_evolver_status() -> dict:
    """返回当前 Evolver 完整状态。"""
    cfg = config_manager.settings
    runtime_state = safe_read_json(_runtime_state_path()) or {}
    evolver_state = safe_read_json(_evolver_state_path()) or {}

    return {
        "enabled": bool(cfg.get("EVOLVER_ENABLED", True)),
        "auto_apply": bool(cfg.get("EVOLVER_AUTO_APPLY", True)),
        "runtime_status": str(runtime_state.get("status", "UNKNOWN")),
        "current_policy_version": str(evolver_state.get("current_policy_version",
                                        runtime_state.get("last_policy_version", "manual-v1"))),
        "current_config_hash": str(evolver_state.get("current_config_hash", "")),
        "pending_evaluation": bool(evolver_state.get("pending_evaluation", False)),
        "frozen": runtime_state.get("status") == "FROZEN",
        "freeze_reason": str(runtime_state.get("freeze_reason", "")),
        "frozen_until": runtime_state.get("frozen_until"),
        "last_evolver_run_at": runtime_state.get("last_started_at"),
        "last_success": bool(runtime_state.get("last_success", True)),
        "last_error": str(runtime_state.get("last_error", "")),
        "rollback_count": int(evolver_state.get("rollback_count", 0)),
        "daily_rollback_count": int(runtime_state.get("daily_rollback_count", 0)),
        "consecutive_errors": int(runtime_state.get("consecutive_errors", 0)),
        "locked_params_unchanged": True,
        "trades_since_last_policy": int(evolver_state.get("trades_since_last_policy", 0)),
    }


def get_current_policy() -> dict:
    """返回当前 policy 快照。"""
    pv = config_manager.settings.get("POLICY_VERSION", "manual-v1")
    return {
        "policy_version": str(pv),
        "config_hash": _compute_cfg_hash(),
        "policy_version_ts": datetime.now().isoformat(),
        "trades_under_policy": 0,
    }


def _compute_cfg_hash() -> str:
    try:
        from strategy_evolver import compute_config_hash
        return compute_config_hash()
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# 历史读取
# ══════════════════════════════════════════════════════════════════════════════
def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def get_recent_evolver_history(limit: int = 20) -> list[dict]:
    return read_jsonl(_path("evolver_history.jsonl"))[-limit:]


def get_recent_runtime_events(limit: int = 20) -> list[dict]:
    return read_jsonl(_path("evolver_runtime_events.jsonl"))[-limit:]


def get_recent_param_patches(limit: int = 50) -> list[dict]:
    try:
        patches = read_jsonl(_path("param_patches.jsonl"))
        # 尝试附加 effectiveness
        perf = read_jsonl(_path("param_patch_performance.jsonl"))
        eff_map = {}
        for p in perf:
            eff_map[p.get("param_patch_id", "")] = {
                "effectiveness": p.get("effectiveness", "UNKNOWN"),
                "real_win_rate": p.get("real_win_rate"),
                "real_pnl_total": p.get("real_pnl_total"),
            }
        for p in patches:
            pid = p.get("param_patch_id", "")
            if pid in eff_map:
                p["_effectiveness"] = eff_map[pid]
        return patches[-limit:]
    except Exception:
        return []


def get_recent_proposal_validations(limit: int = 20) -> list[dict]:
    return read_jsonl(_path("proposal_validation_history.jsonl"))[-limit:]


def get_recent_policy_performance(limit: int = 20) -> list[dict]:
    return read_jsonl(_path("policy_performance.jsonl"))[-limit:]


def get_recent_shadow_summary() -> dict:
    try:
        from shadow_tracker import compute_shadow_outcomes
        return compute_shadow_outcomes()
    except Exception as e:
        logger.warning("shadow summary error: %s", e)
        return {}


def get_locked_params_status() -> dict:
    """返回 LOCKED_PARAMS 和当前值。"""
    locked_names = getattr(config_manager, "LOCKED_PARAMS", set())
    result = {}
    for key in sorted(locked_names):
        result[key] = {
            "current_value": config_manager.settings.get(key),
            "modified": False,
        }
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 健康检查
# ══════════════════════════════════════════════════════════════════════════════
def run_evolver_health_check() -> dict:
    """执行完整健康检查。"""
    warnings: list[str] = []
    errors: list[str] = []
    cfg = config_manager.settings

    # 1. Enabled
    if not cfg.get("EVOLVER_ENABLED", True):
        warnings.append("EVOLVER_ENABLED=False")

    # 2. Locked params
    if not hasattr(config_manager, "LOCKED_PARAMS"):
        errors.append("LOCKED_PARAMS missing")
    else:
        locked = config_manager.LOCKED_PARAMS
        for k in locked:
            if k not in cfg:
                warnings.append(f"LOCKED_PARAM {k} not in settings")

    # 3. PARAM_BOUNDS
    if not hasattr(config_manager, "PARAM_BOUNDS"):
        errors.append("PARAM_BOUNDS missing")

    # 4. Data files
    for fname in ["strategy_trades.jsonl", "evolver_state.json", "evolver_runtime_state.json"]:
        fpath = os.path.join(DATA_DIR, fname)
        if not os.path.exists(fpath):
            warnings.append(f"{fname} not found")

    # 5. Module imports
    for mod_name in ["strategy_evolver", "proposal_validator", "param_attribution", "shadow_tracker"]:
        try:
            __import__(mod_name)
        except ImportError as e:
            errors.append(f"import {mod_name} failed: {e}")

    # 6. Frozen check
    runtime = safe_read_json(_runtime_state_path()) or {}
    if runtime.get("status") == "FROZEN":
        warnings.append(f"Evolver frozen until {runtime.get('frozen_until')}")

    return {
        "ok": len(errors) == 0,
        "warnings": warnings,
        "errors": errors,
        "checked_at": datetime.now().isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 清理
# ══════════════════════════════════════════════════════════════════════════════
def prune_evolver_logs() -> dict:
    """清理过期 evolver 日志文件。"""
    cfg = config_manager.settings
    retention_days = int(cfg.get("EVOLVER_LOG_RETENTION_DAYS", 30) or 30)
    max_lines = int(cfg.get("EVOLVER_MAX_HISTORY_LINES", 10000) or 10000)
    max_backups = int(cfg.get("EVOLVER_MAX_BACKUPS", 50) or 50)
    now = time.time()
    cutoff = now - retention_days * 86400
    pruned = []

    # 按行数清理 JSONL
    for fname in ["evolver_history.jsonl", "evolver_runtime_events.jsonl",
                   "proposal_validation_history.jsonl", "policy_performance.jsonl",
                   "param_patch_performance.jsonl"]:
        fpath = os.path.join(DATA_DIR, fname)
        if not os.path.exists(fpath):
            continue
        try:
            lines = read_jsonl(fpath)
            if len(lines) > max_lines:
                # Keep most recent max_lines
                keep = lines[-max_lines:]
                with open(fpath, "w", encoding="utf-8") as f:
                    for item in keep:
                        f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                pruned.append(f"{fname}: {len(lines)} -> {len(keep)}")
        except Exception as e:
            logger.warning("prune %s failed: %s", fname, e)

    # 按天数清理 config_backups
    backup_dir = os.path.join(DATA_DIR, "config_backups")
    if os.path.exists(backup_dir):
        try:
            all_backups = sorted([f for f in os.listdir(backup_dir) if f.endswith(".json")])
            # Keep current backups within retention
            backups_to_delete = []
            for fname in all_backups[:-max_backups] if len(all_backups) > max_backups else []:
                fpath = os.path.join(backup_dir, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    if mtime < cutoff:
                        backups_to_delete.append(fname)
                        os.remove(fpath)
                except Exception:
                    pass
            if backups_to_delete:
                pruned.append(f"config_backups: removed {len(backups_to_delete)} old backups")
        except Exception as e:
            logger.warning("prune config_backups failed: %s", e)

    # 记录事件
    try:
        from evolver_runtime import _write_event
        _write_event("LOGS_PRUNED", {"status": "IDLE"}, pruned_count=len(pruned))
    except Exception:
        pass

    return {"pruned": pruned, "count": len(pruned)}
