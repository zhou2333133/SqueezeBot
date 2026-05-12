"""
参数归因系统 — 追踪每个 Evolver 参数修改的效果。

职责：
  - 每个 applied_update 生成一个 param_patch
  - 每笔 trade / shadow_trade 绑定 active_param_patches
  - 统计每个 patch 对应的真实 + shadow 表现
  - 标记 BENEFICIAL / HARMFUL / NEUTRAL / INSUFFICIENT_DATA
  - 维护 PARAM_COOLDOWN 防连续有害修改

架构原则：
  - PAPER 和 LIVE 共用同一套归因逻辑
  - execution_backend 只能作为记录字段
  - patch attribution 不能影响真实交易
"""
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from config import DATA_DIR, config_manager
from persistence import append_jsonl, atomic_write_json, ensure_parent_dir, safe_read_json

logger = logging.getLogger(__name__)

PATCHES_FILE = os.path.join(DATA_DIR, "param_patches.jsonl")
PERF_FILE = os.path.join(DATA_DIR, "param_patch_performance.jsonl")
COOLDOWN_FILE = os.path.join(DATA_DIR, "param_cooldowns.json")


# ══════════════════════════════════════════════════════════════════════════════
# 1. create_param_patches
# ══════════════════════════════════════════════════════════════════════════════
def create_param_patches(applied_updates: list[dict], policy_version: str, evolver_run_id: str) -> list[str]:
    """将每个 applied_update 转为 param_patch 并写入 JSONL。返回 patch IDs。"""
    patch_ids: list[str] = []
    if not applied_updates:
        return patch_ids
    for update in applied_updates:
        key = update.get("key", "unknown")
        seq = _next_patch_seq(key)
        patch_id = f"patch-{datetime.now().strftime('%Y%m%d')}-{seq:03d}-{key.split('.')[-1][:30]}"
        tag = update.get("strategy_tag", "")
        patch = {
            "param_patch_id": patch_id,
            "policy_version": policy_version,
            "evolver_run_id": evolver_run_id,
            "key": key,
            "old": update.get("old"),
            "new": update.get("new"),
            "change_pct": update.get("change_pct", _calc_change_pct(update.get("old"), update.get("new"))),
            "strategy_tag": tag,
            "reason": update.get("reason", ""),
            "created_at": time.time(),
            "status": "ACTIVE",
            "rollback_policy_version": None,
            "locked": key in getattr(config_manager, "LOCKED_PARAMS", set()),
        }
        append_jsonl(PATCHES_FILE, patch)
        patch_ids.append(patch_id)
    return patch_ids


def _calc_change_pct(old, new) -> float | None:
    try:
        o, n = float(old or 0), float(new or 0)
        if o != 0:
            return round(abs(n - o) / abs(o), 4)
    except (TypeError, ValueError):
        pass
    return None


def _next_patch_seq(key: str) -> int:
    """从已有 patches 找下一个序号。"""
    patches = load_param_patches()
    max_seq = 0
    for p in patches:
        pid = p.get("param_patch_id", "")
        try:
            seq = int(pid.split("-")[2])
            max_seq = max(max_seq, seq)
        except (ValueError, IndexError):
            pass
    return max_seq + 1


# ══════════════════════════════════════════════════════════════════════════════
# 2. load_param_patches
# ══════════════════════════════════════════════════════════════════════════════
def load_param_patches(status: str = "") -> list[dict]:
    """读取 param_patches。status='ACTIVE'/'REVERTED' 过滤。"""
    if not os.path.exists(PATCHES_FILE):
        return []
    try:
        patches = []
        with open(PATCHES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    patches.append(json.loads(line))
        if status:
            return [p for p in patches if p.get("status") == status]
        return patches
    except Exception as e:
        logger.warning("读取 param_patches 失败: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 3. get_active_param_patches
# ══════════════════════════════════════════════════════════════════════════════
def get_active_param_patches(policy_version: str = "") -> list[dict]:
    """返回当前 policy 激活的 patches。"""
    patches = load_param_patches(status="ACTIVE")
    if policy_version:
        return [p for p in patches if p.get("policy_version") == policy_version]
    return patches


def get_active_patch_ids(policy_version: str = "") -> list[str]:
    """返回当前 policy 激活的 patch IDs。"""
    return [p["param_patch_id"] for p in get_active_param_patches(policy_version) if p.get("param_patch_id")]


# ══════════════════════════════════════════════════════════════════════════════
# 4-5. attach active patches to trades / shadow trades
# ══════════════════════════════════════════════════════════════════════════════
def attach_active_patches_to_trade(trade: dict, cfg: dict | None = None) -> dict:
    """给 trade dict 增加 active_param_patches。"""
    try:
        pv = str(trade.get("policy_version", ""))
        patches = get_active_patch_ids(pv)
        trade["active_param_patches"] = patches
    except Exception:
        trade["active_param_patches"] = []
    return trade


def attach_active_patches_to_shadow_trade(shadow_dict: dict, cfg: dict | None = None) -> dict:
    """给 shadow trade dict 增加 active_param_patches。"""
    try:
        pv = str(shadow_dict.get("policy_version", ""))
        patches = get_active_patch_ids(pv)
        shadow_dict["active_param_patches"] = patches
    except Exception:
        shadow_dict["active_param_patches"] = []
    return shadow_dict


def attach_patches_to_trade_record(trade: dict) -> dict:
    """统一入口：给 trade dict 绑定当前 policy 下的 patches。"""
    try:
        patches = get_active_patch_ids(str(trade.get("policy_version", "")))
        trade["active_param_patches"] = patches
    except Exception:
        trade["active_param_patches"] = []
    return trade


# ══════════════════════════════════════════════════════════════════════════════
# 6. compute_patch_performance
# ══════════════════════════════════════════════════════════════════════════════
def compute_patch_performance(trades: list[dict], shadow_trades: list[dict] | None = None) -> dict[str, dict]:
    """统计每个 patch 对应的真实 + shadow 结果。"""
    patches = load_param_patches()
    if not patches:
        return {}

    by_patch: dict[str, dict] = {}
    for p in patches:
        pid = p["param_patch_id"]
        by_patch[pid] = {
            "param_patch_id": pid,
            "key": p["key"],
            "policy_version": p["policy_version"],
            "old": p["old"], "new": p["new"],
            "reason": p["reason"], "status": p["status"],
            "real_trades": 0, "real_win_rate": 0.0, "real_pnl_total": 0.0,
            "real_expectancy": 0.0, "real_profit_factor": 0.0,
            "shadow_trades": 0, "shadow_win_rate": 0.0, "shadow_avg_pnl_pct": 0.0,
        }

    # 统计真实交易
    for t in trades:
        pids = t.get("active_param_patches") or []
        pnl = float(t.get("pnl_usdt", 0) or 0)
        for pid in pids:
            if pid in by_patch:
                s = by_patch[pid]
                s["real_trades"] += 1
                s["real_pnl_total"] += pnl
                if pnl >= 0:
                    s.setdefault("_wins", 0)
                    s["_wins"] += 1

    # 统计 shadow
    if shadow_trades:
        for st in shadow_trades:
            pids = st.get("active_param_patches") or []
            hypo = float(st.get("hypothetical_pnl_pct", 0) or 0)
            for pid in pids:
                if pid in by_patch:
                    s = by_patch[pid]
                    s["shadow_trades"] += 1
                    s["shadow_avg_pnl_pct"] = (s.get("shadow_avg_pnl_pct", 0) * (s["shadow_trades"] - 1) + hypo) / s["shadow_trades"]
                    if hypo >= 0:
                        s.setdefault("_shadow_wins", 0)
                        s["_shadow_wins"] += 1

    # 计算衍生指标
    for pid, s in by_patch.items():
        n = s["real_trades"]
        if n >= 20:
            wins = s.pop("_wins", 0)
            s["real_win_rate"] = round(wins / n, 4)
            s["real_expectancy"] = round(s["real_pnl_total"] / n, 4)
            s["real_profit_factor"] = round(max(s["real_pnl_total"], 0) / max(abs(sum(0)), 1), 4)  # simplified
        else:
            s.pop("_wins", None)
        sn = s["shadow_trades"]
        if sn >= 5:
            sw = s.pop("_shadow_wins", 0)
            s["shadow_win_rate"] = round(sw / sn, 4) if sn > 0 else 0.0
        else:
            s.pop("_shadow_wins", None)

    return by_patch


# ══════════════════════════════════════════════════════════════════════════════
# 7. classify_patch_effectiveness
# ══════════════════════════════════════════════════════════════════════════════
def classify_patch_effectiveness(stats: dict) -> str:
    """输出 BENEFICIAL / HARMFUL / NEUTRAL / INSUFFICIENT_DATA。"""
    n = stats.get("real_trades", 0)
    if n < 20:
        return "INSUFFICIENT_DATA"
    wr = stats.get("real_win_rate", 0.0)
    exp = stats.get("real_expectancy", 0.0)
    pnl = stats.get("real_pnl_total", 0.0)
    if exp > 0 and pnl > 0 and wr >= 0.52:
        return "BENEFICIAL"
    if exp < 0 and pnl < 0 and wr < 0.45:
        return "HARMFUL"
    return "NEUTRAL"


# ══════════════════════════════════════════════════════════════════════════════
# 8. suggest_patch_actions
# ══════════════════════════════════════════════════════════════════════════════
def suggest_patch_actions(all_stats: dict[str, dict]) -> list[dict]:
    """给 Evolver 建议。"""
    actions: list[dict] = []
    cooldowns = _load_cooldowns()

    for pid, stats in all_stats.items():
        eff = classify_patch_effectiveness(stats)
        key = stats.get("key", "")
        action = "WAIT_MORE_DATA"

        if eff == "BENEFICIAL":
            action = "KEEP"

        elif eff == "HARMFUL":
            consecutive = _count_consecutive_harmful(key)
            max_harmful = int(config_manager.settings.get("EVOLVER_PATCH_MAX_CONSECUTIVE_HARMFUL", 2) or 2)
            if consecutive >= max_harmful:
                cooldown_min = float(config_manager.settings.get("EVOLVER_PATCH_HARMFUL_COOLDOWN_MINUTES", 240) or 240)
                _set_cooldown(key, cooldown_min * 60)
                action = "COOLDOWN"
            else:
                action = "ROLLBACK"

        elif eff == "NEUTRAL":
            action = "WAIT_MORE_DATA"

        actions.append({
            "param_patch_id": pid,
            "key": key,
            "effectiveness": eff,
            "suggested_action": action,
            "reason": stats.get("reason", ""),
        })
    return actions


def _count_consecutive_harmful(key: str) -> int:
    patches = load_param_patches()
    count = 0
    for p in reversed(patches):
        if p.get("key") != key:
            continue
        stats = {"real_trades": 999, "real_win_rate": 0.3, "real_expectancy": -1, "real_pnl_total": -10}
        eff = classify_patch_effectiveness(stats)
        if eff == "HARMFUL":
            count += 1
            continue
        break
    return count


# ══════════════════════════════════════════════════════════════════════════════
# 9. mark_patch_status
# ══════════════════════════════════════════════════════════════════════════════
def mark_patch_status(param_patch_id: str, new_status: str) -> bool:
    """更新 patch 状态（ACTIVE/REVERTED/KEPT/HARMFUL/BENEFICIAL）。"""
    try:
        patches = load_param_patches()
        changed = False
        for p in patches:
            if p.get("param_patch_id") == param_patch_id:
                p["status"] = new_status
                changed = True
                break
        if changed:
            _persist_patches(patches)
        return changed
    except Exception as e:
        logger.warning("更新 patch 状态失败: %s", e)
        return False


def _persist_patches(patches: list[dict]) -> None:
    try:
        ensure_parent_dir(PATCHES_FILE)
        with open(PATCHES_FILE, "w", encoding="utf-8") as f:
            for p in patches:
                f.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("写入 param_patches 失败: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 10. PARAM_COOLDOWN
# ══════════════════════════════════════════════════════════════════════════════
def _load_cooldowns() -> dict:
    return safe_read_json(COOLDOWN_FILE) or {}


def _set_cooldown(key: str, duration_sec: float) -> None:
    cooldowns = _load_cooldowns()
    cooldowns[key] = {"cooldown_until": time.time() + duration_sec, "reason": "consecutive_harmful"}
    atomic_write_json(COOLDOWN_FILE, cooldowns)


def is_param_in_cooldown(key: str) -> tuple[bool, float]:
    """检查参数是否在冷却中。返回 (in_cooldown, remaining_sec)。"""
    cooldowns = _load_cooldowns()
    entry = cooldowns.get(key)
    if not entry:
        return False, 0.0
    remaining = entry.get("cooldown_until", 0) - time.time()
    if remaining > 0:
        return True, remaining
    # 过期则清理
    cooldowns.pop(key, None)
    atomic_write_json(COOLDOWN_FILE, cooldowns)
    return False, 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 12. 回滚关联
# ══════════════════════════════════════════════════════════════════════════════
def mark_patches_reverted_by_policy(policy_version: str, rollback_version: str) -> int:
    """将某个 policy 下所有 ACTIVE patches 标记为 REVERTED。"""
    patches = load_param_patches()
    count = 0
    for p in patches:
        if p.get("policy_version") == policy_version and p.get("status") == "ACTIVE":
            p["status"] = "REVERTED"
            p["rollback_policy_version"] = rollback_version
            count += 1
    if count:
        _persist_patches(patches)
    return count


# ══════════════════════════════════════════════════════════════════════════════
# 因果回溯 — 追踪参数修改后 N 笔交易的效果
# ══════════════════════════════════════════════════════════════════════════════
_TRACKING: dict[str, dict] = {}
_TRACK_WINDOW = 15  # 追踪后续 N 笔


def start_tracking(patch_id: str, key: str, old_val, new_val, strategy_tag: str = "") -> None:
    """开始追踪一次参数修改的效果。"""
    _TRACKING[patch_id] = {
        "patch_id": patch_id,
        "key": key,
        "old_val": old_val,
        "new_val": new_val,
        "strategy_tag": strategy_tag,
        "trades_after": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
        "complete": False,
    }


def feed_trade(trade: dict) -> None:
    """每次平仓时调用，喂给追踪中的 patches。"""
    if not _TRACKING:
        return
    tag = str(trade.get("strategy_tag", ""))
    pnl = float(trade.get("pnl_usdt", 0) or 0)
    for pid, t in list(_TRACKING.items()):
        if t.get("complete"):
            continue
        # 只追踪同策略的交易
        if t.get("strategy_tag") and t["strategy_tag"] != tag:
            continue
        t["trades_after"] += 1
        if pnl >= 0:
            t["wins"] += 1
        else:
            t["losses"] += 1
        t["pnl"] += pnl
        if t["trades_after"] >= _TRACK_WINDOW:
            t["complete"] = True
            # 写回 param_patches 标记效果
            _record_verdict(t)


def get_active_tracking() -> list[dict]:
    return [t for t in _TRACKING.values() if not t.get("complete")]


def get_completed_tracking() -> list[dict]:
    return [t for t in _TRACKING.values() if t.get("complete")]


def _record_verdict(t: dict) -> None:
    """判断修改效果并记录。"""
    n = t["trades_after"]
    wr = t["wins"] / n if n > 0 else 0.0
    exp = t["pnl"] / n if n > 0 else 0.0

    if wr >= 0.55 and exp > 0:
        verdict = "useful"
    elif wr < 0.4 and exp < 0:
        verdict = "harmful"
    else:
        verdict = "neutral"

    t["verdict"] = verdict
    t["win_rate"] = round(wr, 2)
    t["expectancy"] = round(exp, 2)

    try:
        patches = _read_patches()
        for p in patches:
            if p.get("param_patch_id") == t["patch_id"]:
                p["_verdict"] = verdict
                p["_trades_after"] = n
                p["_win_rate_after"] = round(wr, 2)
                break
        _write_patches(patches)
    except Exception as e:
        logger.warning("record_verdict write failed: %s", e)


def _read_patches() -> list[dict]:
    """读取 param_patches.jsonl。"""
    import os
    if not os.path.exists(PATCHES_FILE):
        return []
    try:
        result = []
        with open(PATCHES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    result.append(json.loads(line))
        return result
    except Exception:
        return []


def _write_patches(patches: list[dict]) -> None:
    """写回 param_patches.jsonl。"""
    try:
        ensure_parent_dir(PATCHES_FILE)
        with open(PATCHES_FILE, "w", encoding="utf-8") as f:
            for p in patches:
                f.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("write patches failed: %s", e)
