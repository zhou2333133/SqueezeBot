"""
市场阶段识别 — 纯规则判断，不依赖 LLM。

职责：
  根据 K线/成交量/OI 数据识别当前市场阶段。
  输出 state_key 用于 learning_memory 按场景聚合。

使用方式：
  state = classify(candle_data, oi_data, taker_data)
  → {"trend": "up", "volatility": "normal", "confidence": 0.72,
     "state_key": "trend_up|vol_normal|liq_normal|momentum_expanding"}

规则原则：
  - 所有判断基于明确指标阈值
  - confidence 综合多因子一致性
  - 不直接建议交易，只描述状态
"""
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def classify(candle_buf: list[dict] | None = None,
             oi_change_5m: float | None = None,
             taker_ratio: float | None = None,
             volume_ratio: float | None = None,
             atr_pct: float | None = None,
             ema20_deviation_pct: float | None = None,
             ) -> dict[str, Any]:
    """识别当前市场阶段。

    参数：
      candle_buf:    最近 N 根 K 线 [{o,h,l,c,q}]
      oi_change_5m:   5分钟 OI 变化率 (%)
      taker_ratio:    主动买占比 (0~1)
      volume_ratio:   当前量 / 均量
      atr_pct:        ATR / 价格 (%)
      ema20_deviation_pct: 价格偏离 EMA20 (%)

    返回：
      trend, volatility, liquidity, fake_breakout_risk,
      momentum, confidence, state_key
    """
    result = _default_state()

    if not candle_buf or len(candle_buf) < 10:
        return result  # 数据不足返回默认

    closes = [float(k.get("c", 0)) for k in candle_buf if k.get("c")]
    highs = [float(k.get("h", 0)) for k in candle_buf if k.get("h")]
    lows = [float(k.get("l", 0)) for k in candle_buf if k.get("l")]
    volumes = [float(k.get("q", 0)) for k in candle_buf if k.get("q")]

    if len(closes) < 10:
        return result

    # ── 趋势判断 ────────────────────────────────────────────────────────
    ema5 = _ema(closes, 5)
    ema10 = _ema(closes, 10)
    ema20 = _ema(closes, 20)

    # 斜率：线性回归
    slope = _linear_slope(closes[-10:])
    slope_5 = _linear_slope(closes[-5:]) if len(closes) >= 5 else slope

    # 趋势方向
    trend_signals = 0
    if ema5 > ema10 > ema20 and slope > 0:
        result["trend"] = "up"
        trend_signals = sum([
            slope > 0.0001,
            ema5 > ema10,
            closes[-1] > ema5,
            closes[-1] > closes[-5],
        ])
    elif ema5 < ema10 < ema20 and slope < 0:
        result["trend"] = "down"
        trend_signals = sum([
            slope < -0.0001,
            ema5 < ema10,
            closes[-1] < ema5,
            closes[-1] < closes[-5],
        ])
    else:
        result["trend"] = "range"

    # ── 波动率 ──────────────────────────────────────────────────────────
    if atr_pct is not None and atr_pct > 0:
        # 用 ATR% 判断波动率
        atr_median = _median(atr_pct) if isinstance(atr_pct, list) else atr_pct
        if atr_pct > 2.0:
            result["volatility"] = "high"
        elif atr_pct < 0.5:
            result["volatility"] = "low"
        else:
            result["volatility"] = "normal"
    else:
        # 用 K 线振幅估算
        ranges = [h - l for h, l in zip(highs[-10:], lows[-10:])]
        avg_range = sum(ranges) / len(ranges) if ranges else 0
        median_price = closes[-1] or 1
        range_pct = avg_range / median_price * 100
        if range_pct > 2.0:
            result["volatility"] = "high"
        elif range_pct < 0.5:
            result["volatility"] = "low"
        else:
            result["volatility"] = "normal"

    # ── 流动性 ──────────────────────────────────────────────────────────
    if volume_ratio is not None:
        if volume_ratio < 0.5:
            result["liquidity"] = "low"
        elif volume_ratio > 3.0:
            result["liquidity"] = "surge"
        else:
            result["liquidity"] = "normal"

    # ── 假突破风险 ───────────────────────────────────────────────────────
    fake_signals = 0
    if len(closes) >= 5:
        # 前一根有较长影线 + 当前缩量
        for i in range(-5, -1):
            body = abs(closes[i] - (candle_buf[i].get("o", closes[i])))
            wick = highs[i] - lows[i]
            if wick > 0 and body / wick < 0.3:
                fake_signals += 1
    if oi_change_5m is not None and oi_change_5m < -2.0:
        fake_signals += 1  # OI 骤降 + 假突破
    if volume_ratio is not None and volume_ratio < 0.6 and result["trend"] != "range":
        fake_signals += 1  # 突破不放量
    result["fake_breakout_risk"] = fake_signals >= 2

    # ── 动量 ─────────────────────────────────────────────────────────────
    if len(closes) >= 5:
        recent_roi = (closes[-1] - closes[-5]) / closes[-5] * 100
        vol_trend = _linear_slope(volumes[-10:]) if len(volumes) >= 10 else 0
        if abs(slope_5) > 0.0002 and vol_trend > 0:
            result["momentum"] = "expanding"
        elif abs(recent_roi) < 0.3 and vol_trend < 0:
            result["momentum"] = "fading"
        else:
            result["momentum"] = "neutral"

    # ── Confidence ──────────────────────────────────────────────────────
    signals = 0
    total = 0
    if ema5 and ema10 and ema20:
        total += 1
        if (result["trend"] == "up" and ema5 > ema10 > ema20) or \
           (result["trend"] == "down" and ema5 < ema10 < ema20) or \
           (result["trend"] == "range" and abs(slope) < 0.0001):
            signals += 1
    if atr_pct:
        total += 1
        if (result["volatility"] == "high" and atr_pct > 2.0) or \
           (result["volatility"] == "normal" and 0.5 <= atr_pct <= 2.0) or \
           (result["volatility"] == "low" and atr_pct < 0.5):
            signals += 1
    total += 1
    if result["momentum"] != "neutral":
        signals += 1
    result["confidence"] = round(signals / max(total, 1), 2)

    # ── State Key ────────────────────────────────────────────────────────
    result["state_key"] = _build_state_key(result)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════════════════════

def _default_state() -> dict:
    return {
        "trend": "unknown",
        "volatility": "unknown",
        "liquidity": "normal",
        "fake_breakout_risk": False,
        "momentum": "neutral",
        "confidence": 0.0,
        "state_key": "trend_unknown|vol_unknown|liq_normal|fb_false",
    }


def _build_state_key(s: dict) -> str:
    """生成统一 state_key。所有字段固定出现，保证同状态→同 key。"""
    parts = [
        f"trend_{s.get('trend', 'unknown')}",
        f"vol_{s.get('volatility', 'unknown')}",
        f"liq_{s.get('liquidity', 'normal')}",
        f"fb_{'true' if s.get('fake_breakout_risk') else 'false'}",
    ]
    if s.get("momentum") and s["momentum"] != "neutral":
        parts.append(f"mom_{s['momentum']}")
    return "|".join(parts)


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def _linear_slope(values: list[float]) -> float:
    """最小二乘法斜率。"""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(values) / n
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
    den = sum((x - x_mean) ** 2 for x in xs)
    return num / den if den != 0 else 0.0


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2
