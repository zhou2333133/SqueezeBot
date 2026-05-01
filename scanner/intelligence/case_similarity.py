"""Small deterministic behavior-pattern library for yaobi regimes."""
from __future__ import annotations

from typing import Any


CASE_LIBRARY: list[dict] = [
    {
        "symbol": "ACCUM_PROTO",
        "result": "sustained_trend",
        "lesson": "筹码集中但链上买盘强，OI未启动时更适合埋伏观察，等OI确认再执行。",
        "features": {
            "chip_concentration": 0.65,
            "smart_money": 0.55,
            "spot_buy": 0.78,
            "large_trade": 0.35,
            "oi_rise": 0.10,
            "oi_drop": 0.00,
            "funding_hot": 0.00,
            "social_heat": 0.35,
            "sell_pressure": 0.05,
            "liquidity_thin": 0.15,
            "risk_tags": 0.00,
            "price_pump": 0.08,
            "price_dump": 0.00,
        },
    },
    {
        "symbol": "BULL_TRAP_PROTO",
        "result": "fake_breakout",
        "lesson": "OI暴涨但现货买盘弱、资金费率过热时，OI应视为追多拥挤确认而不是入口。",
        "features": {
            "chip_concentration": 0.35,
            "smart_money": 0.10,
            "spot_buy": 0.35,
            "large_trade": 0.15,
            "oi_rise": 0.95,
            "oi_drop": 0.00,
            "funding_hot": 0.90,
            "social_heat": 0.75,
            "sell_pressure": 0.70,
            "liquidity_thin": 0.25,
            "risk_tags": 0.10,
            "price_pump": 0.15,
            "price_dump": 0.00,
        },
    },
    {
        "symbol": "MM_CONTROL_PROTO",
        "result": "pump_and_dump",
        "lesson": "高度集中、薄盘口、社交同步升温更像做市控盘，只适合短观察，不适合隔夜持有。",
        "features": {
            "chip_concentration": 0.90,
            "smart_money": 0.20,
            "spot_buy": 0.55,
            "large_trade": 0.45,
            "oi_rise": 0.45,
            "oi_drop": 0.00,
            "funding_hot": 0.25,
            "social_heat": 0.85,
            "sell_pressure": 0.25,
            "liquidity_thin": 0.85,
            "risk_tags": 0.20,
            "price_pump": 0.35,
            "price_dump": 0.00,
        },
    },
    {
        "symbol": "DISTRIBUTION_PROTO",
        "result": "pump_and_dump",
        "lesson": "价格上行但OI下降、链上卖压增强时，更像派发或空头撤退尾声，避免追涨。",
        "features": {
            "chip_concentration": 0.65,
            "smart_money": 0.10,
            "spot_buy": 0.25,
            "large_trade": 0.25,
            "oi_rise": 0.00,
            "oi_drop": 0.70,
            "funding_hot": 0.40,
            "social_heat": 0.60,
            "sell_pressure": 0.80,
            "liquidity_thin": 0.40,
            "risk_tags": 0.25,
            "price_pump": 0.45,
            "price_dump": 0.00,
        },
    },
    {
        "symbol": "DEAD_PROTO",
        "result": "death_spiral",
        "lesson": "高风险标签、低流动性、价格破位时，不应让任何OI或社交热度重新打开许可。",
        "features": {
            "chip_concentration": 0.45,
            "smart_money": 0.00,
            "spot_buy": 0.20,
            "large_trade": 0.05,
            "oi_rise": 0.10,
            "oi_drop": 0.50,
            "funding_hot": 0.20,
            "social_heat": 0.20,
            "sell_pressure": 0.85,
            "liquidity_thin": 0.90,
            "risk_tags": 0.90,
            "price_pump": 0.00,
            "price_dump": 0.70,
        },
    },
]


_WEIGHTS = {
    "chip_concentration": 1.1,
    "smart_money": 0.9,
    "spot_buy": 1.2,
    "large_trade": 0.9,
    "oi_rise": 1.2,
    "oi_drop": 1.1,
    "funding_hot": 1.1,
    "social_heat": 0.8,
    "sell_pressure": 1.2,
    "liquidity_thin": 0.8,
    "risk_tags": 1.2,
    "price_pump": 0.8,
    "price_dump": 0.9,
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def feature_vector(c: Any) -> dict[str, float]:
    top10 = _as_float(getattr(c, "okx_top10_hold_pct", 0.0))
    smart = _as_float(getattr(c, "okx_smart_money_holders", 0.0))
    buy_ratio = _as_float(getattr(c, "okx_buy_ratio", 0.0))
    large = _as_float(getattr(c, "okx_large_trade_pct", 0.0))
    oi24 = _as_float(getattr(c, "oi_change_24h_pct", 0.0))
    funding = _as_float(getattr(c, "funding_rate_pct", 0.0))
    square = _as_float(getattr(c, "square_mentions", 0.0))
    news = len(getattr(c, "surf_news_titles", []) or [])
    price24 = _as_float(getattr(c, "price_change_24h", 0.0))
    liquidity = _as_float(getattr(c, "liquidity", 0.0))
    risk_level = _as_float(getattr(c, "okx_risk_level", 0.0))
    tags = getattr(c, "okx_token_tags", []) or []

    return {
        "chip_concentration": _clamp01(top10 / 100),
        "smart_money": _clamp01(smart / 5),
        "spot_buy": _clamp01(buy_ratio if buy_ratio > 0 else 0.5),
        "large_trade": _clamp01(large / 0.50),
        "oi_rise": _clamp01(max(0.0, oi24) / 160),
        "oi_drop": _clamp01(abs(min(0.0, oi24)) / 60),
        "funding_hot": _clamp01(abs(funding) / 0.15),
        "social_heat": _clamp01((square + news * 3) / 30),
        "sell_pressure": _clamp01(1.0 - buy_ratio) if buy_ratio > 0 else 0.25,
        "liquidity_thin": _clamp01((150_000 - liquidity) / 150_000) if liquidity > 0 else 0.20,
        "risk_tags": _clamp01(risk_level / 5 + len(tags) * 0.12),
        "price_pump": _clamp01(max(0.0, price24) / 80),
        "price_dump": _clamp01(abs(min(0.0, price24)) / 60),
    }


def _similarity(vec: dict[str, float], proto: dict[str, float]) -> int:
    total_w = 0.0
    distance = 0.0
    for key, weight in _WEIGHTS.items():
        total_w += weight
        distance += abs(vec.get(key, 0.0) - proto.get(key, 0.0)) * weight
    if total_w <= 0:
        return 0
    return max(0, min(100, int(round((1 - distance / total_w) * 100))))


def top_case_similarities(c: Any, limit: int = 3) -> list[dict]:
    vec = feature_vector(c)
    rows = []
    for case in CASE_LIBRARY:
        rows.append({
            "symbol": case["symbol"],
            "result": case["result"],
            "similarity": _similarity(vec, case["features"]),
            "lesson": case["lesson"],
        })
    rows.sort(key=lambda x: x["similarity"], reverse=True)
    return rows[:max(1, int(limit))]


def apply_case_similarity(c: Any, limit: int = 3) -> list[dict]:
    matches = top_case_similarities(c, limit=limit)
    c.case_similarity = {
        "matches": matches,
        "top_result": matches[0]["result"] if matches else "",
        "top_similarity": matches[0]["similarity"] if matches else 0,
    }
    return matches
