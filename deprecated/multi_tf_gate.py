"""
多周期 Gate（交易结构审核官）。

定位：
  signal → strategy_tag → single_coin_judge → ★ multi_tf_gate → risk → executor

职责：
  对已出现的候选交易进行 4H → 1H → 15m 三层结构审核。
  判断这笔交易是否允许继续进入执行器。
  不是信号源，不扫币，不下单，不改仓位，不改杠杆，不改 SL/TP。

架构约束：
  - PAPER 和 LIVE 共用完全相同的多周期判断逻辑
  - execution_backend 不能作为判断条件
  - Gate 只能踩刹车，不能踩油门
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ── 类型定义 ──────────────────────────────────────────────────────────────────
GateAction = Literal["ALLOW", "WAIT", "REJECT"]
GateBias = Literal["BULLISH", "BEARISH", "NEUTRAL", "UNKNOWN"]
GateSupport = Literal["SUPPORT", "AGAINST", "NEUTRAL", "UNKNOWN"]


@dataclass
class GateResult:
    action: GateAction = "WAIT"
    confidence: float = 0.0
    four_h_bias: GateBias = "UNKNOWN"
    four_h_support: GateSupport = "UNKNOWN"
    one_h_structure: str = "UNKNOWN"
    one_h_support: GateSupport = "UNKNOWN"
    fifteen_m_entry_quality: str = "UNKNOWN"
    fifteen_m_support: GateSupport = "UNKNOWN"
    support: float | None = None
    resistance: float | None = None
    distance_to_support_pct: float | None = None
    distance_to_resistance_pct: float | None = None
    is_equilibrium_zone: bool = False
    is_overheated: bool = False
    is_chasing: bool = False
    is_near_resistance: bool = False
    is_near_support: bool = False
    block_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None


# ── 1. build_tf_context ──────────────────────────────────────────────────────
def build_tf_context(
    symbol: str,
    side: str,
    signal_label: str,
    market_state: str,
    entry_context: dict | None = None,
    kline_buffer: list | None = None,
    market_hub_data: dict | None = None,
    cfg: dict | None = None,
) -> dict:
    """构造多周期上下文。无原生 4H/1H K 线时从 yaobi metadata + 1m buffer 推导。"""
    ctx = entry_context or {}
    price = ctx.get("last_kline_close") or 0.0

    # ── 从 yaobi metadata 提取价格变化 ────────────────────────────────────
    price_change_1h = _g(ctx, "yaobi_price_change_1h")
    price_change_4h = _g(ctx, "yaobi_price_change_4h")
    price_change_24h = _g(ctx, "yaobi_price_change_24h")

    # ── 从 1m kline_buffer 计算结构 ───────────────────────────────────────
    buf = kline_buffer or []
    closes = [k["c"] for k in buf if isinstance(k, dict) and k.get("c")] if buf else []

    # 4H 偏倚（从 price_change + market_stage 推导）
    market_stage = str(_g(ctx, "yaobi_market_stage", "neutral"))
    four_h_bias = _derive_four_h_bias(market_stage, price_change_4h, side)

    # 1H 结构（从价格变化 + taker 推导）
    one_h_structure = _derive_one_h_structure(price_change_1h, price_change_24h,
                                                _g(ctx, "yaobi_taker_buy_ratio_5m"), side)

    # 15m 入场质量
    fifteen_m_entry = _derive_fifteen_m_entry(ctx, closes, side)

    # 支撑阻力
    support, resistance = calc_support_resistance(closes, cfg or {})

    # 距离百分比
    dist_support = calc_distance_pct(price, support) if support and price > 0 else None
    dist_resistance = calc_distance_pct(price, resistance) if resistance and price > 0 else None

    return {
        "symbol": symbol,
        "side": side,
        "signal_label": signal_label,
        "market_state": market_state,
        "price": price,
        "four_h_bias": four_h_bias,
        "four_h_market_stage": market_stage,
        "four_h_price_change_pct": price_change_4h,
        "one_h_structure": one_h_structure,
        "one_h_price_change_pct": price_change_1h,
        "fifteen_m_entry_quality": fifteen_m_entry["quality"],
        "fifteen_m_overheated": fifteen_m_entry["overheated"],
        "fifteen_m_chasing": fifteen_m_entry["chasing"],
        "fifteen_m_price_change_pct": _g(ctx, "yaobi_price_change_15m") or _g(ctx, "trigger_pct"),
        "support": support,
        "resistance": resistance,
        "distance_to_support_pct": dist_support,
        "distance_to_resistance_pct": dist_resistance,
        "market_hub": {
            "taker_ratio": _g(market_hub_data, "taker_ratio"),
            "funding": _g(market_hub_data, "funding"),
            "premium": _g(market_hub_data, "premium"),
        },
        "entry_context": ctx,
    }


def _derive_four_h_bias(market_stage: str, price_change_4h: float | None, side: str) -> str:
    """从市场阶段和 4H 涨跌幅推导 4H 偏倚。"""
    if not price_change_4h:
        return "UNKNOWN"
    # 市场阶段映射
    stage_to_bias = {
        "accumulation_before_oi": "BULLISH",
        "real_breakout": "BULLISH",
        "bull_trap": "BEARISH",
        "mm_control": "NEUTRAL",
        "distribution": "BEARISH",
        "dead": "BEARISH",
        "neutral": "NEUTRAL",
    }
    bias = stage_to_bias.get(market_stage, "NEUTRAL")
    # 价格变化修正：4H 涨 > 5% = BULLISH，跌 > 5% = BEARISH
    if price_change_4h > 5.0:
        bias = "BULLISH"
    elif price_change_4h < -5.0:
        bias = "BEARISH"
    return bias


def _derive_one_h_structure(
    price_change_1h: float | None,
    price_change_24h: float | None,
    taker_ratio: float | None,
    side: str,
) -> str:
    """从 1H 价格变化 + taker 推导结构。"""
    if price_change_1h is None:
        return "UNKNOWN"
    if price_change_1h > 2.0:
        return "UPTREND"
    if price_change_1h < -2.0:
        return "DOWNTREND"
    if abs(price_change_1h) < 0.5:
        if taker_ratio and abs(taker_ratio - 0.5) < 0.05:
            return "EQUILIBRIUM"
        return "RANGE"
    if price_change_1h > 0.5:
        return "MILD_UPTREND"
    if price_change_1h < -0.5:
        return "MILD_DOWNTREND"
    return "RANGE"


def _derive_fifteen_m_entry(ctx: dict, closes: list, side: str) -> dict:
    """从 15m 价格行为判断入场质量。"""
    trigger_pct = abs(_g(ctx, "trigger_pct", 0.0))
    overheated = trigger_pct > 3.0  # 15m 涨幅 > 3% 算过热
    chasing = trigger_pct > 1.5 if side == "LONG" else trigger_pct > 1.5
    if overheated:
        quality = "OVERHEATED"
    elif chasing:
        quality = "CHASING"
    elif trigger_pct < 0.3:
        quality = "TOO_EARLY"
    else:
        quality = "OK"
    return {"quality": quality, "overheated": overheated, "chasing": chasing}


# ── 8. calc_support_resistance ───────────────────────────────────────────────
def calc_support_resistance(closes: list[float], cfg: dict) -> tuple[float | None, float | None]:
    """从最近 close 价格计算基础支撑阻力（无 K 线时返回 None）。"""
    if len(closes) < 20:
        return None, None
    recent = closes[-20:]
    avg = sum(recent) / len(recent)
    support = min(recent) - (max(recent) - min(recent)) * 0.1
    resistance = max(recent) + (max(recent) - min(recent)) * 0.1
    return support, resistance


# ── 9. calc_distance_pct ─────────────────────────────────────────────────────
def calc_distance_pct(price: float, level: float) -> float | None:
    """计算价格距某水平的百分比。"""
    if price <= 0 or level <= 0:
        return None
    return abs(price - level) / price * 100


# ── 2. evaluate_4h_direction ─────────────────────────────────────────────────
def evaluate_4h_direction(context: dict, cfg: dict) -> GateSupport:
    """判断 4H 方向是否支持当前 side。"""
    bias = context.get("four_h_bias", "UNKNOWN")
    side = context.get("side", "")
    required = _g(cfg, "MULTI_TF_4H_REQUIRED", True)
    allow_neutral = _g(cfg, "MULTI_TF_4H_ALLOW_NEUTRAL", True)
    block_against = _g(cfg, "MULTI_TF_4H_BLOCK_AGAINST", True)

    if bias == "UNKNOWN" and not required:
        return "NEUTRAL"

    if side == "LONG":
        if bias == "BULLISH":
            return "SUPPORT"
        if bias == "BEARISH":
            return "AGAINST" if block_against else "NEUTRAL"
    else:  # SHORT
        if bias == "BEARISH":
            return "SUPPORT"
        if bias == "BULLISH":
            return "AGAINST" if block_against else "NEUTRAL"

    if bias == "NEUTRAL":
        return "NEUTRAL" if allow_neutral else "AGAINST"
    return "NEUTRAL"


# ── 3. evaluate_1h_structure ─────────────────────────────────────────────────
def evaluate_1h_structure(context: dict, cfg: dict) -> tuple[GateSupport, str]:
    """判断 1H 结构是否支持交易。"""
    structure = context.get("one_h_structure", "UNKNOWN")
    side = context.get("side", "")
    dist_res = context.get("distance_to_resistance_pct")
    dist_sup = context.get("distance_to_support_pct")
    near_pct = _g(cfg, "MULTI_TF_NEAR_LEVEL_PCT", 0.5)
    min_room_long = _g(cfg, "MULTI_TF_MIN_ROOM_TO_RESISTANCE_PCT_LONG", 0.8)
    min_room_short = _g(cfg, "MULTI_TF_MIN_ROOM_TO_SUPPORT_PCT_SHORT", 0.8)

    warnings_list = []

    # LONG 检查
    if side == "LONG":
        if structure in ("UPTREND", "MILD_UPTREND"):
            if dist_res is not None and dist_res < near_pct:
                warnings_list.append(f"距离阻力仅 {dist_res:.2f}%")
                return "NEUTRAL", "NEAR_RESISTANCE"
            if dist_res is not None and dist_res < min_room_long:
                warnings_list.append(f"上行空间不足 {dist_res:.2f}%")
                return "NEUTRAL", "LOW_UP_ROOM"
            return "SUPPORT", structure
        if structure == "DOWNTREND":
            return "AGAINST", "DOWNTREND_AGAINST_LONG"
        if structure == "EQUILIBRIUM":
            return "NEUTRAL", "EQUILIBRIUM"
        return "NEUTRAL", structure

    # SHORT 检查
    else:
        if structure in ("DOWNTREND", "MILD_DOWNTREND"):
            if dist_sup is not None and dist_sup < near_pct:
                warnings_list.append(f"距离支撑仅 {dist_sup:.2f}%")
                return "NEUTRAL", "NEAR_SUPPORT"
            if dist_sup is not None and dist_sup < min_room_short:
                warnings_list.append(f"下行空间不足 {dist_sup:.2f}%")
                return "NEUTRAL", "LOW_DOWN_ROOM"
            return "SUPPORT", structure
        if structure == "UPTREND":
            return "AGAINST", "UPTREND_AGAINST_SHORT"
        if structure == "EQUILIBRIUM":
            return "NEUTRAL", "EQUILIBRIUM"
        return "NEUTRAL", structure


# ── 4. evaluate_15m_entry ────────────────────────────────────────────────────
def evaluate_15m_entry(context: dict, cfg: dict) -> tuple[GateSupport, str]:
    """判断 15m 入场质量是否合适。"""
    quality = context.get("fifteen_m_entry_quality", "UNKNOWN")
    max_chase = _g(cfg, "MULTI_TF_15M_MAX_CHASE_PCT", 3.0)
    block_overheated = _g(cfg, "MULTI_TF_15M_BLOCK_OVERHEATED", True)

    if quality == "OVERHEATED":
        if block_overheated:
            return "AGAINST", "OVERHEATED"
        return "NEUTRAL", "OVERHEATED_SKIP"
    if quality == "CHASING":
        trigger_pct = abs(_g(context.get("entry_context", {}), "trigger_pct", 0.0))
        if trigger_pct > max_chase:
            return "AGAINST", f"CHASING_{trigger_pct:.1f}%"
        return "NEUTRAL", "CHASING"
    if quality == "TOO_EARLY":
        return "NEUTRAL", "TOO_EARLY"
    return "SUPPORT", "OK"


# ── 5. evaluate_multi_tf_gate ────────────────────────────────────────────────
def evaluate_multi_tf_gate(context: dict, cfg: dict | None = None) -> GateResult:
    """综合 4H / 1H / 15m 评估，输出 GateResult。"""
    if cfg is None:
        cfg = {}
    side = context.get("side", "")
    dist_sup = context.get("distance_to_support_pct")
    dist_res = context.get("distance_to_resistance_pct")
    near_pct = _g(cfg, "MULTI_TF_NEAR_LEVEL_PCT", 0.5)
    is_near_res = dist_res is not None and dist_res < near_pct
    is_near_sup = dist_sup is not None and dist_sup < near_pct

    # 各层评估
    four_h = evaluate_4h_direction(context, cfg)
    one_h_support, one_h_detail = evaluate_1h_structure(context, cfg)
    fifteen_m, fifteen_m_detail = evaluate_15m_entry(context, cfg)

    # 综合决策
    warnings = []
    block_reason = ""

    # 4H 明确反对 → REJECT
    if four_h == "AGAINST":
        block_reason = "4H_AGAINST"
        action: GateAction = "REJECT"
    # 1H 明确反对 → REJECT
    elif one_h_support == "AGAINST":
        block_reason = f"1H_{one_h_detail}"
        action = "REJECT"
    # 4H 和 1H 都反对 → REJECT
    elif four_h == "NEUTRAL" and one_h_support == "AGAINST":
        block_reason = f"4H_NEUTRAL_1H_{one_h_detail}"
        action = "REJECT"
    # 15m 过热 → WAIT
    elif fifteen_m == "AGAINST":
        block_reason = f"15M_{fifteen_m_detail}"
        action = "WAIT"
    # 距离阻力太近做多 → WAIT
    elif side == "LONG" and is_near_res:
        block_reason = "NEAR_RESISTANCE_LONG"
        action = "WAIT"
    # 距离支撑太近做空 → WAIT
    elif side == "SHORT" and is_near_sup:
        block_reason = "NEAR_SUPPORT_SHORT"
        action = "WAIT"
    # 1H 均衡区 → WAIT
    elif one_h_detail == "EQUILIBRIUM":
        block_reason = "1H_EQUILIBRIUM"
        action = "WAIT"
    # 全部支持或中性 → ALLOW
    elif four_h in ("SUPPORT", "NEUTRAL") and one_h_support in ("SUPPORT", "NEUTRAL"):
        action = "ALLOW"
    else:
        action = "WAIT"

    # 补充 warnings
    if is_near_res:
        warnings.append(f"距阻力 {dist_res:.2f}%")
    if is_near_sup:
        warnings.append(f"距支撑 {dist_sup:.2f}%")
    if fifteen_m == "AGAINST":
        warnings.append(f"15m: {fifteen_m_detail}")

    # 计算综合置信度
    confidence = 0.5
    if four_h == "SUPPORT":
        confidence += 0.2
    if one_h_support == "SUPPORT":
        confidence += 0.15
    if fifteen_m == "SUPPORT":
        confidence += 0.15
    if four_h == "AGAINST":
        confidence -= 0.3
    if one_h_support == "AGAINST":
        confidence -= 0.2
    confidence = max(0.0, min(1.0, confidence))

    # 均衡区标记
    is_eq = one_h_detail == "EQUILIBRIUM"
    is_oh = fifteen_m_detail == "OVERHEATED"
    is_ch = "CHASING" in fifteen_m_detail

    return GateResult(
        action=action,
        confidence=round(confidence, 2),
        four_h_bias=context.get("four_h_bias", "UNKNOWN"),
        four_h_support=four_h,
        one_h_structure=one_h_detail,
        one_h_support=one_h_support,
        fifteen_m_entry_quality=fifteen_m_detail,
        fifteen_m_support=fifteen_m,
        support=context.get("support"),
        resistance=context.get("resistance"),
        distance_to_support_pct=dist_sup,
        distance_to_resistance_pct=dist_res,
        is_equilibrium_zone=is_eq,
        is_overheated=is_oh,
        is_chasing=is_ch,
        is_near_resistance=is_near_res,
        is_near_support=is_near_sup,
        block_reason=block_reason,
        warnings=warnings,
    )


# ── 6. default_gate_result ──────────────────────────────────────────────────
def default_gate_result(reason: str = "") -> GateResult:
    """数据不足、计算失败时返回的安全默认结果。"""
    default_action = "WAIT"
    return GateResult(
        action=default_action,
        confidence=0.0,
        block_reason=reason or "insufficient_data",
    )


# ── 7. normalize_gate_result ────────────────────────────────────────────────
def normalize_gate_result(result: GateResult) -> GateResult:
    """补齐缺失字段、限制非法值。"""
    if result.action not in ("ALLOW", "WAIT", "REJECT"):
        result.action = "WAIT"
    if result.four_h_bias not in ("BULLISH", "BEARISH", "NEUTRAL", "UNKNOWN"):
        result.four_h_bias = "UNKNOWN"
    if result.four_h_support not in ("SUPPORT", "AGAINST", "NEUTRAL", "UNKNOWN"):
        result.four_h_support = "NEUTRAL"
    if result.one_h_support not in ("SUPPORT", "AGAINST", "NEUTRAL", "UNKNOWN"):
        result.one_h_support = "NEUTRAL"
    if result.fifteen_m_support not in ("SUPPORT", "AGAINST", "NEUTRAL", "UNKNOWN"):
        result.fifteen_m_support = "NEUTRAL"
    result.confidence = max(0.0, min(1.0, result.confidence))
    result.warnings = [str(w) for w in (result.warnings or [])]
    return result


# ── 10. should_block_by_gate ─────────────────────────────────────────────────
def should_block_by_gate(result: GateResult, cfg: dict) -> tuple[bool, str]:
    """根据 GateResult 和配置决定是否阻止交易。不允许根据 PAPER/LIVE 判断。"""
    enabled = _g(cfg, "MULTI_TF_GATE_ENABLED", False)
    can_veto = _g(cfg, "MULTI_TF_GATE_CAN_VETO", False)
    if not enabled or not can_veto:
        return False, ""
    if result.action in ("WAIT", "REJECT"):
        return True, f"MULTI_TF_{result.action}: {result.block_reason}"
    return False, ""


# ── 工具函数 ─────────────────────────────────────────────────────────────────
def _g(d: dict | None, key: str, default=None):
    if not d:
        return default
    return d.get(key, default)


def gate_to_signal_fields(result: GateResult) -> dict:
    """将 GateResult 转为 signal/trade 记录字段。"""
    return {
        "multi_tf_gate_enabled": True,
        "multi_tf_gate_action": result.action,
        "multi_tf_gate_confidence": result.confidence,
        "multi_tf_4h_bias": result.four_h_bias,
        "multi_tf_4h_support": result.four_h_support,
        "multi_tf_1h_structure": result.one_h_structure,
        "multi_tf_1h_support": result.one_h_support,
        "multi_tf_15m_entry_quality": result.fifteen_m_entry_quality,
        "multi_tf_15m_support": result.fifteen_m_support,
        "multi_tf_support": result.support,
        "multi_tf_resistance": result.resistance,
        "multi_tf_distance_to_support_pct": result.distance_to_support_pct,
        "multi_tf_distance_to_resistance_pct": result.distance_to_resistance_pct,
        "multi_tf_is_equilibrium_zone": result.is_equilibrium_zone,
        "multi_tf_is_overheated": result.is_overheated,
        "multi_tf_is_chasing": result.is_chasing,
        "multi_tf_blocked": False,
        "multi_tf_block_reason": "",
    }
