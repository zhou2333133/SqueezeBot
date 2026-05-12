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
