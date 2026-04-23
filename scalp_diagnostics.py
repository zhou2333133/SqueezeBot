from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Callable


DIAGNOSIS_RULE_VERSION = "scalp_diagnosis_v1"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value: Any, digits: int = 4) -> float:
    return round(_as_float(value), digits)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _kline_ratio(row: dict) -> float | None:
    total = _as_float(row.get("q"))
    if total <= 0:
        return None
    return _as_float(row.get("Q")) / total


def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    seed = _mean(values[:period])
    alpha = 2 / (period + 1)
    ema = seed
    for value in values[period:]:
        ema = value * alpha + ema * (1 - alpha)
    return ema


def _calc_atr_pct(buf: list[dict], n: int = 14) -> float:
    if len(buf) < n + 1:
        return 0.0
    ranges: list[float] = []
    rows = buf[-(n + 1):]
    for i in range(1, len(rows)):
        prev_close = _as_float(rows[i - 1].get("c"))
        high = _as_float(rows[i].get("h"))
        low = _as_float(rows[i].get("l"))
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    last_close = _as_float(rows[-1].get("c"))
    return (sum(ranges) / len(ranges)) / last_close * 100 if last_close > 0 and ranges else 0.0


def _directional_move_pct(base: float, price: float, direction: str) -> float:
    if base <= 0 or price <= 0:
        return 0.0
    if direction == "SHORT":
        return (base - price) / base * 100
    return (price - base) / base * 100


def _lookback_move(buf: list[dict], direction: str, lookback: int, price: float) -> float:
    if len(buf) < lookback:
        return 0.0
    return _directional_move_pct(_as_float(buf[-lookback].get("c")), price, direction)


def _recent_pullback_pct(buf: list[dict], direction: str) -> float:
    window = buf[-8:] if len(buf) >= 8 else list(buf)
    if len(window) < 4:
        return 0.0
    if direction == "SHORT":
        lows = [_as_float(k.get("l"), _as_float(k.get("c"))) for k in window]
        pivot = min(range(len(lows)), key=lows.__getitem__)
        if pivot >= len(window) - 1 or lows[pivot] <= 0:
            return 0.0
        high_after = max(_as_float(k.get("h"), _as_float(k.get("c"))) for k in window[pivot + 1:])
        return max(0.0, (high_after - lows[pivot]) / lows[pivot] * 100)

    highs = [_as_float(k.get("h"), _as_float(k.get("c"))) for k in window]
    pivot = max(range(len(highs)), key=highs.__getitem__)
    if pivot >= len(window) - 1 or highs[pivot] <= 0:
        return 0.0
    low_after = min(_as_float(k.get("l"), _as_float(k.get("c"))) for k in window[pivot + 1:])
    return max(0.0, (highs[pivot] - low_after) / highs[pivot] * 100)


def build_entry_1m_profile(
    buf: list[dict],
    live: dict | None,
    direction: str,
    current_price: float,
    atr_pct: float | None = None,
) -> dict:
    """Build a compact rule-based 1m entry image for later trade review."""
    live = live or {}
    price = _as_float(current_price, _as_float(live.get("close")))
    closes = [_as_float(k.get("c")) for k in buf if _as_float(k.get("c")) > 0]
    closes_with_live = closes + ([price] if price > 0 else [])
    ema20 = _ema(closes_with_live, 20)
    ema20_dev = (price - ema20) / ema20 * 100 if price > 0 and ema20 > 0 else 0.0
    directional_ema20_dev = -ema20_dev if direction == "SHORT" else ema20_dev

    live_total = _as_float(live.get("total_vol"))
    live_ratio = live.get("taker_buy")
    live_taker = (_as_float(live_ratio) / live_total) if live_total > 0 else None
    ratios = [r for r in (_kline_ratio(k) for k in buf[-8:]) if r is not None]
    directional_ratios = [(1 - r) if direction == "SHORT" else r for r in ratios]
    if live_taker is not None:
        directional_ratios.append((1 - live_taker) if direction == "SHORT" else live_taker)

    recent_taker = _mean(directional_ratios[-2:])
    prior_taker = _mean(directional_ratios[-5:-2]) if len(directional_ratios) >= 5 else 0.0
    taker_delta = recent_taker - prior_taker if prior_taker > 0 else 0.0
    if taker_delta >= 0.05:
        taker_trend = "rising"
    elif taker_delta <= -0.05:
        taker_trend = "falling"
    else:
        taker_trend = "flat"

    avg_vol_5 = _mean([_as_float(k.get("q")) for k in buf[-5:]])
    vol_ratio = live_total / avg_vol_5 if live_total > 0 and avg_vol_5 > 0 else 0.0
    atr = _as_float(atr_pct, _calc_atr_pct(buf))
    pullback = _recent_pullback_pct(buf, direction)
    pullback_threshold = max(0.12, min(0.8, atr * 0.25 if atr else 0.12))

    return {
        "profile_version": DIAGNOSIS_RULE_VERSION,
        "pre_entry_3m_pct": round(_lookback_move(buf, direction, 3, price), 4),
        "pre_entry_5m_pct": round(_lookback_move(buf, direction, 5, price), 4),
        "pre_entry_15m_pct": round(_lookback_move(buf, direction, 15, price), 4),
        "ema20": round(ema20, 8) if ema20 else 0.0,
        "ema20_deviation_pct": round(ema20_dev, 4),
        "directional_ema20_deviation_pct": round(directional_ema20_dev, 4),
        "atr_pct": round(atr, 4),
        "recent_pullback_pct": round(pullback, 4),
        "breakout_after_pullback": bool(pullback >= pullback_threshold),
        "pullback_threshold_pct": round(pullback_threshold, 4),
        "current_kline_vol_ratio": round(vol_ratio, 4),
        "live_taker_ratio": round(live_taker, 4) if live_taker is not None else None,
        "directional_taker_live": round((1 - live_taker) if direction == "SHORT" and live_taker is not None else live_taker, 4)
        if live_taker is not None else None,
        "directional_taker_3m_avg": round(_mean(directional_ratios[-3:]), 4),
        "directional_taker_5m_avg": round(_mean(directional_ratios[-5:]), 4),
        "taker_trend_5m": taker_trend,
        "taker_trend_delta_5m": round(taker_delta, 4),
        "kline_buffer_len": len(buf),
    }


def _best_post_exit_favorable(trade: dict) -> float:
    values = [_as_float(trade.get("post_exit_mfe_pct"))]
    for horizon in (15, 30, 60, 120):
        values.append(_as_float(trade.get(f"post_exit_{horizon}m_favorable_pct")))
    return max(values) if values else 0.0


def _post_exit_30_or_best(trade: dict) -> float:
    return _as_float(trade.get("post_exit_30m_favorable_pct"), _best_post_exit_favorable(trade))


def diagnose_trade(trade: dict) -> dict:
    pnl = _as_float(trade.get("pnl_usdt"))
    mfe = _as_float(trade.get("mfe_pct"))
    mae = _as_float(trade.get("mae_pct"))
    net_r = _as_float(trade.get("net_r"))
    post_best = _best_post_exit_favorable(trade)
    post_30 = _post_exit_30_or_best(trade)
    post_60 = _as_float(trade.get("post_exit_60m_favorable_pct"), post_best)
    close_reason = str(trade.get("close_reason") or "")
    tp1_hit = bool(trade.get("tp1_hit"))
    hold_min = _as_float(trade.get("hold_minutes"))

    tags: list[str] = []
    reasons: list[str] = []

    sl_like = close_reason in {"SL", "SL_软保本", "SL_保本", "结构止损"}
    stopped_or_flat = sl_like or close_reason in {"趋势反转", "时间止损", "TP2超时"}

    if stopped_or_flat and post_best >= 1.5:
        tags.append("entry_bad")
        reasons.append(f"平仓后原方向继续最大 {post_best:.2f}%，方向有机会但点位/止损处理偏差")

    if pnl <= 0 and mfe < 0.5 and mae >= 0.8 and post_30 < 1.0:
        tags.append("direction_wrong")
        reasons.append(f"持仓内MFE仅 {mfe:.2f}%，MAE {mae:.2f}%，平仓后30m原方向无明显延续")

    if (tp1_hit or close_reason in {"TP3", "趋势反转", "SL_软保本", "SL_保本", "时间止损"}) and post_60 >= 3.0:
        tags.append("valid_runner_missed")
        reasons.append(f"平仓后60m内原方向仍有 {post_60:.2f}% 延续，可能卖飞runner")
    elif (tp1_hit or pnl >= 0) and post_best >= 2.0:
        tags.append("exit_too_early")
        reasons.append(f"平仓后原方向继续最大 {post_best:.2f}%，出场偏早")

    if sl_like and mfe >= 0.8:
        tags.append("stop_too_tight")
        reasons.append(f"持仓内曾有 {mfe:.2f}% 原方向利润，最终被止损/软保本扫出")

    if pnl <= 0 and mfe < 0.8 and mae < 0.8 and hold_min >= 20:
        tags.append("noise_chop")
        reasons.append("持仓时间较长但MFE/MAE都不大，属于低效率震荡样本")

    if not tags and pnl > 0 and post_best < 1.0:
        tags.append("good_trade")
        reasons.append("盈利出场后原方向未明显继续，出场质量可接受")

    if not tags:
        tags.append("needs_more_data")
        if trade.get("post_exit_status") == "watching":
            reasons.append("平仓后观察仍在进行，等待15/30/60/120m数据补齐")
        else:
            reasons.append("当前规则无法明确归因，需要更多同类样本")

    primary_priority = [
        "entry_bad",
        "direction_wrong",
        "valid_runner_missed",
        "exit_too_early",
        "stop_too_tight",
        "noise_chop",
        "good_trade",
        "needs_more_data",
    ]
    primary = next((tag for tag in primary_priority if tag in tags), tags[0])

    return {
        "trade_diagnosis": primary,
        "diagnosis_tags": list(dict.fromkeys(tags)),
        "diagnosis_reasons": reasons,
        "diagnosis_rule_version": DIAGNOSIS_RULE_VERSION,
        "diagnosis_metrics": {
            "mfe_pct": round(mfe, 4),
            "mae_pct": round(mae, 4),
            "post_exit_best_favorable_pct": round(post_best, 4),
            "post_exit_30m_favorable_pct": round(post_30, 4),
            "net_r": round(net_r, 4),
        },
    }


def apply_trade_diagnosis(trade: dict) -> dict:
    trade.update(diagnose_trade(trade))
    trade["diagnosis_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return trade


def _sample_trade(trade: dict) -> dict:
    ctx = trade.get("entry_context") or {}
    profile = ctx.get("entry_1m_profile") or {}
    return {
        "symbol": trade.get("symbol"),
        "direction": trade.get("direction"),
        "entry_time": trade.get("entry_time"),
        "exit_time": trade.get("exit_time"),
        "signal_label": trade.get("signal_label") or trade.get("signal"),
        "market_state": trade.get("market_state"),
        "close_reason": trade.get("close_reason"),
        "pnl_usdt": trade.get("pnl_usdt"),
        "net_r": trade.get("net_r"),
        "mfe_pct": trade.get("mfe_pct"),
        "mae_pct": trade.get("mae_pct"),
        "post_exit_best_favorable_pct": _round(_best_post_exit_favorable(trade)),
        "post_exit_30m_favorable_pct": trade.get("post_exit_30m_favorable_pct"),
        "post_exit_60m_favorable_pct": trade.get("post_exit_60m_favorable_pct"),
        "trade_diagnosis": trade.get("trade_diagnosis"),
        "diagnosis_tags": trade.get("diagnosis_tags", []),
        "diagnosis_reasons": trade.get("diagnosis_reasons", []),
        "entry_image": {
            "pre_entry_3m_pct": profile.get("pre_entry_3m_pct"),
            "pre_entry_5m_pct": profile.get("pre_entry_5m_pct"),
            "pre_entry_15m_pct": profile.get("pre_entry_15m_pct"),
            "ema20_deviation_pct": profile.get("ema20_deviation_pct"),
            "directional_ema20_deviation_pct": profile.get("directional_ema20_deviation_pct"),
            "atr_pct": profile.get("atr_pct") or ctx.get("atr_pct"),
            "breakout_after_pullback": profile.get("breakout_after_pullback"),
            "recent_pullback_pct": profile.get("recent_pullback_pct"),
            "taker_trend_5m": profile.get("taker_trend_5m"),
            "directional_taker_5m_avg": profile.get("directional_taker_5m_avg"),
            "current_taker_ratio": ctx.get("current_taker_ratio"),
        },
    }


def _top_samples(trades: list[dict], predicate: Callable[[dict], bool], limit: int = 10) -> list[dict]:
    rows = [t for t in trades if predicate(t)]
    rows.sort(
        key=lambda t: (
            _best_post_exit_favorable(t),
            abs(_as_float(t.get("pnl_usdt"))),
            _as_float(t.get("mfe_pct")),
        ),
        reverse=True,
    )
    return [_sample_trade(t) for t in rows[:limit]]


def build_learning_report(trades: list[dict]) -> dict:
    diagnosed = [apply_trade_diagnosis(t) for t in trades]
    sample_size = len(diagnosed)
    diagnosis_counts = Counter(t.get("trade_diagnosis", "needs_more_data") for t in diagnosed)
    tag_counts = Counter(tag for t in diagnosed for tag in t.get("diagnosis_tags", []))

    losing = [t for t in diagnosed if _as_float(t.get("pnl_usdt")) <= 0]
    loss_total_abs = sum(abs(_as_float(t.get("pnl_usdt"))) for t in losing)
    loss_by_diag: dict[str, dict] = {}
    for t in losing:
        diag = str(t.get("trade_diagnosis") or "needs_more_data")
        row = loss_by_diag.setdefault(diag, {"count": 0, "pnl_usdt": 0.0, "avg_mfe_pct": 0.0, "avg_mae_pct": 0.0})
        row["count"] += 1
        row["pnl_usdt"] += _as_float(t.get("pnl_usdt"))
        row["avg_mfe_pct"] += _as_float(t.get("mfe_pct"))
        row["avg_mae_pct"] += _as_float(t.get("mae_pct"))
    for row in loss_by_diag.values():
        count = row["count"] or 1
        row["pct_of_losing_trades"] = round(row["count"] / len(losing) * 100, 1) if losing else 0.0
        row["pct_of_loss_usdt"] = round(abs(row["pnl_usdt"]) / loss_total_abs * 100, 1) if loss_total_abs else 0.0
        row["pnl_usdt"] = round(row["pnl_usdt"], 4)
        row["avg_mfe_pct"] = round(row["avg_mfe_pct"] / count, 4)
        row["avg_mae_pct"] = round(row["avg_mae_pct"] / count, 4)

    suggestions: list[dict] = []
    if sample_size < 50:
        suggestions.append({
            "topic": "样本量",
            "condition": f"当前只有 {sample_size} 笔，低于50笔",
            "suggestion": "先继续跑到50-100笔，再根据entry_bad、direction_wrong、valid_runner_missed占比改参数。",
        })
    if losing:
        entry_bad_loss_pct = loss_by_diag.get("entry_bad", {}).get("pct_of_losing_trades", 0.0)
        direction_wrong_pct = loss_by_diag.get("direction_wrong", {}).get("pct_of_losing_trades", 0.0)
        if entry_bad_loss_pct >= 35:
            suggestions.append({
                "topic": "入场点位",
                "condition": f"亏损样本里 entry_bad 占 {entry_bad_loss_pct}%",
                "suggestion": "优先增加回踩确认/二次确认，或限制突破后立即追入；不要先动方向过滤。",
            })
        if direction_wrong_pct >= 30:
            suggestions.append({
                "topic": "方向过滤",
                "condition": f"亏损样本里 direction_wrong 占 {direction_wrong_pct}%",
                "suggestion": "提高突破Taker、ATR或大盘守卫门槛，按多空分别复核是否存在方向偏置失真。",
            })
    runner_tags = tag_counts.get("valid_runner_missed", 0) + tag_counts.get("exit_too_early", 0)
    if sample_size and runner_tags / sample_size >= 0.25:
        suggestions.append({
            "topic": "止盈/卖飞",
            "condition": f"valid_runner_missed/exit_too_early 合计 {runner_tags} 笔，占 {round(runner_tags / sample_size * 100, 1)}%",
            "suggestion": "保持TP1锁定利润，但放宽runner追踪或软保本触发；重点看这些样本的EMA20偏离和Taker趋势。",
        })
    if tag_counts.get("stop_too_tight", 0) >= max(3, sample_size * 0.2):
        suggestions.append({
            "topic": "止损空间",
            "condition": f"stop_too_tight 共 {tag_counts.get('stop_too_tight', 0)} 笔",
            "suggestion": "复核结构止损是否贴得太近；若entry_bad同时偏高，先解决点位，别单纯放大止损。",
        })
    if not suggestions:
        suggestions.append({
            "topic": "下一步",
            "condition": "当前诊断没有单一高占比问题",
            "suggestion": "继续收集样本，并按信号类型/多空/市场状态分组比较，不急着改默认参数。",
        })

    return {
        "规则版本": DIAGNOSIS_RULE_VERSION,
        "样本数": sample_size,
        "样本状态": "样本不足，先观察到50-100笔" if sample_size < 50 else "可开始按诊断占比调参",
        "诊断分布": dict(diagnosis_counts),
        "标签分布": dict(tag_counts),
        "亏损归因": dict(sorted(loss_by_diag.items(), key=lambda x: x[1]["pnl_usdt"])),
        "点位错样本": _top_samples(diagnosed, lambda t: "entry_bad" in t.get("diagnosis_tags", [])),
        "方向错样本": _top_samples(diagnosed, lambda t: "direction_wrong" in t.get("diagnosis_tags", [])),
        "卖飞样本": _top_samples(
            diagnosed,
            lambda t: bool({"valid_runner_missed", "exit_too_early"} & set(t.get("diagnosis_tags", []))),
        ),
        "下一轮参数建议": suggestions,
    }
