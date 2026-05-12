"""
单币 AI 判单模块（交易审核官 / AI Gate）。

定位：
  已有信号 → 策略标签 → 风控基础检查 → 单币 AI 判单 → 生成统一 order_plan → 执行器执行

职责：
  只判断一笔候选交易当前是否应该继续执行。
  不是信号源，不扫币，不下单，不改仓位，不改杠杆，不改止损止盈。

架构约束：
  - PAPER 和 LIVE 共用完全相同的 AI 判单逻辑
  - execution_backend 只作为记录字段，不参与判断
  - AI 只能踩刹车，不能踩油门
"""
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from config import DATA_DIR, config_manager

logger = logging.getLogger(__name__)

# ── 类型定义 ──────────────────────────────────────────────────────────────────
JudgeAction = Literal["ALLOW", "WAIT", "REJECT"]
JudgeDirection = Literal["LONG", "SHORT", "NONE"]
PositionSizeLevel = Literal["NONE", "SMALL", "HALF", "FULL"]


@dataclass
class JudgeResult:
    action: JudgeAction = "WAIT"
    direction: JudgeDirection = "NONE"
    confidence: float = 0.0
    entry_condition: str = ""
    invalidation_condition: str = ""
    support: float | None = None
    resistance: float | None = None
    suggested_stop_loss: float | None = None
    suggested_take_profit_1: float | None = None
    suggested_take_profit_2: float | None = None
    position_size_level: PositionSizeLevel = "NONE"
    risk_notes: list[str] = field(default_factory=list)
    reason: str = ""
    raw_response: dict[str, Any] | None = None
    used_cache: bool = False
    skipped: bool = False
    skip_reason: str = ""


# ── 缓存 ─────────────────────────────────────────────────────────────────────
_CACHE: dict[str, tuple[float, JudgeResult]] = {}
_CACHE_FILE = ""

_LAST_SCAN_TS: float = 0.0
_SCAN_COUNT: int = 0
_HOUR_COUNT: int = 0
_DAY_COUNT: int = 0
_LAST_HOUR_RESET: float = 0.0
_LAST_DAY_RESET: float = 0.0


def _cfg(key: str, default=None):
    return config_manager.settings.get(key, default)


# ── 1. build_judge_payload ───────────────────────────────────────────────────
def build_judge_payload(
    symbol: str,
    direction: str,
    strategy_tag: str,
    signal_label: str,
    entry_price: float,
    sl_price: float,
    tp1_price: float,
    tp2_price: float,
    market_snapshot: dict | None = None,
    structure: dict | None = None,
    risk_snapshot: dict | None = None,
    strategy_stats: dict | None = None,
    execution_backend: str = "PAPER",
) -> dict:
    """从 signal / market_snapshot / risk 构造判单 payload。
    execution_backend 只用于记录，不参与判断。
    """
    return {
        "symbol": symbol,
        "side": direction,
        "strategy_tag": strategy_tag,
        "source_signal": signal_label,
        "execution_backend": execution_backend,
        "price": entry_price,
        "entry_ref": entry_price,
        "planned_stop_loss": sl_price,
        "planned_take_profit_1": tp1_price,
        "planned_take_profit_2": tp2_price,
        "market_snapshot": {
            "price_change_5m": _g(market_snapshot, "price_change_5m"),
            "price_change_15m": _g(market_snapshot, "price_change_15m"),
            "price_change_1h": _g(market_snapshot, "price_change_1h"),
            "price_change_4h": _g(market_snapshot, "price_change_4h"),
            "price_change_24h": _g(market_snapshot, "price_change_24h"),
            "volume_ratio": _g(market_snapshot, "volume_ratio"),
            "oi_change_15m": _g(market_snapshot, "oi_change_15m"),
            "oi_change_1h": _g(market_snapshot, "oi_change_1h"),
            "oi_change_4h": _g(market_snapshot, "oi_change_4h"),
            "taker_ratio": _g(market_snapshot, "taker_ratio"),
            "funding": _g(market_snapshot, "funding"),
            "premium": _g(market_snapshot, "premium"),
            "btc_state": str(_g(market_snapshot, "btc_state", "UNKNOWN")),
        },
        "structure": {
            "support": _g(structure, "support"),
            "resistance": _g(structure, "resistance"),
            "distance_to_support_pct": _g(structure, "distance_to_support_pct"),
            "distance_to_resistance_pct": _g(structure, "distance_to_resistance_pct"),
            "four_h_trend": str(_g(structure, "four_h_trend", "UNKNOWN")),
            "one_h_structure": str(_g(structure, "one_h_structure", "UNKNOWN")),
        },
        "strategy_stats": {
            "strategy_7d_win_rate": _g(strategy_stats, "strategy_7d_win_rate"),
            "strategy_7d_pnl": _g(strategy_stats, "strategy_7d_pnl"),
            "recent_failure_tags": strategy_stats.get("recent_failure_tags", []) if strategy_stats else [],
        },
        "risk_snapshot": {
            "daily_loss_used_pct": _g(risk_snapshot, "daily_loss_used_pct"),
            "open_positions": _g(risk_snapshot, "open_positions"),
            "same_direction_positions": _g(risk_snapshot, "same_direction_positions"),
            "symbol_exposure_usdt": _g(risk_snapshot, "symbol_exposure_usdt"),
            "total_exposure_usdt": _g(risk_snapshot, "total_exposure_usdt"),
        },
    }


# ── 2. should_call_judge ─────────────────────────────────────────────────────
def should_call_judge(payload: dict) -> tuple[bool, str]:
    """判断是否值得调用 AI。只基于配置/质量/预算，不基于 PAPER/LIVE。"""
    cfg = _cfg
    if not cfg("SINGLE_COIN_JUDGE_ENABLED", False):
        return False, "disabled"

    tag = str(payload.get("strategy_tag", ""))
    allowed = cfg("SINGLE_COIN_JUDGE_ALLOWED_TAGS", [])
    if allowed and tag not in allowed:
        return False, f"tag_not_allowed:{tag}"

    min_score = cfg("SINGLE_COIN_JUDGE_MIN_SCORE", 75)
    # 简单质量分：有价格+方向+tag 就给基础分
    score = 50
    if payload.get("price"):
        score += 20
    if payload.get("planned_stop_loss"):
        score += 15
    if tag:
        score += 15
    if score < min_score:
        return False, f"low_score:{score}"

    symbol = str(payload.get("symbol", ""))
    cooldown = float(cfg("SINGLE_COIN_JUDGE_SYMBOL_COOLDOWN_SEC", 900) or 900)
    last_call = _symbol_cooldown.get(symbol, 0.0)
    if time.time() - last_call < cooldown:
        return False, f"symbol_cooldown:{symbol}"
    _reset_budget_counters()

    global _SCAN_COUNT, _HOUR_COUNT, _DAY_COUNT
    max_per_scan = int(cfg("SINGLE_COIN_JUDGE_MAX_PER_SCAN", 1) or 1)
    max_per_hour = int(cfg("SINGLE_COIN_JUDGE_MAX_PER_HOUR", 10) or 10)
    max_per_day = int(cfg("SINGLE_COIN_JUDGE_MAX_PER_DAY", 50) or 50)

    if _SCAN_COUNT >= max_per_scan:
        return False, "budget_per_scan"
    if _HOUR_COUNT >= max_per_hour:
        return False, "budget_per_hour"
    if _DAY_COUNT >= max_per_day:
        return False, "budget_per_day"

    return True, ""


_symbol_cooldown: dict[str, float] = {}
_scan_budget_used: int = 0


def _reset_budget_counters() -> None:
    global _LAST_HOUR_RESET, _LAST_DAY_RESET, _HOUR_COUNT, _DAY_COUNT
    now = time.time()
    if now - _LAST_HOUR_RESET > 3600:
        _HOUR_COUNT = 0
        _LAST_HOUR_RESET = now
    if now - _DAY_RESET > 86400:
        _DAY_COUNT = 0


_DAY_RESET: float = 0.0


# ── 3. judge_single_coin ─────────────────────────────────────────────────────
async def judge_single_coin(payload: dict) -> JudgeResult:
    """统一入口：AI 单币判单。PAPER/LIVE 共用。"""
    ok, reason = should_call_judge(payload)
    if not ok:
        return default_judge_result(reason, skipped=True, skip_reason=reason)

    # 检查缓存
    cache_key = build_cache_key(payload)
    cached = get_cached_judge(cache_key)
    if cached is not None:
        cached.used_cache = True
        return cached

    # 构造 prompt 并调用 AI
    try:
        prompt = str(_cfg("SINGLE_COIN_JUDGE_PROMPT", ""))
        if not prompt:
            prompt = _DEFAULT_PROMPT
        from scanner.ai_gateway import call_single_coin_judge as _call_judge
        raw = await _call_judge(payload, prompt, timeout=int(_cfg("SINGLE_COIN_JUDGE_TIMEOUT_SEC", 20) or 20))
        result = parse_judge_response(raw)
        result = normalize_judge_result(result)
        set_cached_judge(cache_key, result)
        _mark_called(payload)
        return result
    except Exception as e:
        logger.warning("单币 AI 判单异常: %s", e)
        return default_judge_result(f"exception:{e}")


def _mark_called(payload: dict) -> None:
    """记录本次 AI 调用，用于预算控制。"""
    global _SCAN_COUNT, _HOUR_COUNT, _DAY_COUNT, _LAST_HOUR_RESET, _LAST_DAY_RESET, _scan_budget_used
    _SCAN_COUNT += 1
    _HOUR_COUNT += 1
    _DAY_COUNT += 1
    symbol = str(payload.get("symbol", ""))
    if symbol:
        _symbol_cooldown[symbol] = time.time()
    _scan_budget_used += 1


# ── 4. parse_judge_response ─────────────────────────────────────────────────
def parse_judge_response(raw: dict | None) -> JudgeResult:
    """解析 AI 返回的 JSON，失败返回安全默认值。"""
    if not raw:
        return default_judge_result("empty_response")
    try:
        text = raw.get("text", "")
        if not text:
            # try raw as the parsed response directly
            if "action" in raw:
                return _dict_to_result(raw)
            return default_judge_result("empty_text")
        # try parsing as JSON
        if isinstance(text, str):
            text = text.strip()
            if text.startswith("{"):
                data = json.loads(text)
                return _dict_to_result(data)
            # might be nested in a JSON code block
            if "```json" in text:
                start = text.index("```json") + 7
                end = text.index("```", start) if "```" in text[start:] else len(text)
                data = json.loads(text[start:end].strip())
                return _dict_to_result(data)
            if "```" in text:
                start = text.index("```") + 3
                end = text.index("```", start) if "```" in text[start:] else len(text)
                data = json.loads(text[start:end].strip())
                return _dict_to_result(data)
        return default_judge_result("not_json")
    except Exception as e:
        logger.warning("AI 判单 JSON 解析失败: %s", e)
        return default_judge_result(f"parse_error:{e}")


def _dict_to_result(d: dict) -> JudgeResult:
    return JudgeResult(
        action=str(d.get("action", "WAIT")).upper() if str(d.get("action", "")).upper() in ("ALLOW", "WAIT", "REJECT") else "WAIT",
        direction=str(d.get("direction", "NONE")).upper() if str(d.get("direction", "")).upper() in ("LONG", "SHORT", "NONE") else "NONE",
        confidence=float(d.get("confidence", 0.0) or 0.0),
        entry_condition=str(d.get("entry_condition", "")),
        invalidation_condition=str(d.get("invalidation_condition", "")),
        support=_safe_float(d.get("support")),
        resistance=_safe_float(d.get("resistance")),
        suggested_stop_loss=_safe_float(d.get("suggested_stop_loss")),
        suggested_take_profit_1=_safe_float(d.get("suggested_take_profit_1")),
        suggested_take_profit_2=_safe_float(d.get("suggested_take_profit_2")),
        position_size_level=str(d.get("position_size_level", "NONE")).upper() if str(d.get("position_size_level", "")).upper() in ("NONE", "SMALL", "HALF", "FULL") else "NONE",
        risk_notes=list(d.get("risk_notes", []) or []),
        reason=str(d.get("reason", "")),
        raw_response=d,
    )


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f != 0 else None
    except (TypeError, ValueError):
        return None


# ── 5. normalize_judge_result ────────────────────────────────────────────────
def normalize_judge_result(result: JudgeResult) -> JudgeResult:
    """补齐缺失字段、限制非法值。"""
    if result.action not in ("ALLOW", "WAIT", "REJECT"):
        result.action = "WAIT"
    if result.direction not in ("LONG", "SHORT", "NONE"):
        result.direction = "NONE"
    result.confidence = max(0.0, min(1.0, result.confidence))
    result.risk_notes = [str(n) for n in (result.risk_notes or [])]
    result.used_cache = False
    return result


# ── 6. get_cached_judge ──────────────────────────────────────────────────────
def get_cached_judge(cache_key: str) -> JudgeResult | None:
    """从缓存中获取判单结果。"""
    ttl = float(_cfg("SINGLE_COIN_JUDGE_CACHE_TTL_SEC", 900) or 900)
    entry = _CACHE.get(cache_key)
    if entry and time.time() - entry[0] < ttl:
        logger.debug("单币判单缓存命中: %s", cache_key[:40])
        return entry[1]
    return None


# ── 7. set_cached_judge ──────────────────────────────────────────────────────
def set_cached_judge(cache_key: str, result: JudgeResult) -> None:
    """写入判单缓存。"""
    _CACHE[cache_key] = (time.time(), result)
    # 限制缓存大小
    if len(_CACHE) > 200:
        oldest = min(_CACHE.keys(), key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)


# ── 8. enforce_budget_limits ─────────────────────────────────────────────────
def enforce_budget_limits() -> tuple[bool, str]:
    """检查当前预算是否可以继续调用。"""
    _reset_budget_counters()
    cfg = _cfg
    max_per_hour = int(cfg("SINGLE_COIN_JUDGE_MAX_PER_HOUR", 10) or 10)
    max_per_day = int(cfg("SINGLE_COIN_JUDGE_MAX_PER_DAY", 50) or 50)
    if _HOUR_COUNT >= max_per_hour:
        return False, "budget_per_hour"
    if _DAY_COUNT >= max_per_day:
        return False, "budget_per_day"
    return True, ""


# ── 9. build_cache_key ───────────────────────────────────────────────────────
def build_cache_key(payload: dict) -> str:
    """生成缓存 key。不包含 execution_backend，确保 PAPER/LIVE 缓存共享。"""
    ms = payload.get("market_snapshot", {}) or {}
    parts = [
        str(payload.get("symbol", "")),
        str(payload.get("side", "")),
        str(payload.get("strategy_tag", "")),
        _price_bucket(_g(ms, "price_change_15m")),
        _bucket(_g(ms, "oi_change_15m")),
        _bucket(_g(ms, "taker_ratio")),
        str(_g(ms, "btc_state", "UNKNOWN")),
    ]
    raw = "|".join(parts)
    return hashlib.md5(raw.encode()).hexdigest()


def _price_bucket(v) -> str:
    if v is None:
        return "null"
    v = float(v)
    if abs(v) < 0.3:
        return "flat"
    if v > 0:
        return "up" if v < 2.0 else "up_big"
    return "down" if v > -2.0 else "down_big"


def _bucket(v) -> str:
    if v is None:
        return "null"
    v = float(v)
    if abs(v) < 0.1:
        return "flat"
    if v > 0:
        return "pos"
    return "neg"


# ── 10. default_judge_result ─────────────────────────────────────────────────
def default_judge_result(reason: str = "", skipped: bool = False, skip_reason: str = "") -> JudgeResult:
    """AI 调用失败/预算不足时返回的安全默认值。"""
    default_action = str(_cfg("SINGLE_COIN_JUDGE_DEFAULT_ON_ERROR", "WAIT")).upper()
    if default_action not in ("ALLOW", "WAIT", "REJECT"):
        default_action = "WAIT"
    return JudgeResult(
        action=default_action,
        direction="NONE",
        confidence=0.0,
        reason=reason or "default_fallback",
        skipped=skipped,
        skip_reason=skip_reason,
    )


# ── 默认 prompt ──────────────────────────────────────────────────────────────
_DEFAULT_PROMPT = """
你是加密货币合约短线交易的单币审核官。

你的任务不是寻找新交易，而是审核系统已经发现的一笔候选交易是否应该继续执行。

你必须基于输入的结构化数据，输出严格 JSON。
你不能直接下单。你不能修改仓位。你不能修改杠杆。你不能扩大止损。你不能绕过风控。
你不能因为 PAPER 或 LIVE 改变判断。PAPER 和 LIVE 的决策逻辑必须完全一致。

请判断：
1. 当前价格位置是否适合执行该方向
2. OI / 成交量 / taker / funding / premium 是否支持该方向
3. 当前是否接近支撑或阻力，是否容易被反抽或假突破
4. BTC 环境是否支持山寨延续
5. 该策略近期表现和失败原因是否增加风险
6. 是否应该 ALLOW、WAIT 或 REJECT

只输出 JSON，不要输出任何解释文字。

返回格式：
{
  "action": "ALLOW | WAIT | REJECT",
  "direction": "LONG | SHORT | NONE",
  "confidence": 0.0,
  "entry_condition": "string",
  "invalidation_condition": "string",
  "support": 0.0,
  "resistance": 0.0,
  "suggested_stop_loss": 0.0,
  "suggested_take_profit_1": 0.0,
  "suggested_take_profit_2": 0.0,
  "position_size_level": "NONE | SMALL | HALF | FULL",
  "risk_notes": ["string"],
  "reason": "string"
}
"""


# ── 工具函数 ─────────────────────────────────────────────────────────────────
def _g(d: dict | None, key: str, default=None):
    if not d:
        return default
    return d.get(key, default)


def judge_to_signal_fields(result: JudgeResult) -> dict:
    """将 JudgeResult 转为 signal/trade 记录字段。"""
    return {
        "ai_judge_enabled": True,
        "ai_judge_called": True,
        "ai_judge_used_cache": result.used_cache,
        "ai_judge_action": result.action,
        "ai_judge_confidence": round(result.confidence, 4),
        "ai_judge_reason": result.reason,
        "ai_judge_risk_notes": result.risk_notes,
        "ai_judge_entry_condition": result.entry_condition,
        "ai_judge_invalidation_condition": result.invalidation_condition,
        "ai_judge_support": result.support,
        "ai_judge_resistance": result.resistance,
        "ai_judge_blocked": False,
        "ai_judge_block_reason": "",
    }
