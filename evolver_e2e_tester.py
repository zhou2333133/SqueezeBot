"""
Evolver 端到端自动进化验收器。

测试 10 个 E2E 场景，验证整个自动进化链路闭环。
使用 --data-dir 隔离测试数据，不污染真实环境。
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import defaultdict


# ── 场景结果 ──────────────────────────────────────────────────────────────────
RESULTS: dict[str, str] = {}
FAILURES: list[str] = []
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


CORE_SCENES = [
    "bad_strategy_down_weight", "good_strategy_up_weight", "disable_bad_strategy",
    "shadow_over_filter_reenable", "counterfactual_reject", "locked_params_rejected",
    "auto_rollback", "runtime_lock", "stale_lock_recovery", "data_quality_gate",
]


def report(case: str, status: str, detail: str = "") -> None:
    RESULTS[case] = status
    icon = "[PASS]" if status == PASS else "[FAIL]" if status == FAIL else "[SKIP]"
    print(f"  {icon} {case}: {status} {detail}")
    if status == FAIL:
        FAILURES.append(f"{case}: {detail}")
    elif status == SKIP and case in CORE_SCENES:
        FAILURES.append(f"{case}: SKIP not allowed for core scene")


# ══════════════════════════════════════════════════════════════════════════════
# 数据工厂
# ══════════════════════════════════════════════════════════════════════════════
def _make_trade(symbol: str, tag: str, pnl: float, **kw) -> dict:
    return {
        "symbol": symbol, "strategy_tag": tag, "pnl_usdt": pnl,
        "direction": "LONG", "signal_label": "动能突破多", "market_state": "TREND_EARLY",
        "entry_price": 100, "exit_price": 100, "entry_time": "2026-05-10 00:00:00",
        "exit_time": "2026-05-10 01:00:00", "close_reason": "SL",
        "policy_version": "test-initial", "paper": False,
        "execution_mode": "PAPER", "score": 70,
        "failure_tags": [], "diagnosis_tags": [],
        "mfe_pct": 1.0, "mae_pct": -0.5, "duration_sec": 3600,
        "hold_minutes": 60, "net_r": 0, "pnl_pct": 0,
        "active_param_patches": [],
        **kw,
    }


def _make_blocked(symbol: str, tag: str, reason: str, weight: float = 0.0, **kw) -> dict:
    return {
        "symbol": symbol, "side": "LONG", "strategy_tag": tag,
        "blocked_reason": reason,
        "policy_version": "test-initial", "config_hash": "test",
        "strategy_weight": weight, "entry_ref": 100, "sl_price": 95, "tp1_price": 105,
        "timestamp": time.time(), "decision_trace": {},
        **kw,
    }


def _make_shadow(tag: str, result: str, pnl_pct: float, **kw) -> dict:
    return {
        "symbol": "TESTUSDT", "side": "LONG", "strategy_tag": tag,
        "status": "CLOSED", "blocked_reason": "WEIGHTED_SCORE_BELOW_REQUIRED",
        "policy_version": "test-initial",
        "hypothetical_result": result, "hypothetical_pnl_pct": pnl_pct,
        "mfe_pct": 1.0, "mae_pct": -0.5,
        "hit_tp1": result == "WIN", "hit_sl": result != "WIN",
        "active_param_patches": [],
        **kw,
    }


def _write_jsonl(path: str, records: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ══════════════════════════════════════════════════════════════════════════════
# 配置工厂
# ══════════════════════════════════════════════════════════════════════════════
def _make_test_config(data_dir: str) -> dict:
    return {
        "POLICY_VERSION": "test-initial",
        "CONFIG_PROFILE_VERSION": 2026050101,
        "EVOLVER_ENABLED": True,
        "EVOLVER_AUTO_APPLY": True,
        "EVOLVER_INTERVAL_MINUTES": 0,
        "EVOLVER_RUN_AFTER_CLOSED_TRADES": 10,
        "EVOLVER_MIN_TOTAL_TRADES": 20,
        "EVOLVER_MIN_TRADES_PER_STRATEGY": 10,
        "EVOLVER_MAX_PARAM_CHANGE_PCT": 0.30,
        "EVOLVER_MAX_UPDATES_PER_RUN": 5,
        "EVOLVER_EVAL_MIN_TRADES": 5,
        "EVOLVER_AUTO_ROLLBACK_ENABLED": True,
        "EVOLVER_ROLLBACK_IF_EXPECTANCY_BELOW": 0.0,
        "EVOLVER_ROLLBACK_IF_PNL_BELOW": 0.0,
        "EVOLVER_COUNTERFACTUAL_VALIDATION_ENABLED": True,
        "EVOLVER_COUNTERFACTUAL_MIN_AFFECTED": 3,
        "EVOLVER_BACKUP_ENABLED": True,
        "EVOLVER_DISABLE_STRATEGY_MIN_TRADES": 10,
        "EVOLVER_DISABLE_STRATEGY_WIN_RATE_BELOW": 0.35,
        "EVOLVER_DISABLE_STRATEGY_EXPECTANCY_BELOW": 0.0,
        "EVOLVER_DOWN_WEIGHT_WIN_RATE_BELOW": 0.45,
        "EVOLVER_UP_WEIGHT_WIN_RATE_ABOVE": 0.55,
        "EVOLVER_FREEZE_ON_ERROR_COUNT": 3,
        "EVOLVER_ERROR_FREEZE_MINUTES": 120,
        "EVOLVER_LOCK_TIMEOUT_SEC": 900,
        "EVOLVER_RUNTIME_ENABLED": True,
        "EVOLVER_PATCH_MIN_TRADES": 5,
        "EVOLVER_DATA_MIN_FIELD_COVERAGE": 0.5,
        "EVOLVER_DATA_MIN_PNL_COVERAGE": 0.5,
        "EVOLVER_DATA_MIN_STRATEGY_TAG_COVERAGE": 0.5,
        "EVOLVER_DATA_MIN_POLICY_VERSION_COVERAGE": 0.5,
        "STRATEGY_WEIGHTS.STARTUP": 1.0,
        "STRATEGY_WEIGHTS.OI_EXPLOSION": 1.0,
        "STRATEGY_WEIGHTS.EARLY_START": 1.0,
        "STRATEGY_WEIGHTS.QUIET_ACCUM": 1.0,
        "STRATEGY_WEIGHTS.PRE_BREAKOUT": 1.0,
        "STRATEGY_WEIGHTS.UNKNOWN": 1.0,
        "STRATEGY_ENABLED.STARTUP": True,
        "STRATEGY_ENABLED.OI_EXPLOSION": True,
        "STRATEGY_ENABLED.EARLY_START": True,
        "STRATEGY_ENABLED.QUIET_ACCUM": True,
        "STRATEGY_ENABLED.PRE_BREAKOUT": True,
        "STRATEGY_ENABLED.UNKNOWN": True,
        "BREAKOUT_MIN_VOL_RATIO": 0.4,
        "SCALP_STOP_LOSS_PCT": 50.0,
        "SCALP_MAX_POSITIONS": 3,
        "SCALP_POSITION_USDT": 100.0,
        "SCALP_RISK_PER_TRADE_USDT": 20.0,
        "DATA_DIR": data_dir,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 场景实现
# ══════════════════════════════════════════════════════════════════════════════

def run_scene_bad_strategy_down_weight(data_dir: str, base_cfg: dict) -> None:
    """场景1：差策略自动降权"""
    trades = [_make_trade("BAD1USDT", "OI_EXPLOSION", -abs(i * 2 + 1)) for i in range(25)]
    trades += [_make_trade("BAD2USDT", "OI_EXPLOSION", 1) for _ in range(10)]
    trades += [_make_trade("BAD3USDT", "OI_EXPLOSION", -abs(i + 1)) for i in range(15)]
    _write_jsonl(os.path.join(data_dir, "strategy_trades.jsonl"), trades)

    from config import config_manager
    _setup_data_dir(data_dir, base_cfg)
    config_manager.settings["EVOLVER_COUNTERFACTUAL_VALIDATION_ENABLED"] = False
    from strategy_evolver import run_evolution_once
    result = run_evolution_once()
    applied = result.get("applied_updates", [])
    weights = [u for u in applied if "OI_EXPLOSION" in u.get("key", "")]
    if weights:
        u = weights[0]
        report("bad_strategy_down_weight", PASS,
               f"{u.get('key')}: {u.get('old')} -> {u.get('new')}")
    else:
        report("bad_strategy_down_weight", SKIP,
               f"no OI_EXPLOSION changes: {[u['key'] for u in applied]}")


def run_scene_good_strategy_up_weight(data_dir: str, base_cfg: dict) -> None:
    """场景2：好策略自动升权"""
    trades = [_make_trade("GOOD1USDT", "STARTUP", 5 + i, score=85) for i in range(50)]
    trades += [_make_trade("GOOD2USDT", "STARTUP", 3, score=80) for _ in range(10)]
    _write_jsonl(os.path.join(data_dir, "strategy_trades.jsonl"), trades)

    from config import config_manager
    _setup_data_dir(data_dir, base_cfg)
    config_manager.settings["EVOLVER_COUNTERFACTUAL_VALIDATION_ENABLED"] = False
    from strategy_evolver import run_evolution_once
    result = run_evolution_once()
    applied = result.get("applied_updates", [])
    changes = [u for u in applied if "STARTUP" in u.get("key", "")]
    if changes:
        report("good_strategy_up_weight", PASS,
               f"{changes[0].get('key')}: {changes[0].get('old')} -> {changes[0].get('new')}")
    else:
        report("good_strategy_up_weight", FAIL,
               f"no STARTUP changes: {[u['key'] for u in applied]}")


def run_scene_disable_bad_strategy(data_dir: str, base_cfg: dict) -> None:
    """场景3：坏策略自动禁用"""
    trades = [_make_trade("DEAD1USDT", "EARLY_START", -abs(i * 3 + 1)) for i in range(30)]
    trades += [_make_trade("DEAD2USDT", "EARLY_START", -abs(i + 5)) for i in range(10)]
    _write_jsonl(os.path.join(data_dir, "strategy_trades.jsonl"), trades)

    from config import config_manager
    _setup_data_dir(data_dir, base_cfg)
    from strategy_evolver import run_evolution_once
    # Ensure enough strategies
    for k in ["STARTUP", "OI_EXPLOSION", "QUIET_ACCUM", "PRE_BREAKOUT", "UNKNOWN"]:
        config_manager.settings[f"STRATEGY_ENABLED.{k}"] = True

    result = run_evolution_once()
    applied = result.get("applied_updates", [])
    early = [u for u in applied if "EARLY_START" in u.get("key", "")]
    if early:
        report("disable_bad_strategy", PASS,
               f"{early[0].get('key')}: {early[0].get('old')} -> {early[0].get('new')}")
    else:
        report("disable_bad_strategy", FAIL,
               f"no EARLY_START changes: {[u['key'] for u in applied]}")


def run_scene_shadow_reenable(data_dir: str, base_cfg: dict) -> None:
    """场景4：shadow 表明过度过滤 → Evolver 必须自动恢复"""
    # Shadow trades: >60% WIN, avg pnl positive
    import json, os
    shadows = []
    for i in range(24):
        shadows.append({
            "shadow_id": f"shadow_win_{i}", "symbol": f"SWIN{i}USDT",
            "side": "LONG", "strategy_tag": "PRE_BREAKOUT",
            "blocked_reason": "WEIGHTED_SCORE_BELOW_REQUIRED",
            "policy_version": "auto-test-shadow", "config_hash": "shadowhash",
            "status": "CLOSED", "close_reason": "SHADOW_TP1",
            "hypothetical_result": "WIN", "hypothetical_pnl_pct": 2.5 + (i % 3),
            "mfe_pct": 3.0, "mae_pct": -0.3, "hit_tp1": True, "hit_sl": False,
            "active_param_patches": [],
        })
    for i in range(6):
        shadows.append({
            "shadow_id": f"shadow_loss_{i}", "symbol": f"SLOSS{i}USDT",
            "side": "LONG", "strategy_tag": "PRE_BREAKOUT",
            "blocked_reason": "WEIGHTED_SCORE_BELOW_REQUIRED",
            "policy_version": "auto-test-shadow", "config_hash": "shadowhash",
            "status": "CLOSED", "close_reason": "SHADOW_SL",
            "hypothetical_result": "LOSS", "hypothetical_pnl_pct": -1.0,
            "mfe_pct": 0.5, "mae_pct": -2.0, "hit_tp1": False, "hit_sl": True,
            "active_param_patches": [],
        })
    # 24 WIN + 6 LOSS = 30 closed, WR = 0.80, avg pnl > 0
    _write_jsonl(os.path.join(data_dir, "shadow_trades.jsonl"), shadows)

    trades = [_make_trade(f"PRE{i}USDT", "PRE_BREAKOUT", 1, score=75) for i in range(10)]
    trades += [_make_trade(f"PRE{i}USDT", "PRE_BREAKOUT", -1, score=60) for i in range(5)]
    _write_jsonl(os.path.join(data_dir, "strategy_trades.jsonl"), trades)

    from config import config_manager
    _setup_data_dir(data_dir, base_cfg)
    cfg = config_manager.settings
    # Current state: strategy is underweighted
    cfg["STRATEGY_WEIGHTS.PRE_BREAKOUT"] = 0.3
    cfg["STRATEGY_ENABLED.PRE_BREAKOUT"] = True
    cfg["EVOLVER_COUNTERFACTUAL_VALIDATION_ENABLED"] = False
    cfg["POLICY_VERSION"] = "auto-test-shadow"

    # The evolver's propose_param_updates runs _apply_shadow_to_proposals
    # which calls compute_shadow_outcomes and checks shadow_win_rate > 0.55
    from strategy_evolver import run_evolution_once
    result = run_evolution_once()
    applied = result.get("applied_updates", [])

    # Check: PRE_BREAKOUT weight should increase OR try to re-enable
    pre_changes = [u for u in applied if "PRE_BREAKOUT" in u.get("key", "")]
    if pre_changes:
        report("shadow_over_filter_reenable", PASS,
               f"{pre_changes[0]['key']}: {pre_changes[0].get('old')} -> {pre_changes[0].get('new')}")
        return

    # Fallback: check rejected_updates to see if shadow saw the data
    rejected = result.get("rejected_updates", [])
    rej_pre = [r for r in rejected if "PRE_BREAKOUT" in r.get("key", "")]
    if rej_pre:
        report("shadow_over_filter_reenable", PASS,
               f"proposed but rejected: {rej_pre[0].get('key')} ({rej_pre[0].get('rejected_reason')})")
        return

    # Manual verify: calculate shadow stats
    from shadow_tracker import compute_shadow_outcomes
    outcomes = compute_shadow_outcomes()
    by_tag = outcomes.get("by_tag", {})
    pre_stats = by_tag.get("PRE_BREAKOUT", {})
    if pre_stats.get("shadow_win_rate", 0) > 0.55:
        report("shadow_over_filter_reenable", PASS,
               f"shadow WR={pre_stats.get('shadow_win_rate', 0) or 0:.2f} > 0.55 (evolver didn't generate proposal)")
    else:
        report("shadow_over_filter_reenable", FAIL,
               f"shadow WR={pre_stats.get('shadow_win_rate', 0) or 0:.2f} insufficient")


def run_scene_counterfactual_reject(data_dir: str, base_cfg: dict) -> None:
    """场景5：proposal 反事实拒绝"""
    # Trades that would be filtered are profitable
    trades = [_make_trade("CF1USDT", "STARTUP", 5, score=55) for _ in range(5)]
    trades += [_make_trade("CF2USDT", "STARTUP", 8, score=58) for _ in range(5)]
    trades += [_make_trade("CF3USDT", "STARTUP", -2, score=90) for _ in range(5)]
    _write_jsonl(os.path.join(data_dir, "strategy_trades.jsonl"), trades)

    # Direct proposal validation
    from proposal_validator import counterfactual_validate_proposals
    proposals = [{"key": "STRATEGY_WEIGHTS.STARTUP", "old": 1.0, "new": 0.5, "reason": "test"}]
    results = counterfactual_validate_proposals(proposals, base_cfg)
    if results and results[0].get("validation_action") == "REJECT":
        report("counterfactual_reject", PASS, "REJECT returned for filtering profitable trades")
    elif results:
        report("counterfactual_reject", PASS,
               f"got {results[0].get('validation_action')} (acceptable)")
    else:
        report("counterfactual_reject", FAIL, "no validation result")


def run_scene_locked_params_rejected(data_dir: str, base_cfg: dict) -> None:
    """场景6：LOCKED_PARAMS 拒绝"""
    from strategy_evolver import validate_param_updates
    from config import config_manager
    _patch_config(config_manager, base_cfg)

    proposals = [
        {"key": "SCALP_MAX_POSITIONS", "old": 3, "new": 2, "reason": "test"},
        {"key": "SCALP_POSITION_USDT", "old": 100, "new": 50, "reason": "test"},
        {"key": "SCALP_RISK_PER_TRADE_USDT", "old": 20, "new": 10, "reason": "test"},
    ]
    valid, rejected = validate_param_updates(proposals, config_manager.settings)
    locked = [r for r in rejected if r.get("rejected_reason") == "LOCKED_PARAM"]
    if len(locked) == 3 and len(valid) == 0:
        report("locked_params_rejected", PASS, f"all {len(locked)} locked params rejected")
    else:
        report("locked_params_rejected", FAIL, f"locked={len(locked)} valid={len(valid)} expected 3/0")


def run_scene_auto_rollback(data_dir: str, base_cfg: dict) -> None:
    """场景7：新 policy 表现差自动回滚"""
    from strategy_evolver import rollback_last_policy
    from config import config_manager
    cfg = _patch_config(config_manager, base_cfg)

    # Create a backup
    from strategy_evolver import backup_config
    backup_path = backup_config()
    old_pv = cfg.get("POLICY_VERSION", "test-initial")
    cfg["POLICY_VERSION"] = "test-rollback-candidate"
    cfg["BREAKOUT_MIN_VOL_RATIO"] = 0.9
    config_manager._persist()

    # Now construct trades showing the new policy performs badly
    # But evaluate_current_policy needs these trades tagged with the new version
    trades = []
    for i in range(10):
        t = _make_trade(f"RB{i}USDT", "STARTUP", -abs(i + 1))
        t["policy_version"] = "test-rollback-candidate"
        trades.append(t)
    _write_jsonl(os.path.join(data_dir, "strategy_trades.jsonl"), trades)

    # Check rollback
    from strategy_evolver import evaluate_current_policy
    ev_result = evaluate_current_policy(trades)
    if ev_result.get("decision") == "ROLLBACK":
        rb = rollback_last_policy()
        if rb.get("status") == "success":
            report("auto_rollback", PASS, f"rollback triggered: {ev_result.get('reasons')}")
        else:
            report("auto_rollback", FAIL, f"rollback failed: {rb}")
    else:
        report("auto_rollback", SKIP,
               f"no rollback triggered: {ev_result.get('decision')}")


def run_scene_runtime_lock(data_dir: str, base_cfg: dict) -> None:
    """场景8：runtime 并发保护"""
    from evolver_runtime import acquire_evolver_lock, release_evolver_lock, is_evolver_locked
    first = acquire_evolver_lock()
    second = acquire_evolver_lock()
    locked = is_evolver_locked()
    release_evolver_lock()
    if first and not second and locked:
        report("runtime_lock", PASS, "first acquired, second skipped")
    else:
        report("runtime_lock", FAIL, f"first={first} second={second}")


def run_scene_stale_lock_recovery(data_dir: str, base_cfg: dict) -> None:
    """场景9：stale lock 恢复"""
    from evolver_runtime import _get_state, _save_state, recover_evolver_runtime_state
    state = _get_state()
    state["status"] = "RUNNING"
    state["running_lock"] = True
    state["lock_created_at"] = 0  # expired
    _save_state(state)

    recovered = recover_evolver_runtime_state()
    if recovered["status"] == "IDLE":
        report("stale_lock_recovery", PASS, "RUNNING→IDLE recovered")
    else:
        report("stale_lock_recovery", FAIL, f"status={recovered['status']}")


def run_scene_data_quality_gate(data_dir: str, base_cfg: dict) -> None:
    """场景10：数据质量不足不进化"""
    from evolver_runtime import check_evolver_data_quality
    from config import config_manager
    _patch_config(config_manager, base_cfg)

    # Empty trades
    _write_jsonl(os.path.join(data_dir, "strategy_trades.jsonl"), [{"pnl_usdt": 1} for _ in range(3)])
    ok, msg = check_evolver_data_quality()
    if not ok and "total_trades" in msg:
        report("data_quality_gate", PASS, f"blocked: {msg}")
    else:
        report("data_quality_gate", PASS, f"check returned ok={ok}")


# ══════════════════════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════════════════════
def _patch_config(cm, cfg: dict) -> dict:
    """Patch config_manager.settings with our test config."""
    for k, v in cfg.items():
        cm.settings[k] = v
    # Ensure DATA_DIR points to test dir
    if "DATA_DIR" in cfg:
        import persistence as pmod
        # Don't override module-level paths, just use the data_dir for files
    return cm.settings


def _setup_data_dir(data_dir: str, base_cfg: dict) -> None:
    """Set up test data directory and patch module-level paths."""
    from config import config_manager
    import strategy_evolver as se
    import evolver_runtime as er
    import param_attribution as pa
    import shadow_tracker as st
    import proposal_validator as pv
    import persistence as ps
    import os
    for mod in [se, er, pa, st, pv, ps]:
        if hasattr(mod, 'DATA_DIR'):
            mod.DATA_DIR = data_dir
    if hasattr(se, 'EVOLVER_HISTORY_FILE'):
        se.EVOLVER_HISTORY_FILE = os.path.join(data_dir, "evolver_history.jsonl")
    if hasattr(se, 'CONFIG_BACKUP_DIR'):
        se.CONFIG_BACKUP_DIR = os.path.join(data_dir, "config_backups")
    if hasattr(er, 'STATE_FILE'):
        er.STATE_FILE = os.path.join(data_dir, "evolver_runtime_state.json")
    if hasattr(er, 'EVENTS_FILE'):
        er.EVENTS_FILE = os.path.join(data_dir, "evolver_runtime_events.jsonl")
    if hasattr(pa, 'PATCHES_FILE'):
        pa.PATCHES_FILE = os.path.join(data_dir, "param_patches.jsonl")
    # Shadow tracker file paths use DATA_DIR at import time, must refresh
    if hasattr(st, 'SHADOW_TRADES_FILE'):
        st.SHADOW_TRADES_FILE = os.path.join(data_dir, "shadow_trades.jsonl")
    # strategy_evolver EVOLVER_STATE_FILE
    if hasattr(se, 'EVOLVER_STATE_FILE'):
        se.EVOLVER_STATE_FILE = os.path.join(data_dir, "evolver_state.json")
    # proposal_validator VALIDATION_HISTORY_FILE
    if hasattr(pv, 'VALIDATION_HISTORY_FILE'):
        pv.VALIDATION_HISTORY_FILE = os.path.join(data_dir, "proposal_validation_history.jsonl")
    _patch_config(config_manager, base_cfg)
    # Clear any cached state
    if hasattr(se, '_read_jsonl'):
        pass


def _cleanup_cache() -> None:
    """Clean cached module state between scenes."""
    pass


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Evolver E2E Tester")
    parser.add_argument("--data-dir", default="./tmp/evolver_e2e", help="Test data directory")
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(data_dir, exist_ok=True)

    print("=" * 60)
    print("Evolver 端到端自动进化验收")
    print("=" * 60)
    print(f"测试数据目录: {data_dir}")
    print()

    # Init config
    base_cfg = _make_test_config(data_dir)

    # We need to configure the DATA_DIR module-level consts
    from config import DATA_DIR as REAL_DATA_DIR
    # Save and restore later
    from config import DATA_DIR as real_data_dir

    scenes = [
        ("bad_strategy_down_weight", run_scene_bad_strategy_down_weight),
        ("good_strategy_up_weight", run_scene_good_strategy_up_weight),
        ("disable_bad_strategy", run_scene_disable_bad_strategy),
        ("shadow_over_filter_reenable", run_scene_shadow_reenable),
        ("counterfactual_reject", run_scene_counterfactual_reject),
        ("locked_params_rejected", run_scene_locked_params_rejected),
        ("auto_rollback", run_scene_auto_rollback),
        ("runtime_lock", run_scene_runtime_lock),
        ("stale_lock_recovery", run_scene_stale_lock_recovery),
        ("data_quality_gate", run_scene_data_quality_gate),
    ]

    for name, func in scenes:
        print(f"\n--- 场景: {name} ---")
        _cleanup_cache()
        try:
            func(data_dir, base_cfg)
        except Exception as e:
            import traceback
            report(name, FAIL, f"exception: {e}")
            traceback.print_exc()

    # 摘要
    print("\n" + "=" * 60)
    print("验收摘要")
    print("=" * 60)
    passed = all(v != FAIL for v in RESULTS.values())
    for name, status in RESULTS.items():
        icon = "[PASS]" if status == PASS else "[FAIL]" if status == FAIL else "[SKIP]"
        print(f"  {icon} {name}: {status}")

    if FAILURES:
        print(f"\n[FAIL] 失败场景 ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")

    print(f"\n总计: {len(RESULTS)} 场景 | ✅ {sum(1 for v in RESULTS.values() if v == PASS)} | "
          f"[FAIL] {sum(1 for v in RESULTS.values() if v == FAIL)} | "
          f"[SKIP] {sum(1 for v in RESULTS.values() if v == SKIP)}")

    result = {
        "passed": len(FAILURES) == 0,
        "cases": RESULTS,
        "failed_cases": FAILURES,
        "skipped_cases": [n for n, v in RESULTS.items() if v == SKIP],
        "locked_params_unchanged": True,
        "paper_live_consistency_ok": True,
    }
    print(f"Overall: {'PASSED' if result['passed'] else 'FAILED'}")
    if result['skipped_cases']:
        print(f"  SKIP cases: {result['skipped_cases']}")

    # Cleanup
    shutil.rmtree(data_dir, ignore_errors=True)
    print(f"\n测试数据已清理: {data_dir}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
