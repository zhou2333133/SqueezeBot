"""
Shadow Trade Tracker — 追踪被策略拦截信号的假设结果。

职责：
  - 被 BLOCK 的信号不真实交易，但创建 shadow trade 跟踪后续价格
  - shadow trade 按虚拟 SL/TP 规则关闭
  - 为 Evolver 提供 shadow outcome 判断拦截是否正确

架构原则：
  - PAPER 和 LIVE 共用同一套 shadow tracker
  - shadow trade 不允许进入 open_positions
  - shadow trade 不允许调用 trader.py
  - execution_backend 只能作为记录字段
"""
import hashlib
import logging
import os
import time
from datetime import datetime, timezone

from config import DATA_DIR, config_manager
from persistence import append_jsonl, read_jsonl

logger = logging.getLogger(__name__)

SHADOW_TRADES_FILE = os.path.join(DATA_DIR, "shadow_trades.jsonl")


def create_shadow_trade(blocked_signal: dict) -> dict | None:
    """从被拦截信号创建 shadow trade。"""
    try:
        if not config_manager.settings.get("SHADOW_TRACKER_ENABLED", True):
            return None
        shadow = {
            "shadow_id": _make_shadow_id(blocked_signal),
            "symbol": str(blocked_signal.get("symbol", "")),
            "side": str(blocked_signal.get("side", "LONG")).upper(),
            "strategy_tag": str(blocked_signal.get("strategy_tag", "UNKNOWN")),
            "blocked_reason": str(blocked_signal.get("blocked_reason", "UNKNOWN")),
            "policy_version": str(blocked_signal.get("policy_version", "")),
            "config_hash": str(blocked_signal.get("config_hash", "")),
            "entry_time": time.time(),
            "entry_ref": float(blocked_signal.get("entry_ref", blocked_signal.get("entry_price", 0)) or 0),
            "planned_stop_loss": float(blocked_signal.get("planned_stop_loss",
                                    blocked_signal.get("stop_loss", blocked_signal.get("sl_price", 0))) or 0),
            "planned_take_profit_1": float(blocked_signal.get("planned_take_profit_1",
                                           blocked_signal.get("take_profit_1", blocked_signal.get("tp1_price", 0))) or 0),
            "planned_take_profit_2": float(blocked_signal.get("planned_take_profit_2",
                                           blocked_signal.get("take_profit_2", blocked_signal.get("tp2_price", 0))) or 0),
            "status": "OPEN",
            "current_price": 0.0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "close_time": None,
            "close_price": None,
            "close_reason": None,
            "hypothetical_pnl_pct": None,
            "hypothetical_result": "PENDING",
            "strategy_weight": float(blocked_signal.get("strategy_weight", 1.0) or 1.0),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        append_jsonl(SHADOW_TRADES_FILE, shadow)
        return shadow
    except Exception as e:
        logger.warning("创建 shadow trade 失败: %s", e)
        return None


def _make_shadow_id(signal: dict) -> str:
    raw = f"{signal.get('symbol', '')}:{signal.get('side', '')}:{time.time()}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def load_shadow_trades(status: str = "OPEN") -> list[dict]:
    """读取 shadow trades。"""
    all_trades = read_jsonl(SHADOW_TRADES_FILE)
    if status:
        return [t for t in all_trades if t.get("status") == status]
    return all_trades


def update_shadow_trades(market_prices: dict[str, float]) -> list[dict]:
    """用最新行情价格更新所有 OPEN shadow trades。返回已关闭的 trades。"""
    closed: list[dict] = []
    trades = load_shadow_trades(status="OPEN")
    if not trades:
        return closed

    max_hold = float(config_manager.settings.get("SHADOW_MAX_HOLD_MINUTES", 240) or 240)
    now = time.time()
    updated = []
    remaining_trades = []

    for shadow in trades:
        symbol = shadow.get("symbol", "")
        price = market_prices.get(symbol, 0.0)
        if price <= 0:
            remaining_trades.append(shadow)
            continue

        shadow["current_price"] = price
        entry = shadow.get("entry_ref", 0)
        side = shadow.get("side", "LONG")

        # MFE/MAE
        if entry > 0:
            if side == "LONG":
                mfe = (price - entry) / entry * 100
                mae = (entry - price) / entry * 100
            else:
                mfe = (entry - price) / entry * 100
                mae = (price - entry) / entry * 100
            shadow["mfe_pct"] = round(max(shadow.get("mfe_pct", 0.0), mfe), 4)
            shadow["mae_pct"] = round(min(shadow.get("mae_pct", 0.0), mae), 4)

        # 检查 TP/SL 触发
        sl = shadow.get("planned_stop_loss", 0)
        tp1 = shadow.get("planned_take_profit_1", 0)
        reason = _check_shadow_close(price, side, sl, tp1)

        # 超时检查
        elapsed_min = (now - shadow.get("entry_time", now)) / 60
        if not reason and elapsed_min > max_hold:
            reason = "SHADOW_TIMEOUT"

        if reason:
            shadow["status"] = "CLOSED"
            shadow["close_time"] = now
            shadow["close_price"] = price
            shadow["close_reason"] = reason
            # 计算假设 PnL
            if entry > 0:
                if side == "LONG":
                    shadow["hypothetical_pnl_pct"] = round((price - entry) / entry * 100, 4)
                else:
                    shadow["hypothetical_pnl_pct"] = round((entry - price) / entry * 100, 4)
            shadow["hypothetical_result"] = "WIN" if (shadow.get("hypothetical_pnl_pct") or 0) > 0 else "LOSS"
            updated.append(shadow)
            closed.append(shadow)
        else:
            remaining_trades.append(shadow)

    # 持久化更新
    _persist_shadow_trades(remaining_trades + _load_closed_trades() + updated)
    return closed


def _check_shadow_close(price: float, side: str, sl: float, tp1: float) -> str | None:
    """检查 TP/SL 是否触发。"""
    if sl <= 0 and tp1 <= 0:
        return None
    if side == "LONG":
        if sl > 0 and price <= sl:
            return "SHADOW_SL"
        if tp1 > 0 and price >= tp1:
            return "SHADOW_TP1"
    else:
        if sl > 0 and price >= sl:
            return "SHADOW_SL"
        if tp1 > 0 and price <= tp1:
            return "SHADOW_TP1"
    return None


def _load_closed_trades() -> list[dict]:
    """读取已关闭的 shadow trades。"""
    return read_jsonl(SHADOW_TRADES_FILE)


def _persist_shadow_trades(trades: list[dict]) -> None:
    """重写 shadow_trades.jsonl。"""
    try:
        os.makedirs(os.path.dirname(SHADOW_TRADES_FILE), exist_ok=True)
        with open(SHADOW_TRADES_FILE, "w", encoding="utf-8") as f:
            for t in trades:
                f.write(__import__("json").dumps(t, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("写入 shadow_trades 失败: %s", e)


def compute_shadow_outcomes() -> dict[str, dict]:
    """统计被拦截信号的假设结果，按 strategy_tag 聚合。"""
    all_trades = read_jsonl(SHADOW_TRADES_FILE)
    closed = [t for t in all_trades if t.get("status") == "CLOSED"]
    by_tag: dict[str, dict] = {}

    for t in closed:
        tag = str(t.get("strategy_tag", "UNKNOWN"))
        if tag not in by_tag:
            by_tag[tag] = {"blocked_count": 0, "shadow_closed": 0,
                           "would_have_won": 0, "would_have_lost": 0,
                           "total_hypothetical_pnl": 0.0, "reasons": {}}
        s = by_tag[tag]
        s["shadow_closed"] += 1
        result = str(t.get("hypothetical_result", "UNKNOWN"))
        pnl = float(t.get("hypothetical_pnl_pct", 0) or 0)
        s["total_hypothetical_pnl"] += pnl
        if result == "WIN":
            s["would_have_won"] += 1
        else:
            s["would_have_lost"] += 1
        reason = str(t.get("blocked_reason", "UNKNOWN"))
        s["reasons"][reason] = s["reasons"].get(reason, 0) + 1

    # 添加 OPEN 计数
    open_trades = [t for t in all_trades if t.get("status") == "OPEN"]
    for t in open_trades:
        tag = str(t.get("strategy_tag", "UNKNOWN"))
        if tag not in by_tag:
            by_tag[tag] = {"blocked_count": 0, "shadow_closed": 0,
                           "would_have_won": 0, "would_have_lost": 0,
                           "total_hypothetical_pnl": 0.0, "reasons": {}}

    result = {}
    for tag, s in by_tag.items():
        closed_count = s["shadow_closed"]
        result[tag] = {
            "blocked_count": s["blocked_count"],
            "shadow_closed": closed_count,
            "would_have_won": s["would_have_won"],
            "would_have_lost": s["would_have_lost"],
            "shadow_win_rate": round(s["would_have_won"] / closed_count, 4) if closed_count > 0 else 0.0,
            "avg_hypothetical_pnl_pct": round(s["total_hypothetical_pnl"] / closed_count, 4) if closed_count > 0 else 0.0,
            "blocked_reasons": s["reasons"],
        }
    return result
