"""
scanner/sources/binance_klines.py — 1H / 4H K 线共享缓存

公开接口（其他模块用这些，不要直接动内部 _kline_cache）：
    KlineCache(interval: str)            # interval: "1h" / "4h"
    cache.update_symbols(symbols: list)  # 设置关注列表（覆盖式）
    cache.get(symbol: str, n: int = 50)  # 拿最近 N 根，返回 list[dict]：
                                         #   {"open_time", "o", "h", "l", "c", "v", "qv", "trades", "is_closed"}
    cache.peek(symbol: str)              # 拿最新一根（live 实时收盘前）
    await cache.refresh_once(session)    # 主动触发一次拉取，立刻刷新所有关注币
    await cache.run_loop(session, interval_seconds: int)
                                         # 后台轮询循环（每 N 秒刷一次）

模块单例：
    klines_1h, klines_4h                 # 全局共享（bot_flash + 任何 scanner 都可读）

数据特性：
    - 完全内存，重启失数据
    - 拉取来源：Binance Futures /fapi/v1/klines（公共接口，无需 API key）
    - 限速：每币 weight 1/请求；220 币 × 2 间隔 / 5 min ≈ 90/min（远低于 2400/min）
    - 每次刷新拉最近 50 根 + 当前未收盘根（is_closed=False）
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_BINANCE_FAPI = "https://fapi.binance.com"
_DEFAULT_LIMIT = 60     # 多拉一点，防止 lower-high 回看不够
_BATCH_SIZE = 10        # 并发批大小（限速友好）
_BATCH_SLEEP = 0.25


def _row_from_binance(row: list) -> dict:
    """Binance kline 行 → 标准化字典。

    Binance 返回:
        [open_time, open, high, low, close, volume,
         close_time, quote_volume, trades, taker_buy_vol, taker_buy_quote_vol, ignore]
    """
    return {
        "open_time": int(row[0]),
        "o":      float(row[1]),
        "h":      float(row[2]),
        "l":      float(row[3]),
        "c":      float(row[4]),
        "v":      float(row[5]),
        "close_time": int(row[6]),
        "qv":     float(row[7]),
        "trades": int(row[8]),
        "taker_buy_v":  float(row[9]),
        "taker_buy_qv": float(row[10]),
    }


class KlineCache:
    def __init__(self, interval: str, limit: int = _DEFAULT_LIMIT):
        if interval not in ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"):
            raise ValueError(f"Unsupported interval: {interval}")
        self.interval = interval
        self.limit = limit
        self.symbols: list[str] = []
        self._cache: dict[str, list[dict]] = {}     # symbol → 最近 N 根（含未收盘最后一根）
        self._last_refresh_at: float = 0.0
        self._last_error: dict[str, str] = {}        # symbol → 最近错误信息

    # ── 公开读接口 ────────────────────────────────────────────────────────────
    def get(self, symbol: str, n: int = 50) -> list[dict]:
        rows = self._cache.get(symbol.upper(), [])
        return rows[-n:] if n > 0 else list(rows)

    def peek(self, symbol: str) -> dict | None:
        rows = self._cache.get(symbol.upper())
        return rows[-1] if rows else None

    def closed_only(self, symbol: str, n: int = 50) -> list[dict]:
        """只返回已收盘的根（最后一根可能未收盘，根据 close_time 判断）。"""
        now_ms = int(time.time() * 1000)
        rows = [r for r in self._cache.get(symbol.upper(), []) if r["close_time"] < now_ms]
        return rows[-n:] if n > 0 else rows

    def has(self, symbol: str) -> bool:
        return symbol.upper() in self._cache

    def status(self) -> dict:
        return {
            "interval":  self.interval,
            "symbols":   len(self.symbols),
            "cached":    len(self._cache),
            "last_refresh_at": self._last_refresh_at,
            "errors":    dict(list(self._last_error.items())[:10]),
        }

    # ── 写接口 ────────────────────────────────────────────────────────────────
    def update_symbols(self, symbols: list[str]) -> None:
        normalized = sorted({s.upper() for s in symbols if s})
        self.symbols = normalized
        # 移除不再关注的缓存
        for sym in list(self._cache.keys()):
            if sym not in normalized:
                self._cache.pop(sym, None)

    async def refresh_once(self, session: aiohttp.ClientSession) -> int:
        """拉取所有关注币的最新 K 线，返回成功币数。"""
        if not self.symbols:
            return 0
        successes = 0
        for batch_start in range(0, len(self.symbols), _BATCH_SIZE):
            batch = self.symbols[batch_start: batch_start + _BATCH_SIZE]
            results = await asyncio.gather(
                *(self._fetch(session, sym) for sym in batch),
                return_exceptions=True,
            )
            for sym, result in zip(batch, results):
                if isinstance(result, Exception):
                    self._last_error[sym] = str(result)
                    continue
                if not result:
                    continue
                self._cache[sym] = result
                self._last_error.pop(sym, None)
                successes += 1
            await asyncio.sleep(_BATCH_SLEEP)
        self._last_refresh_at = time.time()
        return successes

    async def run_loop(self, session: aiohttp.ClientSession, interval_seconds: int) -> None:
        """后台循环；调用方负责 create_task 包装。"""
        while True:
            try:
                count = await self.refresh_once(session)
                if count == 0 and self.symbols:
                    logger.warning("KlineCache[%s] 全部刷新失败 (%d 个币)", self.interval, len(self.symbols))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("KlineCache[%s] run_loop 异常: %s", self.interval, e)
            await asyncio.sleep(max(15, interval_seconds))

    # ── 内部 ──────────────────────────────────────────────────────────────────
    async def _fetch(self, session: aiohttp.ClientSession, symbol: str) -> list[dict] | None:
        url = f"{_BINANCE_FAPI}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": self.interval, "limit": self.limit}
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(f"http {resp.status}: {body[:120]}")
                data = await resp.json()
        except Exception as e:
            raise RuntimeError(str(e)) from e
        if not isinstance(data, list) or not data:
            return None
        return [_row_from_binance(row) for row in data]


# ── 全局单例 ──────────────────────────────────────────────────────────────────
klines_1h = KlineCache("1h", limit=_DEFAULT_LIMIT)
klines_4h = KlineCache("4h", limit=_DEFAULT_LIMIT)
