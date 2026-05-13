"""
交易画像 — 从 entry_context 中提取标准化学习特征。
"""
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def extract_features(entry_context: dict) -> dict:
    """从 entry_context 中提取结构化特征向量。"""
    ec = entry_context or {}
    direction = str(ec.get("direction", "")).upper()
    sig = str(ec.get("signal_label", ""))
    state = str(ec.get("market_state", ""))
    trigger = abs(float(ec.get("trigger_pct", 0) or 0))
    ret20 = float(ec.get("pre_entry_directional_30m_pct", 0) or 0)
    taker = float(ec.get("yaobi_taker_buy_ratio_5m", ec.get("taker_ratio", 0)) or 0)
    vol_ratio = float(ec.get("yaobi_volume_ratio", 0) or 0)
    oi_5m = float(ec.get("yaobi_oi_change_5m_pct", 0) or 0)
    oi_15m = float(ec.get("yaobi_oi_change_15m_pct", 0) or 0)
    funding = float(ec.get("yaobi_funding_rate_pct", ec.get("funding_rate", 0)) or 0)
    atr = float(ec.get("atr_pct", 0) or 0)
    ema_dev = float(ec.get("directional_ema20_deviation_pct", 0) or 0)

    # 信号类型
    signal_type = "breakout"
    if "轧空" in sig or "猎杀" in sig or "squeeze" in sig.lower():
        signal_type = "squeeze"
    elif "回踩" in sig or "continuation" in sig.lower():
        signal_type = "continuation"
    elif "突破" in sig or "breakout" in sig.lower():
        signal_type = "breakout"

    # 趋势状态
    trend_state = "unknown"
    if "TREND_EARLY" in state:
        trend_state = "trend_early"
    elif "TREND_MID" in state:
        trend_state = "trend_mid"
    elif "TREND_LATE" in state:
        trend_state = "trend_late"
    elif "RANGE" in state.upper():
        trend_state = "range_chop"
    elif "WATERFALL" in state.upper():
        trend_state = "waterfall"

    # K 线模式识别
    patterns = _detect_patterns(ec, trigger, vol_ratio, oi_5m, taker)

    # 候选来源
    sources = ec.get("candidate_sources", [])
    candidate_source = str(sources[0]) if isinstance(sources, list) and sources else "unknown"

    return {
        "signal_type": signal_type,
        "trend_state": trend_state,
        "trigger_pct": round(trigger, 2),
        "taker_ratio": round(taker, 3),
        "volume_ratio": round(vol_ratio, 2),
        "oi_change_5m_pct": round(oi_5m, 2),
        "oi_change_15m_pct": round(oi_15m, 2),
        "funding_rate": round(funding, 4),
        "atr_pct": round(atr, 2),
        "ema20_deviation_pct": round(ema_dev, 2),
        "retrace_30m_pct": round(ret20, 2),
        "patterns": patterns,
        "raw_entry_atr": round(atr, 2),
        # 新增字段
        "candidate_source": candidate_source,
        "spread_pct": round(float(ec.get("spread_pct", 0) or 0), 4),
        "entry_reason": sig,
        "state_key": str(ec.get("_state_key", "")),
    }


def _detect_patterns(ec: dict, trigger: float, vol_ratio: float,
                     oi_5m: float, taker: float) -> list[str]:
    """识别入场时的 K 线模式。"""
    patterns = []
    pre_5m = float(ec.get("pre_entry_5m_pct", 0) or 0)
    pre_15m = float(ec.get("pre_entry_15m_pct", 0) or 0)
    ema_dev = float(ec.get("directional_ema20_deviation_pct", 0) or 0)

    if trigger > 2.0 and vol_ratio > 2.0:
        patterns.append("volume_spike_breakout")
    if trigger > 2.0 and vol_ratio < 1.0:
        patterns.append("low_volume_breakout")
    if pre_15m > 3.0 and trigger < 0.5:
        patterns.append("premoved_then_stall")
    if ema_dev > 3.0:
        patterns.append("extreme_ema_deviation")
    if taker > 0.75 and vol_ratio > 1.5:
        patterns.append("strong_buy_pressure")
    if taker < 0.45:
        patterns.append("weak_buy_pressure")
    if oi_5m > 5.0:
        patterns.append("oi_surge_5m")
    if oi_5m < -3.0:
        patterns.append("oi_drop_5m")
    if abs(pre_5m) < 0.3 and trigger > 1.0:
        patterns.append("sudden_spike")
    return patterns


def classify_kline(setup: dict) -> str:
    """K线形态分类，用于 learning_memory 的模式匹配。"""
    trigger = setup.get("trigger_pct", 0)
    vol = setup.get("volume_ratio", 0)
    taker = setup.get("taker_ratio", 0.5)
    patterns = setup.get("patterns", [])

    if "volume_spike_breakout" in patterns:
        return "放量突破"
    if "low_volume_breakout" in patterns:
        return "缩量突破"
    if "oi_surge_5m" in patterns and taker > 0.6:
        return "放量上涨"
    if "oi_drop_5m" in patterns:
        return "OI骤降反弹"
    if "sudden_spike" in patterns:
        return "突然拉升"
    if trigger > 1.5 and vol > 1.2:
        return "正常突破"
    if trigger < 0.8:
        return "小幅启动"
    return "普通波动"
