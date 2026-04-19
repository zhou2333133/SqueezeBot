"""
全局市场数据中心 — 统一缓存多维度合约信号
数据每5分钟自动刷新，所有机器人共享，节省API配额

数据来源 (均为免费公开接口, 无需API Key):
  Binance /futures/data/takeBuySellVolume          — 主动买卖量分布
  Binance /futures/data/globalLongShortAccountRatio — 散户(全体账户)多空比
  Binance /futures/data/basis                       — 期现价差(升贴水)
  OKX     /api/v5/market/open-interest              — OKX全市场OI (跨所对比)

速率预算 (Binance weight/min 上限 2400):
  Taker + 散户LS: 120个币 × 2端点 / 5min = 48/min  ←  2%
  Basis:          120个币 / 15min        =  8/min  ←  0.3%
  OKX:            1批次 / 15min          ≈  0/min
  合计: ~56/min，远低于上限
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

_BNFUT  = "https://fapi.binance.com"
_OKXPUB = "https://www.okx.com"

_TTL_TAKER_LS = 300    # 5分钟刷新: Taker + 散户LS
_TTL_BASIS    = 900    # 15分钟刷新: 基差
_TTL_OKX      = 900    # 15分钟刷新: OKX OI
_TOP_N        = 120    # 扫描前N个合约(按24h量排名)
_BATCH_SIZE   = 20     # 每批并发请求数
_BATCH_SLEEP  = 0.35   # 批次间隔(秒), 防限速


@dataclass
class SymbolMetrics:
    """单币种多维度市场信号"""

    # ── Taker 主动买卖 ──────────────────────────────────────────────────────
    taker_buy_pct_1h:  float = 0.5    # 近1h主动买入占比 (0~1, 0.5=中性)
    taker_buy_pct_30m: float = 0.5    # 近30min主动买入占比
    taker_buy_trend:   str   = "flat" # "rising"/"falling"/"flat"

    # ── 多空比 (散户 vs 大户) ──────────────────────────────────────────────
    retail_long_pct:   float = 0.5    # 全体散户多头比例
    smart_long_pct:    float = 0.5    # 大户(Top Trader)多头比例
    ls_divergence:     float = 0.0    # 散户% - 大户% (正=散户更看多, 反向信号)

    # ── 期现基差 ──────────────────────────────────────────────────────────
    basis_pct:         float = 0.0    # 期货溢价% (正=升水/看多情绪, 负=贴水)

    # ── 跨交易所 OI ───────────────────────────────────────────────────────
    bnc_oi_usdt:       float = 0.0    # 币安 OI (USDT)
    okx_oi_usdt:       float = 0.0    # OKX  OI (USDT)
    oi_concentration:  float = 0.0    # 币安OI占比 (>0.7 = 资金集中在币安)

    # ── 元数据 ────────────────────────────────────────────────────────────
    taker_updated: float = 0.0
    basis_updated: float = 0.0

    def is_taker_fresh(self) -> bool:
        return (time.time() - self.taker_updated) < _TTL_TAKER_LS * 2

    def signal_str(self) -> str:
        """给日志用的简短描述"""
        parts = []
        if abs(self.taker_buy_pct_1h - 0.5) > 0.04:
            arrow = "↑" if self.taker_buy_trend == "rising" else ("↓" if self.taker_buy_trend == "falling" else "")
            parts.append(f"Taker买{self.taker_buy_pct_1h*100:.0f}%{arrow}")
        if abs(self.ls_divergence) > 0.08:
            label = "散>智" if self.ls_divergence > 0 else "智>散"
            parts.append(f"{label}{abs(self.ls_divergence)*100:.0f}%")
        if abs(self.basis_pct) > 0.2:
            parts.append(f"基差{self.basis_pct:+.2f}%")
        if self.oi_concentration > 0.75:
            parts.append(f"OI集中{self.oi_concentration*100:.0f}%")
        return " | ".join(parts) if parts else ""


class MarketHub:
    """
    全局市场数据中心 (单例)

    各机器人用法:
        from market_hub import hub
        m = hub.get("BTC")            # SymbolMetrics, 不带USDT
        pct = hub.taker("BTCUSDT")    # 带不带USDT都可以
        div = hub.ls_div("BTC")       # 散户-大户差值
    """

    def __init__(self) -> None:
        self._metrics:     dict[str, SymbolMetrics] = {}   # key = BTCUSDT
        self._okx_oi:      dict[str, float]         = {}   # key = BTC-USDT-SWAP
        self._top_symbols: list[str]                = []   # BTCUSDT list
        self._t_taker_ls = 0.0
        self._t_basis    = 0.0
        self._t_okx      = 0.0
        self.running     = False

    # ── 公共接口 ──────────────────────────────────────────────────────────

    def get(self, symbol: str) -> SymbolMetrics:
        """获取币种数据 (symbol 可带或不带 USDT 后缀)"""
        key = symbol if symbol.endswith("USDT") else symbol + "USDT"
        return self._metrics.get(key, SymbolMetrics())

    def taker(self, symbol: str) -> float:
        """近1h Taker买入比例 (0~1). 0.5=中性"""
        return self.get(symbol).taker_buy_pct_1h

    def taker_trend(self, symbol: str) -> str:
        """Taker买入趋势: 'rising'/'falling'/'flat'"""
        return self.get(symbol).taker_buy_trend

    def ls_div(self, symbol: str) -> float:
        """散户多头% - 大户多头%. 正值=散户比大户更看多(反向信号)"""
        return self.get(symbol).ls_divergence

    def basis(self, symbol: str) -> float:
        """期现溢价%. >0=升水(情绪偏多), <0=贴水(情绪偏空)"""
        return self.get(symbol).basis_pct

    def retail_long(self, symbol: str) -> float:
        """散户多头比例 (0~1)"""
        return self.get(symbol).retail_long_pct

    def smart_long(self, symbol: str) -> float:
        """大户多头比例 (0~1)"""
        return self.get(symbol).smart_long_pct

    def is_retail_crowded_long(self, symbol: str, threshold: float = 0.70) -> bool:
        """散户是否严重偏多 (做空反转信号)"""
        return self.get(symbol).retail_long_pct >= threshold

    def is_smart_bullish(self, symbol: str, threshold: float = 0.52) -> bool:
        """大户是否看多"""
        return self.get(symbol).smart_long_pct >= threshold

    # ── 外部更新接口 (供 binance_futures.py 调用) ─────────────────────────

    def update_smart_ls(self, symbol: str, smart_long_pct: float) -> None:
        """期货扫描器更新大户多空比"""
        key = symbol if symbol.endswith("USDT") else symbol + "USDT"
        if key not in self._metrics:
            self._metrics[key] = SymbolMetrics()
        m = self._metrics[key]
        m.smart_long_pct = smart_long_pct
        m.ls_divergence  = round(m.retail_long_pct - m.smart_long_pct, 4)

    def update_bnc_oi(self, symbol: str, oi_usdt: float) -> None:
        """期货扫描器更新币安OI (USD值)"""
        key = symbol if symbol.endswith("USDT") else symbol + "USDT"
        if key not in self._metrics:
            self._metrics[key] = SymbolMetrics()
        m = self._metrics[key]
        m.bnc_oi_usdt = oi_usdt
        self._calc_oi_concentration(key)

    # ── 后台任务 ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.running = True
        logger.info("📡 MarketHub 启动 (刷新间隔 Taker/LS=5min, Basis=15min)")
        connector = aiohttp.TCPConnector(limit=40, ttl_dns_cache=300)
        async with aiohttp.ClientSession(
            connector=connector,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as sess:
            while self.running:
                try:
                    await self._refresh(sess)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning("📡 MarketHub 刷新异常: %s", e)
                await asyncio.sleep(60)  # 每60s检查一次是否到刷新时间
        self.running = False
        logger.info("📡 MarketHub 已停止")

    async def _refresh(self, sess: aiohttp.ClientSession) -> None:
        now = time.time()

        # 先确保有合约列表
        if not self._top_symbols:
            self._top_symbols = await self._fetch_top_symbols(sess)
            if not self._top_symbols:
                return

        # Taker + 散户LS: 每5分钟
        if now - self._t_taker_ls >= _TTL_TAKER_LS:
            await self._batch_taker_and_ls(sess)
            self._t_taker_ls = now

        # 基差: 每15分钟
        if now - self._t_basis >= _TTL_BASIS:
            await self._batch_basis(sess)
            self._t_basis = now

        # OKX OI: 每15分钟
        if now - self._t_okx >= _TTL_OKX:
            await self._fetch_okx_oi(sess)
            self._t_okx = now

    # ── Taker + 散户LS ────────────────────────────────────────────────────

    async def _batch_taker_and_ls(self, sess: aiohttp.ClientSession) -> None:
        ok_taker = ok_ls = 0
        for i in range(0, len(self._top_symbols), _BATCH_SIZE):
            batch = self._top_symbols[i : i + _BATCH_SIZE]
            # 并发: 每个symbol同时fetch taker + global_ls
            tasks = []
            for sym in batch:
                tasks.append(self._fetch_taker(sess, sym))
                tasks.append(self._fetch_global_ls(sess, sym))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, tuple) and len(r) == 2:
                    rtype, data = r
                    if rtype == "taker" and data:
                        ok_taker += 1
                    elif rtype == "ls" and data:
                        ok_ls += 1
            if i + _BATCH_SIZE < len(self._top_symbols):
                await asyncio.sleep(_BATCH_SLEEP)
        logger.debug("📡 Taker更新%d个 | 散户LS更新%d个", ok_taker, ok_ls)

    async def _fetch_taker(self, sess: aiohttp.ClientSession, symbol: str) -> tuple:
        try:
            async with sess.get(
                f"{_BNFUT}/futures/data/takeBuySellVolume",
                params={"symbol": symbol, "period": "5m", "limit": 24},
            ) as resp:
                if resp.status != 200:
                    return ("taker", None)
                data = await resp.json()
                if not isinstance(data, list) or len(data) < 4:
                    return ("taker", None)

                def _buy_pct(rows: list) -> float:
                    buy  = sum(float(r.get("buyVol",  0) or 0) for r in rows)
                    sell = sum(float(r.get("sellVol", 0) or 0) for r in rows)
                    return buy / (buy + sell) if (buy + sell) > 0 else 0.5

                recent_12 = data[-12:]                        # 近1h
                prev_6    = data[-18:-12] if len(data) >= 18 else data[:6]  # 再前30min
                pct_1h    = _buy_pct(recent_12)
                pct_30m   = _buy_pct(data[-6:])               # 近30min
                pct_prev  = _buy_pct(prev_6)

                if pct_1h > pct_prev + 0.04:
                    trend = "rising"
                elif pct_1h < pct_prev - 0.04:
                    trend = "falling"
                else:
                    trend = "flat"

                if symbol not in self._metrics:
                    self._metrics[symbol] = SymbolMetrics()
                m = self._metrics[symbol]
                m.taker_buy_pct_1h  = round(pct_1h,  4)
                m.taker_buy_pct_30m = round(pct_30m, 4)
                m.taker_buy_trend   = trend
                m.taker_updated     = time.time()
                return ("taker", True)
        except Exception:
            return ("taker", None)

    async def _fetch_global_ls(self, sess: aiohttp.ClientSession, symbol: str) -> tuple:
        try:
            async with sess.get(
                f"{_BNFUT}/futures/data/globalLongShortAccountRatio",
                params={"symbol": symbol, "period": "5m", "limit": 1},
            ) as resp:
                if resp.status != 200:
                    return ("ls", None)
                data = await resp.json()
                if not isinstance(data, list) or not data:
                    return ("ls", None)
                row = data[-1]
                retail_long = float(row.get("longAccount", 0.5) or 0.5)
                if symbol not in self._metrics:
                    self._metrics[symbol] = SymbolMetrics()
                m = self._metrics[symbol]
                m.retail_long_pct = round(retail_long, 4)
                m.ls_divergence   = round(m.retail_long_pct - m.smart_long_pct, 4)
                return ("ls", True)
        except Exception:
            return ("ls", None)

    # ── 基差 ─────────────────────────────────────────────────────────────

    async def _batch_basis(self, sess: aiohttp.ClientSession) -> None:
        ok = 0
        for i in range(0, len(self._top_symbols), _BATCH_SIZE):
            batch = self._top_symbols[i : i + _BATCH_SIZE]
            tasks = [self._fetch_basis(sess, sym) for sym in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if r:
                    ok += 1
            if i + _BATCH_SIZE < len(self._top_symbols):
                await asyncio.sleep(_BATCH_SLEEP)
        logger.debug("📡 Basis更新%d个", ok)

    async def _fetch_basis(self, sess: aiohttp.ClientSession, symbol: str) -> bool:
        try:
            async with sess.get(
                f"{_BNFUT}/futures/data/basis",
                params={"symbol": symbol, "contractType": "PERPETUAL",
                        "period": "5m", "limit": 1},
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                if not isinstance(data, list) or not data:
                    return False
                row = data[-1]
                fut   = float(row.get("futuresPrice") or 0)
                idx   = float(row.get("indexPrice")   or 0)
                if idx <= 0:
                    return False
                basis_pct = (fut - idx) / idx * 100
                if symbol not in self._metrics:
                    self._metrics[symbol] = SymbolMetrics()
                self._metrics[symbol].basis_pct    = round(basis_pct, 4)
                self._metrics[symbol].basis_updated = time.time()
                return True
        except Exception:
            return False

    # ── OKX OI (跨所对比) ────────────────────────────────────────────────

    async def _fetch_okx_oi(self, sess: aiohttp.ClientSession) -> None:
        try:
            async with sess.get(
                f"{_OKXPUB}/api/v5/market/open-interest",
                params={"instType": "SWAP"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return
                body  = await resp.json()
                items = body.get("data", [])
                count = 0
                for item in items:
                    inst = item.get("instId", "")
                    if not inst.endswith("-USDT-SWAP"):
                        continue
                    oi_usd = float(item.get("oiUsd") or 0)
                    self._okx_oi[inst] = oi_usd
                    count += 1
                # Update concentration for all metrics
                for sym in list(self._metrics.keys()):
                    self._calc_oi_concentration(sym)
                logger.debug("📡 OKX OI更新%d个合约", count)
        except Exception as e:
            logger.debug("📡 OKX OI异常: %s", e)

    def _calc_oi_concentration(self, symbol: str) -> None:
        """计算币安OI在两所合计中的占比"""
        if symbol not in self._metrics:
            return
        base    = symbol.replace("USDT", "")
        okx_key = f"{base}-USDT-SWAP"
        m       = self._metrics[symbol]
        total   = m.bnc_oi_usdt + self._okx_oi.get(okx_key, 0)
        m.okx_oi_usdt      = self._okx_oi.get(okx_key, 0)
        m.oi_concentration = round(m.bnc_oi_usdt / total, 4) if total > 0 else 0.0

    # ── 辅助 ─────────────────────────────────────────────────────────────

    async def _fetch_top_symbols(self, sess: aiohttp.ClientSession) -> list[str]:
        try:
            async with sess.get(
                f"{_BNFUT}/fapi/v1/ticker/24hr",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    usdt = [
                        t for t in data
                        if isinstance(t, dict)
                        and t.get("symbol", "").endswith("USDT")
                        and "_" not in t.get("symbol", "")
                    ]
                    usdt.sort(key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)
                    syms = [t["symbol"] for t in usdt[:_TOP_N]]
                    logger.info("📡 MarketHub 监控 %d 个合约", len(syms))
                    return syms
        except Exception as e:
            logger.warning("📡 获取合约列表失败: %s", e)
        return []

    def stats(self) -> dict:
        """返回缓存统计 (给web.py用)"""
        fresh = sum(1 for m in self._metrics.values() if m.is_taker_fresh())
        return {
            "total":       len(self._metrics),
            "fresh_taker": fresh,
            "top_n":       len(self._top_symbols),
            "running":     self.running,
        }


# ── 全局单例 ─────────────────────────────────────────────────────────────────
hub = MarketHub()
