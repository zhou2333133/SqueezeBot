"""
Shadow Trade Tracker — 影子成交回放器。

追踪被策略拦截信号的假设结果，判断拦是否避免亏损或错过盈利。

架构原则：
  - shadow trade 不允许真实下单
  - shadow trade 不允许进入 open_positions
  - PAPER 和 LIVE 共用同一套 shadow 逻辑
  - execution_backend 只能作为记录字段
"""
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

from config import DATA_DIR, config_manager
from persistence import append_jsonl, read_jsonl

logger = logging.getLogger(__name__)

SHADOW_TRADES_FILE = os.path.join(DATA_DIR, "shadow_trades.jsonl")


# ══════════════════════════════════════════════════════════════════════════════
# 创建
# ══════════════════════════════════════════════════════════════════════════════
def create_shadow_trade(blocked_signal: dict) -> dict | None:
    """从被拦截信号创建 shadow trade。含去重。"""
    try:
        if not config_manager.settings.get("SHADOW_TRACKER_ENABLED", True):
            return None
        source_id = str(blocked_signal.get("source_signal_id") or blocked_signal.get("signal_id", ""))
        # 去重检查
        if source_id and shadow_trade_exists(source_id):
            return None
        entry_ref = float(blocked_signal.get("entry_ref", blocked_signal.get("entry_price", 0)) or 0)
        side = str(blocked_signal.get("side", "LONG")).upper()
        if side not in ("LONG", "SHORT"):
            side = "LONG"
        shadow = {
            "shadow_id": _make_shadow_id(blocked_signal, source_id),
            "source_signal_id": source_id,
            "symbol": str(blocked_signal.get("symbol", "")),
            "side": side,
            "strategy_tag": str(blocked_signal.get("strategy_tag", "UNKNOWN")),
            "blocked_reason": str(blocked_signal.get("blocked_reason", "UNKNOWN")),
            "policy_version": str(blocked_signal.get("policy_version", "")),
            "config_hash": str(blocked_signal.get("config_hash", "")),
            "evolver_run_id": str(blocked_signal.get("evolver_run_id", "")),
            "strategy_weight": float(blocked_signal.get("strategy_weight", 1.0) or 1.0),
            "raw_score": blocked_signal.get("raw_score"),
            "weighted_score": blocked_signal.get("weighted_score"),
            "required_score": blocked_signal.get("required_score"),
            "entry_time": time.time(),
            "entry_ref": entry_ref,
            "planned_stop_loss": float(blocked_signal.get("planned_stop_loss",
                                    blocked_signal.get("stop_loss", blocked_signal.get("sl_price", 0))) or 0),
            "planned_take_profit_1": float(blocked_signal.get("planned_take_profit_1",
                                           blocked_signal.get("take_profit_1", blocked_signal.get("tp1_price", 0))) or 0),
            "planned_take_profit_2": float(blocked_signal.get("planned_take_profit_2",
                                           blocked_signal.get("take_profit_2", blocked_signal.get("tp2_price", 0))) or 0),
            "planned_take_profit_3": float(blocked_signal.get("planned_take_profit_3",
                                           blocked_signal.get("take_profit_3", 0)) or 0),
            "status": "OPEN",
            "current_price": 0.0,
            "highest_price": entry_ref,
            "lowest_price": entry_ref,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "hit_sl": False,
            "best_hit": "NONE",
            "close_time": None,
            "close_price": None,
            "close_reason": None,
            "hypothetical_pnl_pct": None,
            "hypothetical_result": "UNKNOWN",
            "last_price_missing_at": None,
            "decision_trace": blocked_signal.get("decision_trace", {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            from param_attribution import attach_active_patches_to_shadow_trade
            attach_active_patches_to_shadow_trade(shadow)
        except Exception:
            pass
        append_jsonl(SHADOW_TRADES_FILE, shadow)
        return shadow
    except Exception as e:
        logger.warning("创建 shadow trade 失败: %s", e)
        return None


def _make_shadow_id(signal: dict, source_id: str) -> str:
    raw = source_id or f"{signal.get('symbol', '')}:{signal.get('side', '')}:{time.time()}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def shadow_trade_exists(source_signal_id: str) -> bool:
    """去重：检查 source_signal_id 是否已有 shadow trade。"""
    if not source_signal_id:
        return False
    try:
        all_trades = read_jsonl(SHADOW_TRADES_FILE)
        for t in all_trades:
            if str(t.get("source_signal_id", "")) == source_signal_id:
                return True
    except Exception:
        pass
    return False


# ══════════════════════════════════════════════════════════════════════════════
# 加载
# ══════════════════════════════════════════════════════════════════════════════
def load_shadow_trades(status: str = "") -> list[dict]:
    """读取 shadow trades。status='OPEN'/'CLOSED' 过滤。"""
    all_trades = read_jsonl(SHADOW_TRADES_FILE)
    if status:
        return [t for t in all_trades if t.get("status") == status]
    return all_trades


# ══════════════════════════════════════════════════════════════════════════════
# 价格更新 + 关闭逻辑
# ══════════════════════════════════════════════════════════════════════════════
def update_shadow_trades(market_prices: dict[str, float]) -> list[dict]:
    """用最新行情更新所有 OPEN shadow trades。返回本轮关闭的 trades。"""
    closed: list[dict] = []
    trades = load_shadow_trades(status="OPEN")
    if not trades:
        return closed

    cfg = config_manager.settings
    max_hold = float(cfg.get("SHADOW_MAX_HOLD_MINUTES", 240) or 240)
    close_on_tp1 = cfg.get("SHADOW_CLOSE_ON_TP1", True)
    close_on_tp2 = cfg.get("SHADOW_CLOSE_ON_TP2", True)
    close_on_tp3 = cfg.get("SHADOW_CLOSE_ON_TP3", True)
    now = time.time()
    remaining: list[dict] = []

    for shadow in trades:
        symbol = shadow.get("symbol", "")
        price = market_prices.get(symbol, 0.0)
        if price <= 0:
            shadow["last_price_missing_at"] = now
            remaining.append(shadow)
            continue

        shadow["current_price"] = price
        entry = shadow.get("entry_ref", 0)
        side = shadow.get("side", "LONG")
        close_reason = None

        # MFE/MAE: 全程跟踪最高最低价
        if entry > 0:
            prev_high = shadow.get("highest_price", entry)
            prev_low = shadow.get("lowest_price", entry)
            shadow["highest_price"] = max(prev_high, price)
            shadow["lowest_price"] = min(prev_low, price)
            hp = shadow["highest_price"]
            lp = shadow["lowest_price"]
            if side == "LONG":
                shadow["mfe_pct"] = round(max(shadow["mfe_pct"], (hp - entry) / entry * 100), 4)
                shadow["mae_pct"] = round(min(shadow["mae_pct"], (lp - entry) / entry * 100), 4)
            else:
                shadow["mfe_pct"] = round(max(shadow["mfe_pct"], (entry - lp) / entry * 100), 4)
                shadow["mae_pct"] = round(min(shadow["mae_pct"], (entry - hp) / entry * 100), 4)

        # 分层 TP/SL 检查（按优先级：SL > TP3 > TP2 > TP1）
        sl = shadow.get("planned_stop_loss", 0)
        tp1 = shadow.get("planned_take_profit_1", 0)
        tp2 = shadow.get("planned_take_profit_2", 0)
        tp3 = shadow.get("planned_take_profit_3", 0)

        # 先记录曾经触发的 TP（不立即关闭）
        if side == "LONG":
            if tp3 > 0 and price >= tp3:
                shadow["hit_tp3"] = True
            if tp2 > 0 and price >= tp2:
                shadow["hit_tp2"] = True
            if tp1 > 0 and price >= tp1:
                shadow["hit_tp1"] = True
            if sl > 0 and price <= sl:
                shadow["hit_sl"] = True
        else:
            if tp3 > 0 and price <= tp3:
                shadow["hit_tp3"] = True
            if tp2 > 0 and price <= tp2:
                shadow["hit_tp2"] = True
            if tp1 > 0 and price <= tp1:
                shadow["hit_tp1"] = True
            if sl > 0 and price >= sl:
                shadow["hit_sl"] = True

        # 更新 best_hit
        for level, flag in [("TP3", shadow["hit_tp3"]), ("TP2", shadow["hit_tp2"]),
                            ("TP1", shadow["hit_tp1"]), ("SL", shadow["hit_sl"])]:
            if flag:
                shadow["best_hit"] = level

        # 关闭判断：LONG
        if side == "LONG":
            if sl > 0 and price <= sl:
                close_reason = "SHADOW_SL"
            elif close_on_tp3 and tp3 > 0 and price >= tp3:
                close_reason = "SHADOW_TP3"
            elif close_on_tp2 and tp2 > 0 and price >= tp2:
                close_reason = "SHADOW_TP2"
            elif close_on_tp1 and tp1 > 0 and price >= tp1:
                close_reason = "SHADOW_TP1"
        else:  # SHORT
            if sl > 0 and price >= sl:
                close_reason = "SHADOW_SL"
            elif close_on_tp3 and tp3 > 0 and price <= tp3:
                close_reason = "SHADOW_TP3"
            elif close_on_tp2 and tp2 > 0 and price <= tp2:
                close_reason = "SHADOW_TP2"
            elif close_on_tp1 and tp1 > 0 and price <= tp1:
                close_reason = "SHADOW_TP1"

        # 超时检查
        elapsed_min = (now - shadow.get("entry_time", now)) / 60
        if not close_reason and elapsed_min > max_hold:
            close_reason = "SHADOW_TIMEOUT"

        if close_reason:
            shadow["status"] = "CLOSED"
            shadow["close_time"] = now
            shadow["close_price"] = price
            shadow["close_reason"] = close_reason
            # 假设 PnL
            if entry > 0:
                if side == "LONG":
                    hypo = (price - entry) / entry * 100
                else:
                    hypo = (entry - price) / entry * 100
                shadow["hypothetical_pnl_pct"] = round(hypo, 4)
            shadow["hypothetical_result"] = "WIN" if (shadow.get("hypothetical_pnl_pct") or 0) > 0 else "LOSS"
            closed.append(shadow)
        else:
            remaining.append(shadow)

    # 持久化
    _persist_shadow_trades(remaining + _load_closed_trades() + closed)
    return closed


# ══════════════════════════════════════════════════════════════════════════════
# 持久化
# ══════════════════════════════════════════════════════════════════════════════
def _load_closed_trades() -> list[dict]:
    return read_jsonl(SHADOW_TRADES_FILE)


def _persist_shadow_trades(trades: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(SHADOW_TRADES_FILE), exist_ok=True)
        with open(SHADOW_TRADES_FILE, "w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("写入 shadow_trades 失败: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# Shadow outcome 统计
# ══════════════════════════════════════════════════════════════════════════════
def compute_shadow_outcomes() -> dict[str, dict]:
    """增强型 shadow 统计：按 strategy_tag + blocked_reason。"""
    all_trades = read_jsonl(SHADOW_TRADES_FILE)
    closed = [t for t in all_trades if t.get("status") == "CLOSED"]
    open_trades = [t for t in all_trades if t.get("status") == "OPEN"]

    by_tag: dict[str, dict] = {}
    by_reason: dict[str, dict] = {}
    by_policy: dict[str, dict] = {}

    for t in closed:
        tag = str(t.get("strategy_tag", "UNKNOWN"))
        reason = str(t.get("blocked_reason", "UNKNOWN"))
        policy = str(t.get("policy_version", "UNKNOWN"))

        for bucket, key in [(by_tag, tag), (by_reason, reason), (by_policy, policy)]:
            if key not in bucket:
                bucket[key] = _empty_shadow_bucket()
            _accumulate_shadow(bucket[key], t)

    for t in open_trades:
        tag = str(t.get("strategy_tag", "UNKNOWN"))
        if tag not in by_tag:
            by_tag[tag] = _empty_shadow_bucket()

    result = {}
    for label, bucket in [("by_tag", by_tag), ("by_reason", by_reason), ("by_policy", by_policy)]:
        processed = {}
        for key, s in bucket.items():
            closed_count = s["shadow_closed"]
            s["shadow_win_rate"] = round(s["would_have_won"] / closed_count, 4) if closed_count > 0 else 0.0
            s["avg_hypothetical_pnl_pct"] = round(s["total_hypothetical_pnl"] / closed_count, 4) if closed_count > 0 else 0.0
            s["avg_mfe_pct"] = round(s["total_mfe"] / closed_count, 4) if closed_count > 0 else 0.0
            s["avg_mae_pct"] = round(s["total_mae"] / closed_count, 4) if closed_count > 0 else 0.0
            s["tp1_hit_rate"] = round(s["tp1_hits"] / closed_count, 4) if closed_count > 0 else 0.0
            s["tp2_hit_rate"] = round(s["tp2_hits"] / closed_count, 4) if closed_count > 0 else 0.0
            s["tp3_hit_rate"] = round(s["tp3_hits"] / closed_count, 4) if closed_count > 0 else 0.0
            s["sl_hit_rate"] = round(s["sl_hits"] / closed_count, 4) if closed_count > 0 else 0.0
            del s["total_hypothetical_pnl"]
            del s["total_mfe"]
            del s["total_mae"]
            processed[key] = s
        result[label] = processed

    return result


def _empty_shadow_bucket() -> dict:
    return {
        "blocked_count": 0, "shadow_closed": 0, "shadow_open": 0,
        "would_have_won": 0, "would_have_lost": 0,
        "total_hypothetical_pnl": 0.0, "total_mfe": 0.0, "total_mae": 0.0,
        "tp1_hits": 0, "tp2_hits": 0, "tp3_hits": 0, "sl_hits": 0,
    }


def _accumulate_shadow(bucket: dict, t: dict) -> None:
    bucket["shadow_closed"] += 1
    result = str(t.get("hypothetical_result", "UNKNOWN"))
    pnl = float(t.get("hypothetical_pnl_pct", 0) or 0)
    mfe = float(t.get("mfe_pct", 0) or 0)
    mae = float(t.get("mae_pct", 0) or 0)
    bucket["total_hypothetical_pnl"] += pnl
    bucket["total_mfe"] += max(0, mfe)
    bucket["total_mae"] += min(0, mae)
    if result == "WIN":
        bucket["would_have_won"] += 1
    else:
        bucket["would_have_lost"] += 1
    if t.get("hit_tp1"):
        bucket["tp1_hits"] += 1
    if t.get("hit_tp2"):
        bucket["tp2_hits"] += 1
    if t.get("hit_tp3"):
        bucket["tp3_hits"] += 1
    if t.get("hit_sl"):
        bucket["sl_hits"] += 1
