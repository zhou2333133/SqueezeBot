"""
Evolver Runtime Supervisor — 自动进化运行守护器。

职责：
  - 管理 evolver 状态机（IDLE/RUNNING/FROZEN/ERROR）
  - 运行锁防并发
  - 数据质量 Gate
  - 运行前自检
  - 异常冻结 + 自动恢复
  - 非阻塞触发
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from config import DATA_DIR, config_manager
from persistence import append_jsonl, atomic_write_json, safe_read_json

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(DATA_DIR, "evolver_runtime_state.json")
EVENTS_FILE = os.path.join(DATA_DIR, "evolver_runtime_events.jsonl")


# ══════════════════════════════════════════════════════════════════════════════
# 状态管理
# ══════════════════════════════════════════════════════════════════════════════
def _default_state() -> dict:
    return {
        "status": "IDLE",
        "current_job_id": None,
        "last_job_id": None,
        "last_started_at": 0.0,
        "last_finished_at": 0.0,
        "last_error": "",
        "last_success": True,
        "consecutive_errors": 0,
        "frozen_until": 0.0,
        "freeze_reason": "",
        "running_lock": False,
        "lock_owner": "",
        "lock_created_at": 0.0,
        "last_policy_version": "",
        "last_config_hash": "",
        "last_skip_reason": "",
        "last_skip_at": 0.0,
        "trades_at_last_check": 0,
    }


def _get_state() -> dict:
    state = safe_read_json(STATE_FILE)
    if not isinstance(state, dict):
        state = {}
    defaults = _default_state()
    defaults.update(state)
    return defaults


def _save_state(state: dict) -> None:
    try:
        atomic_write_json(STATE_FILE, state)
    except Exception as e:
        logger.warning("写入 evolver state 失败: %s", e)


def _write_event(event_type: str, state: dict, **extra) -> None:
    try:
        record = {
            "event_type": event_type,
            "job_id": state.get("current_job_id") or state.get("last_job_id", ""),
            "created_at": time.time(),
            "status_before": state.get("status", "UNKNOWN"),
            "status_after": state.get("status", "UNKNOWN"),
            "policy_version": state.get("last_policy_version", ""),
            "config_hash": state.get("last_config_hash", ""),
            "error": state.get("last_error", ""),
            **extra,
        }
        append_jsonl(EVENTS_FILE, record)
    except Exception as e:
        logger.debug("写入 runtime event 失败: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 恢复
# ══════════════════════════════════════════════════════════════════════════════
def recover_evolver_runtime_state() -> dict:
    """启动恢复：检查 stale lock / frozen / 损坏状态。"""
    state = _get_state()
    changed = False

    # 安全上限：frozen_until 超过 24 小时视为异常，自动缩短
    MAX_FREEZE_SEC = 86400  # 24 小时
    frozen_until = state.get("frozen_until", 0.0)
    if frozen_until > time.time() + MAX_FREEZE_SEC:
        logger.warning("Evolver 状态修复: frozen_until=%s 超过24小时上限，重置为 %d 秒",
                       frozen_until, MAX_FREEZE_SEC)
        state["frozen_until"] = time.time() + MAX_FREEZE_SEC
        changed = True

    if state.get("status") == "RUNNING":
        lock_timeout = float(config_manager.settings.get("EVOLVER_LOCK_TIMEOUT_SEC", 900) or 900)
        lock_ts = state.get("lock_created_at", 0.0)
        if time.time() - lock_ts > lock_timeout:
            logger.warning("Evolver 恢复: stale RUNNING → IDLE (lock超时)")
            state["status"] = "IDLE"
            state["running_lock"] = False
            state["lock_owner"] = ""
            state["consecutive_errors"] = 0
            changed = True
            _write_event("RECOVERED", state, reason="stale_lock")

    if state.get("status") == "FROZEN":
        frozen_until = state.get("frozen_until", 0.0)
        if time.time() >= frozen_until:
            logger.warning("Evolver 恢复: FROZEN → IDLE (frozen到期)")
            state["status"] = "IDLE"
            state["frozen_until"] = 0.0
            state["freeze_reason"] = ""
            state["consecutive_errors"] = 0
            changed = True
            _write_event("RECOVERED", state, reason="frozen_expired")

    if changed:
        _save_state(state)
    return state


# ══════════════════════════════════════════════════════════════════════════════
# 运行锁
# ══════════════════════════════════════════════════════════════════════════════
def acquire_evolver_lock() -> bool:
    """尝试获取运行锁。返回是否成功。"""
    state = _get_state()
    if state.get("running_lock", False):
        lock_timeout = float(config_manager.settings.get("EVOLVER_LOCK_TIMEOUT_SEC", 900) or 900)
        lock_ts = state.get("lock_created_at", 0.0)
        if time.time() - lock_ts < lock_timeout:
            return False
        # stale lock — 恢复
        logger.warning("Evolver lock stale, recovering")
        state["running_lock"] = False
        state["lock_owner"] = ""

    job_id = f"evojob-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    state["running_lock"] = True
    state["lock_owner"] = job_id
    state["lock_created_at"] = time.time()
    state["current_job_id"] = job_id
    _save_state(state)
    return True


def release_evolver_lock() -> None:
    """释放运行锁。"""
    state = _get_state()
    state["running_lock"] = False
    state["lock_owner"] = ""
    state["current_job_id"] = None
    _save_state(state)


def is_evolver_locked() -> bool:
    """检查是否被锁定。"""
    state = _get_state()
    if not state.get("running_lock", False):
        return False
    lock_timeout = float(config_manager.settings.get("EVOLVER_LOCK_TIMEOUT_SEC", 900) or 900)
    lock_ts = state.get("lock_created_at", 0.0)
    return time.time() - lock_ts < lock_timeout


def recover_stale_lock() -> bool:
    """恢复 stale lock。返回是否有锁被恢复。"""
    state = _get_state()
    if not state.get("running_lock", False):
        return False
    lock_timeout = float(config_manager.settings.get("EVOLVER_LOCK_TIMEOUT_SEC", 900) or 900)
    lock_ts = state.get("lock_created_at", 0.0)
    if time.time() - lock_ts > lock_timeout:
        state["running_lock"] = False
        state["lock_owner"] = ""
        _save_state(state)
        _write_event("RECOVERED", state, reason="stale_lock_recovered")
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# 数据质量 Gate
# ══════════════════════════════════════════════════════════════════════════════
def check_evolver_data_quality() -> tuple[bool, str]:
    """检查历史数据质量。返回 (pass, reason)。"""
    from strategy_evolver import load_trade_data
    trades = load_trade_data()
    n = len(trades)

    cfg = config_manager.settings
    min_total = int(cfg.get("EVOLVER_MIN_TOTAL_TRADES", 50) or 50)
    min_field_cov = float(cfg.get("EVOLVER_DATA_MIN_FIELD_COVERAGE", 0.70) or 0.70)
    min_pnl_cov = float(cfg.get("EVOLVER_DATA_MIN_PNL_COVERAGE", 0.95) or 0.95)
    min_tag_cov = float(cfg.get("EVOLVER_DATA_MIN_STRATEGY_TAG_COVERAGE", 0.95) or 0.95)
    min_pv_cov = float(cfg.get("EVOLVER_DATA_MIN_POLICY_VERSION_COVERAGE", 0.90) or 0.90)

    if n < min_total:
        return False, f"total_trades={n}<{min_total}"

    # 字段覆盖率
    with_pnl = sum(1 for t in trades if t.get("pnl_usdt") is not None)
    with_tag = sum(1 for t in trades if t.get("strategy_tag"))
    with_pv = sum(1 for t in trades if t.get("policy_version"))
    patches = sum(1 for t in trades if t.get("active_param_patches") is not None)

    if with_pnl / max(n, 1) < min_pnl_cov:
        return False, f"pnl_coverage={with_pnl}/{n}"
    if with_tag / max(n, 1) < min_tag_cov:
        return False, f"tag_coverage={with_tag}/{n}"
    if with_pv / max(n, 1) < min_pv_cov:
        return False, f"policy_version_coverage={with_pv}/{n}"
    if patches / max(n, 1) < min_field_cov:
        return False, f"param_patch_coverage={patches}/{n}"

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# 运行前自检
# ══════════════════════════════════════════════════════════════════════════════
def preflight_evolver_check() -> tuple[bool, str]:
    """运行前自检。"""
    cfg = config_manager.settings

    if not cfg.get("EVOLVER_ENABLED", True):
        return False, "EVOLVER_ENABLED=False"

    if not hasattr(config_manager, "LOCKED_PARAMS"):
        return False, "LOCKED_PARAMS missing"

    if not hasattr(config_manager, "PARAM_BOUNDS"):
        return False, "PARAM_BOUNDS missing"

    trades_file = os.path.join(DATA_DIR, "strategy_trades.jsonl")
    if not os.path.exists(trades_file):
        return False, "strategy_trades.jsonl not found"

    try:
        from strategy_evolver import run_evolution_once
        from deprecated.proposal_validator import counterfactual_validate_proposals
        from param_attribution import create_param_patches
        from shadow_tracker import create_shadow_trade
    except ImportError as e:
        return False, f"import_error: {e}"

    state = _get_state()
    if state.get("status") == "FROZEN":
        frozen_until = state.get("frozen_until", 0.0)
        if time.time() < frozen_until:
            return False, f"frozen_until_{frozen_until - time.time():.0f}s"

    if state.get("pending_evaluation", False):
        return False, "pending_evaluation"

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# 运行流程
# ══════════════════════════════════════════════════════════════════════════════
def run_evolver_job() -> dict:
    """执行完整 evolver job，含自检+锁+state+事件。返回结果。"""
    result: dict = {"job_executed": False, "status": "SKIPPED", "reason": ""}
    state = _get_state()

    try:
        # 自检
        ok, msg = preflight_evolver_check()
        if not ok:
            result["reason"] = f"preflight:{msg}"
            _write_event("SKIPPED", state, reason=result["reason"])
            return result

        # 数据质量
        ok, msg = check_evolver_data_quality()
        if not ok:
            result["reason"] = f"data_quality:{msg}"
            _write_event("SKIPPED", state, reason=result["reason"])
            return result

        # 锁
        if not acquire_evolver_lock():
            result["reason"] = "locked"
            _write_event("SKIPPED", state, reason="locked")
            return result

        state = _get_state()
        state["status"] = "RUNNING"
        state["last_started_at"] = time.time()
        state["last_error"] = ""
        _save_state(state)
        _write_event("STARTED", state)

        # 运行
        evo_result = run_evolution_once()
        duration = time.time() - state.get("last_started_at", time.time())
        max_runtime = float(config_manager.settings.get("EVOLVER_MAX_RUNTIME_SEC", 600) or 600)

        if evo_result.get("status") == "applied":
            state["status"] = "IDLE"
            state["last_success"] = True
            state["consecutive_errors"] = 0
            state["last_finished_at"] = time.time()
            state["last_policy_version"] = evo_result.get("policy_version", "")
            state["last_config_hash"] = evo_result.get("config_hash", "")
            _save_state(state)
            _write_event("SUCCESS", state, duration_sec=round(duration, 2),
                         updates=len(evo_result.get("applied_updates", [])))
            result["job_executed"] = True
            result["status"] = "SUCCESS"
            result["policy_version"] = state["last_policy_version"]
        else:
            state["status"] = "IDLE"
            state["last_success"] = True
            state["last_finished_at"] = time.time()
            _save_state(state)
            _write_event("SUCCESS", state, duration_sec=round(duration, 2),
                         message=evo_result.get("message", "no_changes"))
            result["status"] = "NO_CHANGES"
            result["reason"] = evo_result.get("message", "")

    except Exception as e:
        duration = time.time() - state.get("last_started_at", time.time())
        logger.error("Evolver job 异常: %s", e, exc_info=True)
        state = _get_state()
        state["status"] = "ERROR"
        state["last_error"] = str(e)
        state["last_success"] = False
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        state["last_finished_at"] = time.time()
        freeze_count = int(config_manager.settings.get("EVOLVER_FREEZE_ON_ERROR_COUNT", 3) or 3)
        freeze_min = float(config_manager.settings.get("EVOLVER_ERROR_FREEZE_MINUTES", 120) or 120)
        if state["consecutive_errors"] >= freeze_count:
            state["status"] = "FROZEN"
            # 保护：frozen_until 最多 24 小时，防止配置异常导致永久冻结
            freeze_sec = min(freeze_min * 60, 86400)
            state["frozen_until"] = time.time() + freeze_sec
            state["freeze_reason"] = f"{state['consecutive_errors']} consecutive errors"
            logger.critical("Evolver 已冻结 %d 分钟", freeze_min)
        _save_state(state)
        _write_event("ERROR" if state["status"] != "FROZEN" else "FROZEN", state,
                     duration_sec=round(duration, 2), error=str(e))
        result["status"] = state["status"]
        result["reason"] = str(e)

    finally:
        release_evolver_lock()

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 非阻塞调度
# ══════════════════════════════════════════════════════════════════════════════
_last_schedule_ts: float = 0.0
_scheduled_count: int = 0
_last_job_ts: float = 0.0


def _save_skip_reason(reason: str, trades: int = 0) -> None:
    """将调度跳过原因写入 runtime state，供复盘包读取。"""
    try:
        state = _get_state()
        state["last_skip_reason"] = str(reason)[:240]
        state["last_skip_at"] = time.time()
        state["trades_at_last_check"] = int(trades)
        _save_state(state)
    except Exception:
        pass


def maybe_schedule_evolver_job(cfg: dict | None = None) -> dict:
    """轻量触发器：检查条件，满足则异步安排 evolver job。"""
    global _last_schedule_ts, _scheduled_count, _last_job_ts

    result: dict = {"scheduled": False, "reason": ""}
    try:
        if cfg is None:
            cfg = config_manager.settings

        # 恢复状态
        recover_evolver_runtime_state()

        # 启动恢复：内存计数器为 0 且持久化有值 → 从 evolver_state 恢复
        if _scheduled_count == 0:
            try:
                from strategy_evolver import get_evolver_state_snapshot
                _evo_snap = get_evolver_state_snapshot()
                _saved_trades = _evo_snap.get("trades_since_last_policy", 0)
                if _saved_trades > 0:
                    logger.info("Evolver 从持久化状态恢复计数: %d 笔", _saved_trades)
                    _scheduled_count = _saved_trades
            except Exception:
                pass

        if not cfg.get("EVOLVER_RUNTIME_ENABLED", True):
            result["reason"] = "runtime_disabled"
            _save_skip_reason(result["reason"])
            return result

        if is_evolver_locked():
            result["reason"] = "locked"
            _save_skip_reason(result["reason"])
            return result

        state = _get_state()
        if state.get("status") in ("RUNNING", "FROZEN"):
            result["reason"] = f"status={state['status']}"
            _save_skip_reason(result["reason"], _scheduled_count)
            return result

        now = time.time()
        interval = float(cfg.get("EVOLVER_INTERVAL_MINUTES", 60) or 60)
        if now - _last_job_ts < interval * 60:
            result["reason"] = f"interval_{interval}m"
            _save_skip_reason(result["reason"], _scheduled_count)
            return result

        after_trades = int(cfg.get("EVOLVER_RUN_AFTER_CLOSED_TRADES", 30) or 30)
        if _scheduled_count < after_trades:
            result["reason"] = f"trades_{_scheduled_count}<{after_trades}"
            _save_skip_reason(result["reason"], _scheduled_count)
            return result

        # 触发 job
        _last_schedule_ts = now
        _last_job_ts = now
        _scheduled_count = 0

        # 同步执行（异步支持后续可加）
        job_result = run_evolver_job()
        result["scheduled"] = True
        result["job_result"] = job_result
        return result

    except Exception as e:
        logger.warning("Evolver 调度异常: %s", e)
        result["reason"] = f"exception:{e}"
        _save_skip_reason(result["reason"])
        return result


def mark_trade_closed() -> None:
    """每笔平仓后调用，用于计数。"""
    global _scheduled_count
    _scheduled_count += 1
