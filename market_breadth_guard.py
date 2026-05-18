"""
市场宽度守卫 — 判断全局市场状态，控制 rule_fallback 放行 LONG。

职责：
  - 检查 BTC 趋势、跌币占比、整体市场强弱
  - 输出 degrade_long: True/False
  - 被 bot_scalp._detect_signal_v3 和 yaobi_scanner._apply_opportunity_queue 调用

规则：
  BTC 5m 跌幅 > 1.5% → degrade
  BTC 15m 跌幅 > 2.5% → degrade
  监控币种中下跌占比 > 60% → degrade
  degrade 后，rule_fallback 的 LONG 降级为 OBSERVE
"""
import logging
import time

logger = logging.getLogger(__name__)

# 缓存上次结果，避免每次 tick 都重算
_cache: dict = {"verdict": None, "cached_at": 0.0, "ttl": 5.0}


def should_degrade_long(bot=None) -> dict:
    """判断当前是否应降级 LONG。

    参数：
      bot: BinanceScalpBot 实例（提供 kline_buffer 和 candidate_meta）

    返回：
      {"degrade": bool, "reason": str, "btc_drop_5m": float, "decline_ratio": float}
    """
    if not bot:
        return {"degrade": False, "reason": "", "btc_drop_5m": 0.0, "decline_ratio": 0.0}

    # 开关检查
    try:
        from config import config_manager
        if not config_manager.settings.get("MARKET_BREADTH_GUARD_ENABLED", True):
            return {"degrade": False, "reason": "guard_disabled", "btc_drop_5m": 0.0, "decline_ratio": 0.0}
    except Exception:
        pass

    now = time.time()
    if now - _cache.get("cached_at", 0) < _cache.get("ttl", 5):
        return _cache["verdict"]

    result = {"degrade": False, "reason": "", "btc_drop_5m": 0.0, "decline_ratio": 0.0}

    # 读取可配置阈值
    try:
        from config import config_manager
        _c = config_manager.settings
        _btc_5m = float(_c.get("MARKET_BREADTH_BTC_5M_DROP_PCT", 1.5) or 1.5)
        _btc_15m = float(_c.get("MARKET_BREADTH_BTC_15M_DROP_PCT", 2.5) or 2.5)
        _decline_ratio = float(_c.get("MARKET_BREADTH_DECLINE_RATIO", 0.6) or 0.6)
    except Exception:
        _btc_5m, _btc_15m, _decline_ratio = 1.5, 2.5, 0.6

    # 1. BTC 短周期跌幅
    btc_buf = bot.kline_buffer.get("BTCUSDT", []) if hasattr(bot, "kline_buffer") else []
    if len(btc_buf) >= 5:
        btc_close_5m_ago = _get_close(btc_buf, -5)
        btc_close_now = _get_close(btc_buf, -1)
        if btc_close_5m_ago and btc_close_5m_ago > 0:
            btc_drop = (btc_close_now - btc_close_5m_ago) / btc_close_5m_ago * 100
            result["btc_drop_5m"] = round(btc_drop, 2)
            if btc_drop < -_btc_5m:
                result["degrade"] = True
                result["reason"] = f"BTC 5m 跌 {btc_drop:.1f}%"
                _set_cache(result)
                return result

    # 2. 更长周期 (15m)
    if len(btc_buf) >= 15:
        btc_close_15m_ago = _get_close(btc_buf, -15)
        btc_close_now = _get_close(btc_buf, -1)
        if btc_close_15m_ago and btc_close_15m_ago > 0:
            btc_drop_15 = (btc_close_now - btc_close_15m_ago) / btc_close_15m_ago * 100
            if btc_drop_15 < -_btc_15m:
                result["degrade"] = True
                result["reason"] = f"BTC 15m 跌 {btc_drop_15:.1f}%"
                _set_cache(result)
                return result

    # 3. 候选币下跌占比
    try:
        symbols = bot.candidate_symbols if hasattr(bot, "candidate_symbols") else []
        if len(symbols) >= 10:
            declining = 0
            checked = 0
            for sym in symbols:
                buf = bot.kline_buffer.get(sym, []) if hasattr(bot, "kline_buffer") else []
                if len(buf) >= 5:
                    c5 = _get_close(buf, -5)
                    c1 = _get_close(buf, -1)
                    if c5 and c5 > 0 and c1:
                        checked += 1
                        if c1 < c5:
                            declining += 1
            if checked >= 10:
                ratio = declining / checked
                result["decline_ratio"] = round(ratio, 2)
                if ratio > _decline_ratio:
                    result["degrade"] = True
                    result["reason"] = f"候选币下跌占比 {ratio:.0%}"
                    _set_cache(result)
                    return result
    except Exception:
        pass

    _set_cache(result)
    return result


def _get_close(buf: list, idx: int) -> float:
    try:
        return float(buf[idx].get("c", 0) or buf[idx].get("close", 0))
    except (IndexError, TypeError, ValueError):
        return 0.0


def _set_cache(verdict: dict) -> None:
    _cache["verdict"] = verdict
    _cache["cached_at"] = time.time()
