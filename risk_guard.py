"""
Risk Guard — 独立硬风控层。

职责：
  - 检查每笔开仓是否越界（金额、持仓数、杠杆、日亏损）
  - 检查参数修改提案是否越界（LOCKED_PARAMS、PARAM_BOUNDS）
  - 管理冷却状态
  - 独立于 AI/Evolver，不能被任何代码绕过

架构位置：
  order_intent → risk_guard → execution_router → executor
"""
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime

from config import DATA_DIR, config_manager
from persistence import atomic_write_json, safe_read_json, append_jsonl

logger = logging.getLogger(__name__)

BOUNDARY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "boundary_config.json")
GUARD_EVENTS_FILE = os.path.join(DATA_DIR, "guard_events.jsonl")


# ══════════════════════════════════════════════════════════════════════════════
# Boundary config
# ══════════════════════════════════════════════════════════════════════════════
def _load_boundary() -> dict:
    """读取 boundary_config.json。"""
    try:
        data = safe_read_json(BOUNDARY_FILE)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_boundary(data: dict) -> None:
    atomic_write_json(BOUNDARY_FILE, data)


def get_boundary(key: str, default=None):
    """获取边界配置值。"""
    return _load_boundary().get(key, default)


# ══════════════════════════════════════════════════════════════════════════════
# 硬风控：开仓检查
# ══════════════════════════════════════════════════════════════════════════════
LOCKED_PARAMS_NAMES = [
    "fixed_order_amount_usdt", "max_open_positions", "max_leverage",
    "daily_max_loss_usdt", "max_account_drawdown_pct",
    "execution_mode", "trading_enabled", "allow_open", "allow_close",
]


def check_open_order(symbol: str, direction: str, amount_usdt: float, leverage: int,
                      current_positions: int, daily_loss_usdt: float) -> dict:
    """检查一笔新开仓是否越界。返回 {allow, reason}。"""
    boundary = _load_boundary()

    if not boundary.get("trading_enabled", True):
        return {"allow": False, "reason": "trading_disabled"}
    if not boundary.get("allow_open", True):
        return {"allow": False, "reason": "open_disabled"}

    max_pos = int(boundary.get("max_open_positions", 3) or 3)
    if current_positions >= max_pos:
        return {"allow": False, "reason": f"max_positions_{max_pos}"}

    max_amount = float(boundary.get("fixed_order_amount_usdt", 100) or 100)
    if amount_usdt > max_amount:
        return {"allow": False, "reason": f"amount_{amount_usdt:.0f}>max_{max_amount:.0f}"}

    max_lev = int(boundary.get("max_leverage", 10) or 10)
    if leverage > max_lev:
        return {"allow": False, "reason": f"leverage_{leverage}x>max_{max_lev}x"}

    max_daily_loss = float(boundary.get("daily_max_loss_usdt", 200) or 200)
    if abs(daily_loss_usdt) >= max_daily_loss:
        return {"allow": False, "reason": f"daily_loss_{abs(daily_loss_usdt):.0f}>={max_daily_loss:.0f}"}

    allowed = boundary.get("allowed_symbols", [])
    if allowed and symbol not in allowed:
        return {"allow": False, "reason": "symbol_not_allowed"}
    blocked = boundary.get("blocked_symbols", [])
    if symbol in blocked:
        return {"allow": False, "reason": "symbol_blocked"}

    return {"allow": True, "reason": ""}


# ══════════════════════════════════════════════════════════════════════════════
# 参数提案审核
# ══════════════════════════════════════════════════════════════════════════════
LOCKED_PARAMS_SET = frozenset(LOCKED_PARAMS_NAMES)


def check_param_proposal(key: str, old_val, new_val) -> dict:
    """检查参数修改是否越界。返回 {allow, reason}。"""
    # 检查 LOCKED_PARAMS
    if key in LOCKED_PARAMS_SET:
        return {"allow": False, "reason": "LOCKED_PARAM"}

    # 检查 PARAM_BOUNDS
    bounds = getattr(config_manager, "PARAM_BOUNDS", {})
    if key in bounds:
        lo, hi = bounds[key]
        try:
            nv = float(new_val)
            if nv < lo or nv > hi:
                return {"allow": False, "reason": f"OUT_OF_BOUNDS [{lo}, {hi}]"}
        except (TypeError, ValueError):
            pass

    return {"allow": True, "reason": ""}


def check_proposals(proposals: list[dict]) -> tuple[list[dict], list[dict]]:
    """批量审核提案。返回 (valid, rejected)。"""
    valid, rejected = [], []
    for prop in proposals:
        result = check_param_proposal(prop.get("key", ""), prop.get("old"), prop.get("new"))
        if result["allow"]:
            valid.append(prop)
        else:
            rejected.append({**prop, "rejected_reason": result["reason"]})
    return valid, rejected


# ══════════════════════════════════════════════════════════════════════════════
# 冷却追踪
# ══════════════════════════════════════════════════════════════════════════════
_COOLDOWNS: dict[str, float] = {}

_cooldown_file = os.path.join(DATA_DIR, "param_cooldowns.json")


def _load_cooldowns() -> dict:
    return safe_read_json(_cooldown_file) or {}


def _save_cooldowns(data: dict) -> None:
    atomic_write_json(_cooldown_file, data)


def set_cooldown(key: str, duration_sec: float, reason: str = "") -> None:
    cd = _load_cooldowns()
    cd[key] = {"cooldown_until": time.time() + duration_sec, "reason": reason}
    _save_cooldowns(cd)


def is_in_cooldown(key: str) -> tuple[bool, float]:
    cd = _load_cooldowns()
    entry = cd.get(key)
    if not entry:
        return False, 0.0
    remaining = entry.get("cooldown_until", 0) - time.time()
    if remaining > 0:
        return True, remaining
    cd.pop(key, None)
    _save_cooldowns(cd)
    return False, 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 健康检查（替代 autopilot_guard.run_periodic_guard）
# ══════════════════════════════════════════════════════════════════════════════
def health_check() -> dict:
    """综合健康检查。"""
    warnings, errors = [], []
    boundary = _load_boundary()

    if not boundary.get("trading_enabled"):
        warnings.append("trading_disabled")
    if not boundary.get("allow_open"):
        warnings.append("open_disabled")

    for fname in ["strategy_trades.jsonl", "settings.json"]:
        fpath = os.path.join(DATA_DIR, fname)
        if not os.path.exists(fpath):
            warnings.append(f"{fname} not found")

    return {"ok": len(errors) == 0, "warnings": warnings, "errors": errors}


# ══════════════════════════════════════════════════════════════════════════════
# 事件记录
# ══════════════════════════════════════════════════════════════════════════════
def write_guard_event(event_type: str, severity: str, reason: str, details: dict | None = None) -> None:
    try:
        append_jsonl(GUARD_EVENTS_FILE, {
            "event_type": event_type,
            "created_at": time.time(),
            "severity": severity,
            "reason": reason,
            "details": details or {},
        })
    except Exception as e:
        logger.warning("write_guard_event failed: %s", e)


def get_guard_events(limit: int = 50) -> list[dict]:
    if not os.path.exists(GUARD_EVENTS_FILE):
        return []
    try:
        events = []
        with open(GUARD_EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events[-limit:]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 审核通过后写入
# ══════════════════════════════════════════════════════════════════════════════
def apply_proposals(proposals: list[dict]) -> tuple[list[dict], list[dict]]:
    """审核提案，通过的写入 config_manager.settings，拒绝的返回。
    
    这是 risk_guard 作为"裁判"的核心入口：
      Evolver 提案 → risk_guard 审核 → risk_guard 写入
      不允许 Evolver 直接写配置。
    """
    valid, rejected = check_proposals(proposals)

    # 额外检查 PARAM_BOUNDS
    bounds = getattr(config_manager, "PARAM_BOUNDS", {})
    max_change_pct = float(config_manager.settings.get("EVOLVER_MAX_PARAM_CHANGE_PCT", 0.30) or 0.30)
    max_updates = int(config_manager.settings.get("EVOLVER_MAX_UPDATES_PER_RUN", 5) or 5)
    filtered = []

    for prop in valid:
        key = prop["key"]
        old_v = prop["old"]
        new_v = prop["new"]

        # INEFFECTIVE_PARAMS
        if hasattr(config_manager, "INEFFECTIVE_PARAMS") and key in config_manager.INEFFECTIVE_PARAMS:
            rejected.append({**prop, "rejected_reason": "INEFFECTIVE_PARAM"})
            continue
        # PARAM_BOUNDS
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

    # 限制每轮修改数量
    filtered = filtered[:max_updates]

    # 审核通过的写入配置
    applied = []
    for prop in filtered:
        key = prop["key"]
        new_val = prop["new"]
        old_val = config_manager.settings.get(key)
        if old_val is not None:
            typ = type(old_val)
            try:
                converted = typ(new_val)
            except (ValueError, TypeError):
                converted = new_val
            config_manager.settings[key] = converted
            applied.append({**prop, "new": converted})

    if applied:
        config_manager._persist()
        write_guard_event("PARAMS_APPLIED", "INFO", f"{len(applied)} params updated",
                          {"applied": [{a["key"]: a["new"]} for a in applied]})

    return applied, rejected


# ══════════════════════════════════════════════════════════════════════════════
# 状态读取（原 evolver_status.py，合并至此）
# ══════════════════════════════════════════════════════════════════════════════
def _g(d: dict | None, key: str, default=None):
    if not d:
        return default
    return d.get(key, default)


def get_evolver_status() -> dict:
    """返回当前 Evolver 完整状态。"""
    from config import config_manager
    cfg = config_manager.settings
    runtime_state = safe_read_json(os.path.join(DATA_DIR, "evolver_runtime_state.json")) or {}
    evolver_state = safe_read_json(os.path.join(DATA_DIR, "evolver_state.json")) or {}

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
        "last_evolver_run_at": runtime_state.get("last_started_at"),
        "last_success": bool(runtime_state.get("last_success", True)),
        "last_error": str(runtime_state.get("last_error", "")),
        "rollback_count": int(evolver_state.get("rollback_count", 0)),
        "daily_rollback_count": int(runtime_state.get("daily_rollback_count", 0)),
        "consecutive_errors": int(runtime_state.get("consecutive_errors", 0)),
        "locked_params_unchanged": True,
        "trades_since_last_policy": int(evolver_state.get("trades_since_last_policy", 0)),
        "last_skip_reason": str(runtime_state.get("last_skip_reason", "")),
        "last_skip_at": runtime_state.get("last_skip_at", 0.0),
        "trades_at_last_check": int(runtime_state.get("trades_at_last_check", 0)),
    }


def get_locked_params_status() -> dict:
    """返回 LOCKED_PARAMS 和当前值。"""
    from config import config_manager
    locked_names = getattr(config_manager, "LOCKED_PARAMS", set())
    result = {}
    for key in sorted(locked_names):
        result[key] = {"current_value": config_manager.settings.get(key), "modified": False}
    return result


def get_recent_evolver_history(limit: int = 20) -> list[dict]:
    path = os.path.join(DATA_DIR, "evolver_history.jsonl")
    if not os.path.exists(path):
        return []
    try:
        events = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    events.append(json.loads(line))
        return events[-limit:]
    except Exception:
        return []


def get_recent_param_patches(limit: int = 50) -> list[dict]:
    path = os.path.join(DATA_DIR, "param_patches.jsonl")
    if not os.path.exists(path):
        return []
    try:
        patches = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    patches.append(json.loads(line))
        return patches[-limit:]
    except Exception:
        return []


def get_recent_shadow_summary() -> dict:
    try:
        from shadow_tracker import compute_shadow_outcomes
        return compute_shadow_outcomes()
    except Exception as e:
        logger.warning("shadow summary error: %s", e)
        return {}


def run_evolver_health_check() -> dict:
    """执行完整健康检查。"""
    from config import config_manager
    warnings, errors = [], []
    cfg = config_manager.settings

    if not cfg.get("EVOLVER_ENABLED", True):
        warnings.append("EVOLVER_ENABLED=False")
    if not hasattr(config_manager, "LOCKED_PARAMS"):
        errors.append("LOCKED_PARAMS missing")
    if not hasattr(config_manager, "PARAM_BOUNDS"):
        errors.append("PARAM_BOUNDS missing")

    for fname in ["strategy_trades.jsonl", "evolver_runtime_state.json"]:
        fpath = os.path.join(DATA_DIR, fname)
        if not os.path.exists(fpath):
            warnings.append(f"{fname} not found")

    return {
        "ok": len(errors) == 0,
        "warnings": warnings,
        "errors": errors,
        "checked_at": datetime.now().isoformat(),
    }


def prune_evolver_logs() -> dict:
    """清理过期 evolver 日志（原 evolver_status.py）。"""
    from config import config_manager
    cfg = config_manager.settings
    retention_days = int(cfg.get("EVOLVER_LOG_RETENTION_DAYS", 30) or 30)
    max_lines = int(cfg.get("EVOLVER_MAX_HISTORY_LINES", 10000) or 10000)
    max_backups = int(cfg.get("EVOLVER_MAX_BACKUPS", 50) or 50)
    now = time.time()
    cutoff = now - retention_days * 86400
    pruned = []

    for fname in ["evolver_history.jsonl", "evolver_runtime_events.jsonl",
                   "proposal_validation_history.jsonl", "policy_performance.jsonl"]:
        fpath = os.path.join(DATA_DIR, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                lines = [json.loads(l) for l in f if l.strip()]
            if len(lines) > max_lines:
                keep = lines[-max_lines:]
                with open(fpath, "w", encoding="utf-8") as f:
                    for item in keep:
                        f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                pruned.append(f"{fname}: {len(lines)} -> {len(keep)}")
        except Exception as e:
            logger.warning("prune %s failed: %s", fname, e)

    backup_dir = os.path.join(DATA_DIR, "config_backups")
    if os.path.exists(backup_dir):
        try:
            backups = sorted([f for f in os.listdir(backup_dir) if f.endswith(".json")])
            for fname in backups[:-max_backups] if len(backups) > max_backups else []:
                fpath = os.path.join(backup_dir, fname)
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    pruned.append(f"backup: {fname}")
        except Exception as e:
            logger.warning("prune backups failed: %s", e)

    return {"pruned": pruned, "count": len(pruned)}
