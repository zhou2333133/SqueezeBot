"""
币安合约 OI 扫描 — 检测妖币合约信号
指标:
  OI日增%      — 今日OI相对24h前的变化
  成交量放量倍数 — 今日量 vs 7日均量
  OI死平N天    — 连续N天OI变化<5%（蓄势）
  OI加速       — 近4h OI变化率 vs 前4h（趋势加速）
  大户多空比    — 大户多头比例（<0.45 = 大户翻空）
  空头拥挤      — 全局空头比例
"""
from __future__ import annotations
import asyncio
import logging

import aiohttp

from market_hub import hub

logger = logging.getLogger(__name__)

_BASE = "https://fapi.binance.com"
_HEADERS = {"Accept": "application/json"}


async def scan_futures_oi(
    session: aiohttp.ClientSession,
    top_n: int = 120,
) -> list[dict]:
    """
    扫描合约市场OI异动，返回有信号的币种列表。
    每个元素 dict 可直接用于构建 Candidate。
    """
    tickers = await _get_24h_tickers(session)
    if not tickers:
        logger.warning("合约OI扫描: 无法获取行情数据")
        return []

    # Filter USDT perpetuals, rank by quote volume
    usdt = [
        t for t in tickers
        if isinstance(t, dict)
        and t.get("symbol", "").endswith("USDT")
        and "_" not in t.get("symbol", "")
    ]
    usdt.sort(key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)

    top_symbols  = [t["symbol"] for t in usdt[:top_n]]
    ticker_index = {t["symbol"]: t for t in usdt}

    logger.info("合约OI扫描: 分析前 %d 个交易对", len(top_symbols))

    results = []
    batch_size = 10
    for i in range(0, len(top_symbols), batch_size):
        batch = top_symbols[i : i + batch_size]
        tasks = [_analyze_symbol(session, sym, ticker_index) for sym in batch]
        batch_res = await asyncio.gather(*tasks, return_exceptions=True)
        for r in batch_res:
            if isinstance(r, dict) and r.get("has_signal"):
                results.append(r)
        if i + batch_size < len(top_symbols):
            await asyncio.sleep(0.4)

    logger.info("合约OI扫描: %d 个合约有信号", len(results))
    return results


# ── 单币分析 ─────────────────────────────────────────────────────────────────

async def _analyze_symbol(
    session: aiohttp.ClientSession,
    symbol: str,
    ticker_index: dict,
) -> dict:
    try:
        oi_hist_4h, klines_1d, trader_ratio, funding_rate, ls_ratio = await asyncio.gather(
            _get_oi_hist(session, symbol, period="4h", limit=120),
            _get_klines(session, symbol, interval="1d", limit=10),
            _get_top_trader_ratio(session, symbol),
            _get_funding_rate(session, symbol),
            _get_global_ls_ratio(session, symbol),
            return_exceptions=True,
        )

        if not isinstance(oi_hist_4h, list) or len(oi_hist_4h) < 7:
            return {}
        if not isinstance(klines_1d, list) or len(klines_1d) < 2:
            return {}

        sym = symbol.replace("USDT", "")
        result: dict = {
            "symbol":             sym,
            "has_signal":         False,
            "has_futures":        True,
            "oi_change_24h_pct":  0.0,
            "oi_acceleration":    0.0,
            "oi_flat_days":       0,
            "volume_ratio":       1.0,
            "whale_long_ratio":   0.5,
            "short_crowd_pct":    50.0,
            "price_change_24h":   0.0,
            "signals":            [],
            "category":           "",
            "funding_rate_pct":   0.0,
            "fr_extreme_short":   False,
            "retail_short_pct":   50.0,
        }

        # ── OI 日增% (24h = 6 个 4h 周期) ───────────────────────────────────
        oi_now  = float(oi_hist_4h[-1]["sumOpenInterest"])
        lookback = min(6, len(oi_hist_4h) - 1)
        oi_24h  = float(oi_hist_4h[-1 - lookback]["sumOpenInterest"])
        oi_change_24h = (oi_now - oi_24h) / oi_24h * 100 if oi_24h > 0 else 0.0
        result["oi_change_24h_pct"] = round(oi_change_24h, 1)

        # ── OI 加速 (近4h vs 前4h) ────────────────────────────────────────────
        if len(oi_hist_4h) >= 3:
            oi_4h_ago = float(oi_hist_4h[-2]["sumOpenInterest"])
            oi_8h_ago = float(oi_hist_4h[-3]["sumOpenInterest"])
            recent_chg = (oi_now  - oi_4h_ago) / oi_4h_ago * 100 if oi_4h_ago > 0 else 0
            prev_chg   = (oi_4h_ago - oi_8h_ago) / oi_8h_ago * 100 if oi_8h_ago > 0 else 0
            result["oi_acceleration"] = round(recent_chg - prev_chg, 1)

        # ── OI 死平天数 ──────────────────────────────────────────────────────
        result["oi_flat_days"] = _calc_flat_days(oi_hist_4h)

        # ── 成交量放量倍数 ────────────────────────────────────────────────────
        today_vol = float(klines_1d[-1][5])
        prev_vols = [float(k[5]) for k in klines_1d[-8:-1]]
        avg_vol   = sum(prev_vols) / len(prev_vols) if prev_vols else 1
        vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
        result["volume_ratio"] = round(vol_ratio, 1)

        # ── 24h 价格变化 ─────────────────────────────────────────────────────
        t = ticker_index.get(symbol, {})
        result["price_change_24h"] = round(float(t.get("priceChangePercent", 0) or 0), 1)
        result["price_usd"]        = float(t.get("lastPrice", 0) or 0)

        # ── 大户多空比 ───────────────────────────────────────────────────────
        if isinstance(trader_ratio, list) and trader_ratio:
            ls = float(trader_ratio[-1].get("longShortRatio", 1.0) or 1.0)
            long_pct = ls / (1 + ls) * 100
            result["whale_long_ratio"] = round(long_pct / 100, 3)
            result["short_crowd_pct"]  = round(100 - long_pct, 1)
            hub.update_smart_ls(symbol, result["whale_long_ratio"])

        # ── 资金费率 & 全局多空比 ────────────────────────────────────────────
        if isinstance(funding_rate, float):
            fr_pct = round(funding_rate * 100, 4)
            result["funding_rate_pct"] = fr_pct
            result["fr_extreme_short"] = fr_pct < -0.05

        if isinstance(ls_ratio, float):
            result["retail_short_pct"] = round(ls_ratio * 100, 1)

        # ── 更新 Hub OI ──────────────────────────────────────────────────────
        if isinstance(oi_hist_4h, list) and oi_hist_4h:
            oi_usdt_now = float(oi_hist_4h[-1].get("sumOpenInterestValue", 0) or 0)
            if oi_usdt_now > 0:
                hub.update_bnc_oi(symbol, oi_usdt_now)

        # ── 信号生成 & 分类 ──────────────────────────────────────────────────
        sigs: list[str] = []
        is_launch = is_accum = is_risk = False

        oi_chg = result["oi_change_24h_pct"]
        v_ratio = result["volume_ratio"]
        flat_d  = result["oi_flat_days"]
        accel   = result["oi_acceleration"]
        whale_s = (1 - result["whale_long_ratio"]) * 100   # short%
        short_c = result["short_crowd_pct"]
        price_c = result["price_change_24h"]

        # OI日增
        if oi_chg >= 80:
            sigs.append(f"OI日增+{oi_chg:.0f}%")
            is_launch = True
        elif oi_chg >= 40:
            sigs.append(f"OI日增+{oi_chg:.0f}%")
            is_accum = True

        # 成交量放量
        if v_ratio >= 10:
            sigs.append(f"成交量{v_ratio:.0f}x放量")
            is_launch = True
        elif v_ratio >= 5:
            sigs.append(f"成交量{v_ratio:.0f}x放量")
            is_accum = True

        # 24h价格
        if abs(price_c) >= 10:
            sign = "+" if price_c >= 0 else ""
            sigs.append(f"24h{sign}{price_c:.0f}%")

        # OI死平 + 首次放量
        if flat_d >= 7 and v_ratio >= 5:
            sigs.append(f"OI死平{flat_d}天+首次放量{v_ratio:.0f}x")
            is_accum = True

        # OI加速
        if accel >= 20 and v_ratio >= 3:
            sigs.append(f"OI加速+{accel:.0f}%+放量{v_ratio:.0f}x")
            is_accum = True

        # 大户翻空
        if result["whale_long_ratio"] > 0 and whale_s >= 55:
            excess = whale_s - 50
            sigs.append(f"大户翻空-{excess:.0f}%")
            is_risk = True

        # 空头拥挤
        if short_c >= 65:
            sigs.append(f"空头{short_c:.0f}%拥挤")
            is_risk = True

        # 庄家出货 (OI↓ 价格↑)
        if oi_chg < -15 and price_c > 10:
            sigs.append("OI↓价格↑(庄家出货)")
            is_risk = True

        # 空头撤退 (OI↓ + 暴涨 = 空头爆仓)
        if oi_chg < -20 and price_c > 20:
            sigs.append("空头撤退(庄家收网)")
            is_accum = True

        result["signals"] = sigs

        # 分类优先级：风险 > 启动预警 > 蓄势观察
        if is_risk:
            result["category"] = "风险"
        elif is_launch:
            result["category"] = "启动预警"
        elif is_accum:
            result["category"] = "蓄势观察"

        result["has_signal"] = bool(sigs) and bool(result["category"])
        return result

    except Exception as e:
        logger.debug("OI分析 %s 异常: %s", symbol, e)
        return {}


def _calc_flat_days(oi_hist_4h: list) -> int:
    """计算最近连续OI死平天数 (每天=6个4h周期, 日变化<5% = 死平)"""
    n = len(oi_hist_4h)
    if n < 7:
        return 0
    flat = 0
    max_days = min(20, (n - 1) // 6)
    for day in range(max_days):
        end_i   = n - 1 - day * 6
        start_i = end_i - 6
        if start_i < 0:
            break
        oi_e = float(oi_hist_4h[end_i]["sumOpenInterest"])
        oi_s = float(oi_hist_4h[start_i]["sumOpenInterest"])
        if oi_s <= 0:
            break
        daily_chg = abs((oi_e - oi_s) / oi_s * 100)
        if daily_chg < 5:
            flat += 1
        else:
            break
    return flat


# ── API helpers ───────────────────────────────────────────────────────────────

async def _get_24h_tickers(session: aiohttp.ClientSession) -> list:
    try:
        async with session.get(
            f"{_BASE}/fapi/v1/ticker/24hr",
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        logger.warning("合约24h行情失败: %s", e)
    return []


async def _get_oi_hist(session, symbol, period="4h", limit=120) -> list:
    try:
        async with session.get(
            f"{_BASE}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": period, "limit": limit},
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return []


async def _get_klines(session, symbol, interval="1d", limit=10) -> list:
    try:
        async with session.get(
            f"{_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return []


async def _get_funding_rate(session: aiohttp.ClientSession, symbol: str) -> float:
    """获取当前资金费率，返回浮点数如 -0.0006 表示 -0.06%"""
    try:
        async with session.get(
            f"{_BASE}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data.get("lastFundingRate", 0) or 0)
    except Exception:
        pass
    return 0.0


async def _get_global_ls_ratio(session: aiohttp.ClientSession, symbol: str) -> float:
    """获取全局账户多空比，返回空头侧占比(0~1)，如 0.68 表示68%账户持空"""
    try:
        async with session.get(
            f"{_BASE}/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": "5m", "limit": 1},
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and isinstance(data, list):
                    long_ratio = float(data[0].get("longAccount", 0.5) or 0.5)
                    return round(1.0 - long_ratio, 4)
    except Exception:
        pass
    return 0.5


async def _get_top_trader_ratio(session, symbol) -> list:
    try:
        async with session.get(
            f"{_BASE}/futures/data/topLongShortPositionRatio",
            params={"symbol": symbol, "period": "5m", "limit": 1},
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return []
