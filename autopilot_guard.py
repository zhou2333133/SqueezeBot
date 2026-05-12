"""
Autopilot Guard — 自动运行守卫。

保证长期自动运行时任何路径都不能偷改 LOCKED_PARAMS。
"""
import json
import logging
import os
import time
from datetime import datetime

from config import DATA_DIR, config_manager
from persistence import append_jsonl, atomic_write_json, safe_read_json

logger = logging.getLogger(__name__)

GUARD_EVENTS_FILE = os.path.join(DATA_DIR, "autopilot_guard_events.jsonl")
LOCKED_SNAPSHOT_FILE = os.path.join(DATA_DIR, "locked_params_snapshot.json")
E2E_STATUS_FILE = os.path.join(DATA_DIR, "evolver_e2e_status.json")


# ══════════════════════════════════════════════════════════════════════════════
# Locked param snapshot
# ══════════════════════════════════════════════════════════════════════════════
def get_current_locked_param_values(cfg: dict | None = None) -> dict:
    if cfg is None:
        cfg = config_manager.settings
    locked = getattr(config_manager, "LOCKED_PARAMS", set())
    return {k: cfg.get(k) for k in sorted(locked) if k in cfg}


def load_locked_param_snapshot() -> dict | None:
    data = safe_read_json(LOCKED_SNAPSHOT_FILE)
    if isinstance(data, dict) and "locked_params" in data:
        return data
    return None


def save_locked_param_snapshot(snapshot: dict | None = None) -> dict:
    if snapshot is None:
        snapshot = get_current_locked_param_values()
    record = {"created_at": time.time(), "locked_params": snapshot}
    atomic_write_json(LOCKED_SNAPSHOT_FILE, record)
    return record


def assert_locked_params_unchanged(before: dict, after: dict) -> bool:
    """检查锁定参数是否变化。返回 True=未变, False=被修改。"""
    for k in before:
        if before.get(k) != after.get(k):
            return False
    return True


def restore_locked_params(cfg: dict | None, snapshot: dict) -> None:
    if cfg is None:
        cfg = config_manager.settings
    for k, v in snapshot.items():
        if k in cfg:
            cfg[k] = v
    config_manager._persist()


# ══════════════════════════════════════════════════════════════════════════════
# Guard: config write guard
# ══════════════════════════════════════════════════════════════════════════════
def guard_config_write(before_cfg: dict | None = None) -> bool:
    """配置写入守卫：检查 LOCKED_PARAMS 是否被改。返回 True=安全。"""
    if before_cfg is None:
        before_cfg = get_current_locked_param_values()
    after_cfg = get_current_locked_param_values()
    if assert_locked_params_unchanged(before_cfg, after_cfg):
        return True
    # Locked param changed — restore
    snapshot = load_locked_param_snapshot()
    if snapshot:
        restore_locked_params(config_manager.settings, snapshot["locked_params"])
    write_guard_event("LOCKED_PARAM_MUTATED", "CRITICAL",
                      "locked param changed during config write, restored")
    freeze_evolver("LOCKED_PARAM_MUTATED")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Guard: startup
# ══════════════════════════════════════════════════════════════════════════════
def run_startup_guard(cfg: dict | None = None) -> dict:
    """启动自检。返回 (ok, warnings, errors)。"""
    warnings: list[str] = []
    errors: list[str] = []
    if cfg is None:
        cfg = config_manager.settings

    # 1. LOCKED_PARAMS exists
    if not hasattr(config_manager, "LOCKED_PARAMS"):
        errors.append("LOCKED_PARAMS missing")
        return {"ok": False, "warnings": warnings, "errors": errors, "frozen": True}

    # 2. Create snapshot if not exists
    snapshot = load_locked_param_snapshot()
    if snapshot is None:
        snapshot = save_locked_param_snapshot()
        logger.info("Locked params snapshot created")

    # 3. Check current vs snapshot
    current = get_current_locked_param_values(cfg)
    snap_vals = snapshot.get("locked_params", {})
    for k in snap_vals:
        if k in current and current[k] != snap_vals[k]:
            errors.append(f"LOCKED_PARAM {k} changed: {snap_vals[k]} -> {current[k]}, restoring")
            cfg[k] = snap_vals[k]
    if errors:
        config_manager._persist()
        freeze_evolver("LOCKED_PARAM_MISMATCH_AT_STARTUP")

    # 4. PARAM_BOUNDS exists
    if not hasattr(config_manager, "PARAM_BOUNDS"):
        errors.append("PARAM_BOUNDS missing")

    # 5. Module imports
    for mod_name in ["strategy_evolver", "evolver_runtime", "deprecated.proposal_validator",
                      "param_attribution", "shadow_tracker", "strategy_policy"]:
        try:
            __import__(mod_name)
        except ImportError as e:
            errors.append(f"import {mod_name} failed: {e}")

    # 6. Data dir writable
    if not os.access(DATA_DIR, os.W_OK):
        errors.append(f"DATA_DIR not writable: {DATA_DIR}")

    # 7. E2E check
    e2e_status = safe_read_json(E2E_STATUS_FILE)
    if not isinstance(e2e_status, dict) or not e2e_status.get("passed"):
        warnings.append("E2E not passed or no E2E data")
        e2e_max_age = float(cfg.get("AUTOPILOT_E2E_MAX_AGE_HOURS", 24) or 24)
        if isinstance(e2e_status, dict) and e2e_status.get("last_run_at"):
            age_hours = (time.time() - e2e_status["last_run_at"]) / 3600
            if age_hours > e2e_max_age:
                warnings.append(f"E2E result too old: {age_hours:.1f}h > {e2e_max_age}h")

    ok = len(errors) == 0
    if not ok and cfg.get("AUTOPILOT_FREEZE_EVOLVER_ON_GUARD_FAIL", True):
        freeze_evolver("startup_guard_failed")
    write_guard_event("STARTUP_CHECK", "WARNING" if warnings else "INFO",
                      f"ok={ok} errors={len(errors)} warnings={len(warnings)}",
                      {"errors": errors, "warnings": warnings})
    return {"ok": ok, "warnings": warnings, "errors": errors,
            "frozen": not ok and cfg.get("AUTOPILOT_FREEZE_EVOLVER_ON_GUARD_FAIL", True)}


# ══════════════════════════════════════════════════════════════════════════════
# Guard: periodic
# ══════════════════════════════════════════════════════════════════════════════
def run_periodic_guard(cfg: dict | None = None) -> dict:
    """周期健康检查（委托 risk_guard）。"""
    try:
        from risk_guard import health_check
        return health_check()
    except Exception as e:
        return {"ok": False, "warnings": [str(e)], "errors": [], "frozen": False}

def freeze_evolver(reason: str) -> None:
    """冻结 Evolver。不影响交易。"""
    try:
        from evolver_runtime import _get_state, _save_state
        state = _get_state()
        state["status"] = "FROZEN"
        state["frozen_until"] = time.time() + 86400 * 365
        state["freeze_reason"] = reason
        state["consecutive_errors"] = 99
        _save_state(state)
        config_manager.settings["EVOLVER_ENABLED"] = False
        config_manager._persist()
        write_guard_event("EVOLVER_FROZEN", "CRITICAL", reason)
        logger.critical("Evolver frozen: %s", reason)
    except Exception as e:
        logger.warning("freeze_evolver failed: %s", e)


def unfreeze_evolver_if_safe() -> bool:
    """安全解冻：先检查锁定参数是否正常。"""
    try:
        snapshot = load_locked_param_snapshot()
        if snapshot:
            current = get_current_locked_param_values()
            if not assert_locked_params_unchanged(snapshot.get("locked_params", {}), current):
                logger.warning("Unfreeze rejected: locked params changed")
                return False
        from evolver_runtime import _get_state, _save_state
        state = _get_state()
        state["status"] = "IDLE"
        state["frozen_until"] = 0.0
        state["freeze_reason"] = ""
        state["consecutive_errors"] = 0
        _save_state(state)
        config_manager.settings["EVOLVER_ENABLED"] = True
        config_manager._persist()
        write_guard_event("EVOLVER_UNFROZEN", "INFO", "safe unfreeze")
        return True
    except Exception as e:
        logger.warning("unfreeze failed: %s", e)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Guard events
# ══════════════════════════════════════════════════════════════════════════════
def write_guard_event(event_type: str, severity: str, reason: str, details: dict | None = None) -> None:
    try:
        record = {
            "event_type": event_type,
            "created_at": time.time(),
            "severity": severity,
            "reason": reason,
            "details": details or {},
        }
        append_jsonl(GUARD_EVENTS_FILE, record)
    except Exception as e:
        logger.warning("write_guard_event failed: %s", e)


def get_guard_events(limit: int = 50) -> list[dict]:
    return safe_read_json(GUARD_EVENTS_FILE) if GUARD_EVENTS_FILE.endswith(".json") else _read_jsonl(GUARD_EVENTS_FILE, limit)


def _read_jsonl(path: str, limit: int = 0) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        result = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    result.append(json.loads(line))
        if limit > 0:
            return result[-limit:]
        return result
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# E2E status
# ══════════════════════════════════════════════════════════════════════════════
def load_e2e_status() -> dict:
    data = safe_read_json(E2E_STATUS_FILE)
    if isinstance(data, dict):
        return data
    return {"last_run_at": 0, "passed": False, "cases": {}, "failed_cases": [], "skipped_cases": []}


def save_e2e_status(result: dict) -> None:
    record = {
        "last_run_at": time.time(),
        "passed": result.get("passed", False),
        "cases": result.get("cases", {}),
        "failed_cases": result.get("failed_cases", []),
        "skipped_cases": result.get("skipped_cases", []),
    }
    atomic_write_json(E2E_STATUS_FILE, record)


# ══════════════════════════════════════════════════════════════════════════════
# Check functions for status display
# ══════════════════════════════════════════════════════════════════════════════
def check_config_integrity() -> dict:
    issues = []
    snapshot = load_locked_param_snapshot()
    if snapshot:
        current = get_current_locked_param_values()
        for k, v in snapshot.get("locked_params", {}).items():
            if k in current and current[k] != v:
                issues.append({"key": k, "snapshot": v, "current": current[k]})
    return {"ok": len(issues) == 0, "issues": issues}


def check_required_files() -> list[str]:
    missing = []
    for fname in ["strategy_trades.jsonl", "settings.json", "evolver_runtime_state.json"]:
        if not os.path.exists(os.path.join(DATA_DIR, fname)):
            missing.append(fname)
    return missing
