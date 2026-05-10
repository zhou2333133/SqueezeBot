"""
策略分类引擎：根据信号标签、K线、OI、Taker 等数据，给信号打上策略标签。

五类策略：
  启动型, OI爆发, 静默建仓, 突破前夜, 早期启动

用法：
  tag = classify(cfg, signal_label, direction, market_state,
                 oi_change_pct, atr_pct, price_change_15m,
                 price_change_1h, price_change_24h, taker_ratio,
                 funding_rate, vol_ratio)
"""
import logging

logger = logging.getLogger(__name__)

# ── 默认参数（会被 cfg 覆盖）────────────────────────────────────────────────
_DEFAULTS = {
    "STRATEGY_启动型_MIN_PRICE_CHANGE_15M": 0.3,
    "STRATEGY_启动型_MAX_PRICE_CHANGE_1H": 2.0,
    "STRATEGY_启动型_MIN_VOL_RATIO": 1.2,
    "STRATEGY_启动型_MIN_OI_CHANGE_15M": 0.5,
    "STRATEGY_启动型_MAX_OI_CHANGE_1H": 3.0,

    "STRATEGY_OI爆发_MIN_OI_CHANGE_15M": 2.0,
    "STRATEGY_OI爆发_MIN_OI_CHANGE_1H": 5.0,
    "STRATEGY_OI爆发_MIN_VOL_RATIO": 1.5,
    "STRATEGY_OI爆发_MAX_PRICE_CHANGE_15M": 5.0,

    "STRATEGY_静默建仓_MAX_PRICE_CHANGE_15M": 1.0,
    "STRATEGY_静默建仓_MAX_PRICE_CHANGE_1H": 2.0,
    "STRATEGY_静默建仓_MIN_OI_CHANGE_15M": 0.3,
    "STRATEGY_静默建仓_MIN_OI_CHANGE_1H": 1.0,
    "STRATEGY_静默建仓_MIN_VOL_RATIO": 1.0,
    "STRATEGY_静默建仓_MAX_FUNDING_ABS": 0.01,

    "STRATEGY_突破前夜_MIN_OI_CHANGE_15M": 0.5,
    "STRATEGY_突破前夜_MIN_VOL_RATIO": 1.2,
    "STRATEGY_突破前夜_MAX_RETRACE_PCT": 0.5,

    "STRATEGY_早期启动_MIN_PRICE_CHANGE_15M": 0.5,
    "STRATEGY_早期启动_MIN_PRICE_CHANGE_1H": 1.0,
    "STRATEGY_早期启动_MAX_PRICE_CHANGE_24H": 15.0,
    "STRATEGY_早期启动_MIN_VOL_RATIO": 1.3,
    "STRATEGY_早期启动_MIN_OI_CHANGE_15M": 0.5,
}


def classify(
    cfg: dict,
    signal_label: str = "",
    direction: str = "",
    market_state: str = "",
    oi_change_pct: float | None = None,
    atr_pct: float | None = None,
    price_change_15m: float | None = None,
    price_change_1h: float | None = None,
    price_change_24h: float | None = None,
    taker_ratio: float | None = None,
    funding_rate: float | None = None,
    vol_ratio: float | None = None,
) -> str:
    """返回策略标签：启动型 / OI爆发 / 静默建仓 / 突破前夜 / 早期启动 / ""（无法判断）"""
    try:
        return _classify_impl(
            cfg, signal_label, direction, market_state,
            oi_change_pct, atr_pct, price_change_15m,
            price_change_1h, price_change_24h, taker_ratio,
            funding_rate, vol_ratio,
        )
    except Exception as e:
        logger.debug("策略分类异常: %s", e)
        return ""


def _g(cfg: dict, key: str) -> float:
    """从 cfg 取参数，无则用默认值。"""
    val = cfg.get(key)
    if val is not None:
        return float(val)
    return float(_DEFAULTS.get(key, 0.0))


def _classify_impl(
    cfg: dict,
    signal_label: str,
    direction: str,
    market_state: str,
    oi_change_pct: float | None,
    atr_pct: float | None,
    price_change_15m: float | None,
    price_change_1h: float | None,
    price_change_24h: float | None,
    taker_ratio: float | None,
    funding_rate: float | None,
    vol_ratio: float | None,
) -> str:
    """内部实现，无 try/except 方便测试。"""
    # ── 辅助：有值且 >= 阈值
    def _has(v: float | None, limit: float) -> bool:
        return v is not None and v >= limit

    def _within(v: float | None, lo: float, hi: float) -> bool:
        return v is not None and lo <= v <= hi

    def _le(v: float | None, limit: float) -> bool:
        return v is not None and v <= limit

    # 优先按 signal_label 预分类
    is_breakout = "动能突破" in signal_label
    is_squeeze = "轧空" in signal_label or "猎杀" in signal_label
    is_continuation = "顺势回踩" in signal_label

    # ── 1. 检查 OI 爆发 ──────────────────────────────────────────────────
    if _has(oi_change_pct, _g(cfg, "STRATEGY_OI爆发_MIN_OI_CHANGE_1H")):
        if _has(vol_ratio, _g(cfg, "STRATEGY_OI爆发_MIN_VOL_RATIO")):
            if _le(price_change_15m, _g(cfg, "STRATEGY_OI爆发_MAX_PRICE_CHANGE_15M")):
                return "OI爆发"

    # ── 2. 检查 启动型 ──────────────────────────────────────────────────
    if _has(price_change_15m, _g(cfg, "STRATEGY_启动型_MIN_PRICE_CHANGE_15M")):
        if _le(price_change_1h, _g(cfg, "STRATEGY_启动型_MAX_PRICE_CHANGE_1H")):
            if _has(vol_ratio, _g(cfg, "STRATEGY_启动型_MIN_VOL_RATIO")):
                if _has(oi_change_pct, _g(cfg, "STRATEGY_启动型_MIN_OI_CHANGE_15M")):
                    if _le(oi_change_pct, _g(cfg, "STRATEGY_启动型_MAX_OI_CHANGE_1H")):
                        return "启动型"

    # ── 3. 检查 早期启动 ────────────────────────────────────────────────
    if _has(price_change_15m, _g(cfg, "STRATEGY_早期启动_MIN_PRICE_CHANGE_15M")):
        if _has(price_change_1h, _g(cfg, "STRATEGY_早期启动_MIN_PRICE_CHANGE_1H")):
            if _le(price_change_24h, _g(cfg, "STRATEGY_早期启动_MAX_PRICE_CHANGE_24H")):
                if _has(vol_ratio, _g(cfg, "STRATEGY_早期启动_MIN_VOL_RATIO")):
                    if _has(oi_change_pct, _g(cfg, "STRATEGY_早期启动_MIN_OI_CHANGE_15M")):
                        return "早期启动"

    # ── 4. 检查 静默建仓 ────────────────────────────────────────────────
    if _le(price_change_15m, _g(cfg, "STRATEGY_静默建仓_MAX_PRICE_CHANGE_15M")):
        if _le(price_change_1h, _g(cfg, "STRATEGY_静默建仓_MAX_PRICE_CHANGE_1H")):
            if _has(oi_change_pct, _g(cfg, "STRATEGY_静默建仓_MIN_OI_CHANGE_15M")):
                if _has(vol_ratio, _g(cfg, "STRATEGY_静默建仓_MIN_VOL_RATIO")):
                    if funding_rate is None or abs(funding_rate) <= _g(cfg, "STRATEGY_静默建仓_MAX_FUNDING_ABS"):
                        return "静默建仓"

    # ── 5. 检查 突破前夜 ────────────────────────────────────────────────
    if is_breakout and _has(oi_change_pct, _g(cfg, "STRATEGY_突破前夜_MIN_OI_CHANGE_15M")):
        if _has(vol_ratio, _g(cfg, "STRATEGY_突破前夜_MIN_VOL_RATIO")):
            return "突破前夜"

    # ── 兜底：走信号标签映射 ────────────────────────────────────────────
    if is_breakout:
        return "突破前夜"
    if is_squeeze:
        return "启动型"
    if is_continuation:
        return "早期启动"

    return ""
