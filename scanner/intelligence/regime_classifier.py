"""Market-stage and playbook classification for yaobi candidates."""
from __future__ import annotations

from typing import Any

from .case_similarity import apply_case_similarity
from .holder_structure import apply_holder_structure


STAGE_LABELS = {
    "accumulation_before_oi": "OI前吸筹",
    "real_breakout": "真实启动",
    "bull_trap": "诱多收割",
    "mm_control": "做市控盘",
    "distribution": "出货派发",
    "dead": "死亡/高危",
    "neutral": "观察",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def _append_unique(items: list, value: str) -> None:
    if value and value not in items:
        items.append(value)


def classify_market_regime(c: Any) -> dict:
    holder = apply_holder_structure(c)
    matches = apply_case_similarity(c, limit=3)

    chip = int(holder["chip_score"])
    control = int(holder["control_score"])
    distribution = int(holder["distribution_score"])
    price24 = _as_float(getattr(c, "price_change_24h", 0.0))
    price1h = _as_float(getattr(c, "price_change_1h", 0.0))
    oi24 = _as_float(getattr(c, "oi_change_24h_pct", 0.0))
    oi15 = _as_float(getattr(c, "oi_change_15m_pct", 0.0))
    funding = _as_float(getattr(c, "funding_rate_pct", 0.0))
    taker = _as_float(getattr(c, "taker_buy_ratio_5m", 0.5), 0.5)
    buy_ratio = _as_float(getattr(c, "okx_buy_ratio", 0.0))
    square = int(_as_float(getattr(c, "square_mentions", 0.0)))
    news = len(getattr(c, "surf_news_titles", []) or [])
    liquidity = _as_float(getattr(c, "liquidity", 0.0))
    risk_level = int(_as_float(getattr(c, "okx_risk_level", 0.0)))
    tags = {str(t) for t in (getattr(c, "okx_token_tags", []) or [])}
    whale_long = _as_float(getattr(c, "whale_long_ratio", 0.5), 0.5)
    long_account = _as_float(getattr(c, "long_account_pct", 50.0), 50.0)
    top_trader_long = _as_float(getattr(c, "top_trader_long_pct", 50.0), 50.0)

    social_heat = min(100, square * 4 + news * 10)
    social_warming = 2 <= square <= 18 or news > 0
    social_overheated = square >= 25 or social_heat >= 80
    spot_buy_strong = buy_ratio >= 0.62
    spot_buy_weak = 0 < buy_ratio <= 0.42
    oi_started = oi24 >= 40 or oi15 >= 3
    oi_quiet = oi24 < 30 and abs(oi15) < 2
    funding_hot_long = funding >= 0.08
    funding_hot_short = funding <= -0.10
    crowded_long = long_account >= 65 or top_trader_long >= 65
    crowded_short = _as_float(getattr(c, "retail_short_pct", 50.0), 50.0) >= 68
    thin_liquidity = bool(liquidity and liquidity < 80_000)
    danger_tags = tags.intersection({"honeypot", "lowLiquidity", "devHoldingStatusSellAll"})

    reasons: list[str] = []
    risks: list[str] = []
    reasons.extend(holder.get("reasons", [])[:3])
    risks.extend(holder.get("risks", [])[:3])

    hard_risk = (
        risk_level >= 4
        or bool(danger_tags)
        or bool(getattr(c, "surf_ai_hard_block", False))
        or str(getattr(c, "surf_news_sentiment", "") or "") == "negative"
    )
    dead_risk = hard_risk or (price24 <= -35 and distribution >= 40) or (thin_liquidity and distribution >= 70)

    risk_score = distribution
    if funding_hot_long or funding_hot_short:
        risk_score += 12
    if social_overheated:
        risk_score += 8
    if price24 <= -25:
        risk_score += 12
    if spot_buy_weak:
        risk_score += 8
    if oi_started and (funding_hot_long or funding_hot_short) and (spot_buy_weak or taker < 0.52):
        risk_score += 24
    if hard_risk:
        risk_score += 25
    risk_score = _clamp_score(risk_score)

    stage = "neutral"
    playbook = "observe"
    trade_permission = "OBSERVE"

    if dead_risk:
        stage = "dead"
        playbook = "block_high_risk"
        trade_permission = "BLOCK"
        _append_unique(risks, "高危链上/新闻/风险标签")
    elif (oi24 < -15 and price24 > 10) or (spot_buy_weak and price24 > 8 and distribution >= 35):
        stage = "distribution"
        playbook = "avoid_distribution"
        trade_permission = "BLOCK" if risk_score >= 60 or (oi24 < -15 and price24 > 10) else "OBSERVE"
        _append_unique(risks, "价格上行但OI/链上结构像派发")
    elif (
        oi_started
        and price24 < 18
        and (funding_hot_long or crowded_long)
        and (spot_buy_weak or taker < 0.52)
    ):
        stage = "bull_trap"
        playbook = "avoid_chasing_oi"
        trade_permission = "BLOCK" if risk_score >= 55 or funding_hot_long else "OBSERVE"
        _append_unique(risks, "OI启动但资金费率/买盘结构不支持追多")
    elif (
        chip >= 45
        and oi_quiet
        and spot_buy_strong
        and social_warming
        and not social_overheated
        and funding < 0.05
        and risk_score < 60
    ):
        stage = "accumulation_before_oi"
        playbook = "ambush_watch"
        trade_permission = "AMBUSH_WATCH"
        _append_unique(reasons, "OI未启动但链上吸筹和社交开始升温")
    elif control >= 50 and (thin_liquidity or social_overheated or chip >= 70):
        stage = "mm_control"
        playbook = "thin_book_control"
        trade_permission = "OBSERVE"
        _append_unique(risks, "筹码集中/薄流动性，疑似做市控盘")
    elif (
        oi_started
        and price24 > 8
        and taker >= 0.56
        and (buy_ratio == 0 or buy_ratio >= 0.50)
        and not funding_hot_long
        and risk_score < 65
    ):
        stage = "real_breakout"
        playbook = "oi_confirmation"
        trade_permission = "WATCH_CONFIRMATION"
        _append_unique(reasons, "OI作为确认信号，仍需1m执行层触发")
    elif price24 <= -30:
        stage = "dead"
        playbook = "avoid_breakdown"
        trade_permission = "BLOCK"
        _append_unique(risks, "价格已明显破位")

    if matches:
        top = matches[0]
        _append_unique(reasons, f"历史相似:{top['symbol']} {top['similarity']}%")

    return {
        "market_stage": stage,
        "playbook_type": playbook,
        "trade_permission": trade_permission,
        "risk_score": risk_score,
        "reasons": reasons[:6],
        "risks": risks[:6],
        "social_heat_score": social_heat,
        "case_matches": matches,
        "stage_label": STAGE_LABELS.get(stage, stage),
        "price_1h": price1h,
        "whale_long": whale_long,
        "crowded_short": crowded_short,
    }


def apply_market_intelligence(c: Any) -> dict:
    result = classify_market_regime(c)
    c.market_stage = result["market_stage"]
    c.playbook_type = result["playbook_type"]
    c.trade_permission = result["trade_permission"]
    c.risk_score = result["risk_score"]
    c.social_heat_score = result["social_heat_score"]
    c.intelligence_reasons = result["reasons"]
    c.intelligence_risks = result["risks"]

    stage_label = result["stage_label"]
    _append_unique(c.signals, f"剧本:{stage_label}")
    if c.trade_permission == "AMBUSH_WATCH":
        c.alpha_status = "AMBUSH_WATCH"
    elif c.trade_permission == "BLOCK":
        c.alpha_status = "BLOCK"
    elif c.trade_permission == "WATCH_CONFIRMATION":
        c.alpha_status = "WATCH_CONFIRMATION"
    return result
