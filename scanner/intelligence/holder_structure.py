"""On-chain holder structure scoring.

The first implementation intentionally uses only fields already collected by
OKX/Dex/Binance adapters. It produces compact scores that downstream regime
classification can combine with exchange and social structure.
"""
from __future__ import annotations

from typing import Any


_DANGER_TAGS = {
    "honeypot",
    "lowLiquidity",
    "devHoldingStatusSellAll",
    "blacklist",
    "mintable",
    "proxyContract",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def analyze_holder_structure(c: Any) -> dict:
    """Return chip/control/distribution scores plus short reasons."""
    top10 = _as_float(getattr(c, "okx_top10_hold_pct", 0.0))
    dev_hold = _as_float(getattr(c, "okx_dev_hold_pct", 0.0))
    lp_burned = _as_float(getattr(c, "okx_lp_burned_pct", 0.0))
    smart = _as_int(getattr(c, "okx_smart_money_holders", 0))
    large_trade = _as_float(getattr(c, "okx_large_trade_pct", 0.0))
    buy_ratio = _as_float(getattr(c, "okx_buy_ratio", 0.0))
    liquidity = _as_float(getattr(c, "liquidity", 0.0))
    risk_level = _as_int(getattr(c, "okx_risk_level", 0))
    tags = {str(t) for t in (getattr(c, "okx_token_tags", []) or [])}

    chip = 0.0
    control = 0.0
    distribution = 0.0
    reasons: list[str] = []
    risks: list[str] = []

    if top10 >= 80:
        chip += 30
        control += 28
        distribution += 18
        reasons.append(f"Top10持仓{top10:.0f}%")
        risks.append("Top10筹码过度集中")
    elif top10 >= 65:
        chip += 22
        control += 18
        distribution += 8
        reasons.append(f"Top10持仓{top10:.0f}%")
    elif top10 >= 50:
        chip += 14
        control += 8
        reasons.append(f"Top10持仓{top10:.0f}%")

    if dev_hold >= 15:
        control += 18
        distribution += 18
        risks.append(f"Dev持仓{dev_hold:.1f}%")
    elif dev_hold >= 8:
        control += 10
        distribution += 8
        risks.append(f"Dev持仓{dev_hold:.1f}%")

    if smart >= 3:
        chip += 18
        reasons.append(f"聪明钱持仓{smart}")
    elif smart >= 1:
        chip += 10
        reasons.append(f"聪明钱持仓{smart}")

    if large_trade >= 0.30:
        chip += 18
        control += 12
        reasons.append(f"链上大单{large_trade * 100:.0f}%")
    elif large_trade >= 0.15:
        chip += 10
        control += 6
        reasons.append(f"链上大单{large_trade * 100:.0f}%")

    if buy_ratio >= 0.70:
        chip += 14
        reasons.append(f"链上买盘{buy_ratio * 100:.0f}%")
    elif buy_ratio >= 0.62:
        chip += 8
        reasons.append(f"链上买盘{buy_ratio * 100:.0f}%")
    elif 0 < buy_ratio <= 0.35:
        chip -= 8
        distribution += 18
        risks.append(f"链上买盘偏弱{buy_ratio * 100:.0f}%")

    if liquidity and liquidity < 50_000:
        control += 14
        distribution += 12
        risks.append("链上流动性偏薄")
    elif liquidity and liquidity < 150_000:
        control += 6

    if lp_burned and lp_burned < 20:
        control += 8
        distribution += 6
        risks.append(f"LP销毁低{lp_burned:.0f}%")

    danger = sorted(tags.intersection(_DANGER_TAGS))
    if risk_level >= 4:
        distribution += 30
        risks.append(f"OKX风险等级{risk_level}")
    elif risk_level == 3:
        distribution += 14
        risks.append("OKX中高风险")
    if danger:
        distribution += 24
        risks.append("风险标签:" + "/".join(danger[:3]))

    tx_accel = _as_float(getattr(c, "txs_5m_accel", 0.0))
    if tx_accel >= 2.0:
        chip += 6
        reasons.append(f"链上Tx加速{tx_accel:.1f}x")
    elif tx_accel >= 1.5:
        chip += 3

    early_layout = (
        chip >= 45
        and distribution < 60
        and _as_float(getattr(c, "oi_change_24h_pct", 0.0)) < 30
        and _as_float(getattr(c, "funding_rate_pct", 0.0)) < 0.05
    )

    return {
        "chip_score": _clamp_score(chip),
        "control_score": _clamp_score(control),
        "distribution_score": _clamp_score(distribution),
        "early_wallet_layout": bool(early_layout),
        "reasons": reasons[:5],
        "risks": risks[:5],
    }


def apply_holder_structure(c: Any) -> dict:
    result = analyze_holder_structure(c)
    c.chip_score = result["chip_score"]
    c.control_score = result["control_score"]
    c.distribution_score = result["distribution_score"]
    c.early_wallet_layout = result["early_wallet_layout"]
    return result
