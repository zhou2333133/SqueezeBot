"""
scanner/sources/token_supply.py — 上币时间 + 流通量 + 启发式 vesting 分组

公开接口：
    await refresh_listing_dates(session)            # 拉一次 Binance exchangeInfo
    get_listing_age_days(symbol)                    # 缓存查询，无副作用
    classify_vesting(symbol, market_cap, fdv,
                     volume_24h, listing_age_days,
                     circulation_pct, fdv_ratio)    # 启发式分组（纯函数，可单测）
    enrich_candidate(candidate_dict, settings)      # 给 candidates_map 一行注入：
                                                    #   listing_age_days / circulation_pct / fdv_ratio
                                                    #   / vesting_phase / flash_eligible / flash_ban_reason

Vesting phase 取值（V4AF 选币用）：
    pre_unlock         未开始解锁或解锁极少（< 60 天 + circulation < 30%）            ★ 目标池
    unlock_active      处于解锁期、持续抛压（60–365 天 + 30–70% 流通）                ★ 目标池
    unlock_late        大额解锁完成低关注度（> 365 天 + 中低流量 + 距 ATH > 70%）     ★ 目标池
    near_full_meme     接近全流通的低关注度 meme（fdv_ratio > 阈值）                  ❌ ban
    low_volume         24h 量低于阈值                                                 ❌ ban
    unknown            数据不足                                                       ❌ ban（默认）

资料来源（全免费，无需 key）：
    - Binance /fapi/v1/exchangeInfo: onboardDate（合约首次上线时间戳）
    - candidates_map 已有：market_cap / volume_24h
    - candidates_map 增量：circulating_supply / total_supply（如果 yaobi_scanner 后续填充就用，没填就启发式跳过）
"""
from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

_BINANCE_FAPI = "https://fapi.binance.com"

# symbol -> {"onboard_ms": int, "status": str}
_listing_cache: dict[str, dict] = {}
_last_refresh_at: float = 0.0
_REFRESH_TTL_SEC = 86400   # exchangeInfo 一天刷一次足够


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def refresh_listing_dates(session: aiohttp.ClientSession, force: bool = False) -> int:
    """从 Binance Futures exchangeInfo 拉所有 USDT 合约的上线时间。"""
    global _last_refresh_at
    now = time.time()
    if not force and _listing_cache and (now - _last_refresh_at) < _REFRESH_TTL_SEC:
        return len(_listing_cache)
    try:
        async with session.get(
            f"{_BINANCE_FAPI}/fapi/v1/exchangeInfo",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 400:
                logger.warning("token_supply: exchangeInfo http %s", resp.status)
                return len(_listing_cache)
            body = await resp.json()
    except Exception as e:
        logger.warning("token_supply: exchangeInfo 拉取失败: %s", e)
        return len(_listing_cache)

    new_cache: dict[str, dict] = {}
    for item in body.get("symbols", []) or []:
        symbol = (item.get("symbol") or "").upper()
        if not symbol or item.get("contractType") != "PERPETUAL":
            continue
        if item.get("quoteAsset") != "USDT":
            continue
        onboard = item.get("onboardDate")
        if not onboard:
            continue
        new_cache[symbol] = {
            "onboard_ms": int(onboard),
            "status": item.get("status", "TRADING"),
        }
    if new_cache:
        _listing_cache.clear()
        _listing_cache.update(new_cache)
        _last_refresh_at = now
    return len(_listing_cache)


def get_listing_age_days(symbol: str) -> int:
    """距合约上线天数；不存在返回 0。"""
    info = _listing_cache.get(symbol.upper())
    if not info:
        return 0
    onboard_ms = info.get("onboard_ms", 0)
    if onboard_ms <= 0:
        return 0
    age_seconds = time.time() - (onboard_ms / 1000.0)
    return max(0, int(age_seconds // 86400))


def listing_status() -> dict:
    return {
        "cached": len(_listing_cache),
        "last_refresh_at": _last_refresh_at,
    }


# ── 启发式分类（纯函数，可单测）──────────────────────────────────────────────

def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    if not b or b == 0:
        return default
    return a / b


def classify_vesting(
    *,
    listing_age_days: int,
    market_cap: float,
    fdv: float,
    volume_24h: float,
    circulation_pct: float,    # 0-1
    price_drawdown_from_ath_pct: float = 0.0,
    cfg: dict | None = None,
) -> tuple[str, bool, str]:
    """
    返回 (vesting_phase, flash_eligible, flash_ban_reason)。

    优先级：低流动性 > 接近全流通 meme > 三个目标组 > unknown
    """
    cfg = cfg or {}
    min_volume = float(cfg.get("FLASH_VOLUME_24H_MIN_USD", 1_000_000.0))
    fdv_max = float(cfg.get("FLASH_FDV_RATIO_MAX", 0.85))
    ban_meme = bool(cfg.get("FLASH_BAN_NEAR_FULL_CIRC_MEME", True))

    fdv_ratio = _safe_div(market_cap, fdv) if fdv > 0 else 0.0

    if volume_24h > 0 and volume_24h < min_volume:
        return "low_volume", False, f"volume_24h<{min_volume:.0f}"

    if ban_meme and fdv_ratio >= fdv_max:
        return "near_full_meme", False, f"fdv_ratio>={fdv_max:.2f}"

    if listing_age_days <= 0:
        return "unknown", False, "no_listing_date"

    # 1. 未解锁 / 早期 VC
    if listing_age_days < 60:
        if circulation_pct == 0 or circulation_pct < 0.30:
            return "pre_unlock", True, ""
        # 新币但流通度高 = 可能是 fair-launch / meme，看 fdv_ratio
        if fdv_ratio > 0.0 and fdv_ratio < fdv_max:
            return "pre_unlock", True, ""

    # 2. 解锁期持续抛压
    if 60 <= listing_age_days <= 365:
        if 0.0 < circulation_pct < 0.70 or circulation_pct == 0:
            return "unlock_active", True, ""

    # 3. 大额解锁完成低关注度老币（V4AF 推崇的"绝佳模板"）
    if listing_age_days > 365:
        # 高跌幅 + 仍有流量 = 老庄币
        if (circulation_pct == 0 or circulation_pct >= 0.70) and price_drawdown_from_ath_pct >= 70.0:
            return "unlock_late", True, ""
        if circulation_pct == 0 or circulation_pct >= 0.70:
            return "unlock_late", True, ""

    return "unknown", False, "no_phase_match"


def enrich_candidate(c: dict, settings: dict) -> dict:
    """给一个 candidate dict 注入 V4AF 选币所需字段（in-place 修改并返回）。"""
    symbol = (c.get("symbol") or "").upper()
    age = get_listing_age_days(symbol)

    market_cap = _as_float(c.get("market_cap"))
    # FDV 当前妖币扫描器没有显式字段，后续若接入再填；现在用 0 兜底
    fdv = _as_float(c.get("fdv"))
    volume_24h = _as_float(c.get("volume_24h"))
    circulating = _as_float(c.get("circulating_supply"))
    total = _as_float(c.get("total_supply"))
    circ_pct = _safe_div(circulating, total) if total > 0 else 0.0
    fdv_ratio = _safe_div(market_cap, fdv) if fdv > 0 else 0.0

    drawdown = _as_float(c.get("price_drawdown_from_ath_pct"))

    phase, eligible, ban = classify_vesting(
        listing_age_days=age,
        market_cap=market_cap,
        fdv=fdv,
        volume_24h=volume_24h,
        circulation_pct=circ_pct,
        price_drawdown_from_ath_pct=drawdown,
        cfg=settings,
    )

    c["listing_age_days"]    = age
    c["circulating_supply"]  = circulating
    c["total_supply"]        = total
    c["circulation_pct"]     = round(circ_pct, 4)
    c["fdv_ratio"]           = round(fdv_ratio, 4)
    c["vesting_phase"]       = phase
    c["flash_eligible"]      = eligible
    c["flash_ban_reason"]    = ban
    return c
