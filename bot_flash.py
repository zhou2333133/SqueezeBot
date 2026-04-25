"""
bot_flash.py — V4AF 闪崩做空模块

策略：
    妖币暴涨后回落、4H 多头衰竭、1H lower high 确认 → 做空。
    出场：trail / 宽硬 SL / 智能时间止损（每 N 小时重评估，符合维持条件就延期）。

公开接口（其他模块 / web / tests 用这些）：
    FlashCrashBot()                       构造（单实例，bot_state.flash_bot 持有）
    bot.run()                             主循环（asyncio）
    bot.detect_entry(symbol, candidate)   纯函数：评估单币是否入场（返回 dict 或 None）
    bot.evaluate_continuation(position)   纯函数：到期重评估，返回 dict {keep, reason, ...}
    bot.open_paper_positions              {symbol: FlashPosition}
    bot.snapshot_status()                 实时状态（给 web 复盘 / UI）

设计要点：
    - 完全独立于 bot_scalp，不共用 position / signal / time_stop 逻辑
    - 共享 candidates_map（妖币扫描器输出）+ klines_1h/4h（K 线缓存）
    - 独立账户：trader 用 BINANCE_FLASH_API_KEY/SECRET，留空则回退主账户
    - 智能时间止损：到 FLASH_REVIEW_HOURS 时跑维持条件检查，通过就延 FLASH_REVIEW_EXTEND_HOURS
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

import bot_state
from config import (
    BINANCE_FLASH_API_KEY, BINANCE_FLASH_API_SECRET,
    config_manager,
)
from scanner.candidates import candidates_map
from scanner.sources.binance_klines import klines_1h, klines_4h
from scanner.sources.token_supply import refresh_listing_dates, enrich_candidate
from signals import (
    add_flash_signal, set_flash_position, add_flash_trade, add_flash_review,
    flash_positions, flash_filter_stats,
)
from trader import BinanceTrader

logger = logging.getLogger("bot_flash")


# ── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class FlashPosition:
    symbol:           str
    direction:        str               # 始终 "SHORT"（V4AF 是单向做空）
    entry_price:      float
    quantity:         float
    sl_price:         float              # 宽硬止损
    trail_pct:        float              # 浮盈达 activation 后的回调出场距离
    trail_activation_pct: float
    peak_price:       float = 0.0        # 入场前发现的 24h 高点（用于维持条件判定）
    paper:            bool  = True
    entry_time:       str   = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    entry_ts:         float = field(default_factory=time.time)
    next_review_ts:   float = 0.0        # 下次重评估时间（unix）
    review_count:     int   = 0
    extension_count:  int   = 0
    max_favorable_pct: float = 0.0
    max_adverse_pct:  float = 0.0
    trail_active:     bool  = False
    best_price:       float = 0.0        # 做空时是迄今最低价
    realized_pnl:     float = 0.0
    sl_order_id:      int | None = None
    risk_usdt:        float = 0.0
    setup_meta:       dict = field(default_factory=dict)   # 入场时三条件的具体值，用于复盘

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "sl_price": self.sl_price,
            "trail_pct": self.trail_pct,
            "trail_activation_pct": self.trail_activation_pct,
            "peak_price": self.peak_price,
            "paper": self.paper,
            "entry_time": self.entry_time,
            "entry_ts": self.entry_ts,
            "next_review_ts": self.next_review_ts,
            "next_review_at": (
                datetime.fromtimestamp(self.next_review_ts).strftime("%Y-%m-%d %H:%M:%S")
                if self.next_review_ts else ""
            ),
            "review_count": self.review_count,
            "extension_count": self.extension_count,
            "max_favorable_pct": self.max_favorable_pct,
            "max_adverse_pct": self.max_adverse_pct,
            "trail_active": self.trail_active,
            "best_price": self.best_price,
            "realized_pnl": self.realized_pnl,
            "setup_meta": self.setup_meta,
        }


# ── 入场条件检测（纯函数，可单测）─────────────────────────────────────────

def detect_4h_exhaustion(klines_4h_rows: list[dict]) -> tuple[bool, dict]:
    """
    4H 多头衰竭：
        1) 当前 4H K 线 收盘 < 开盘 (4H 阴)
        2) 4H K 线 taker_buy_qv / qv < 0.5（瞬时卖压 > 买压）
        3) 上一根 4H 也是阳线或刚收阴（趋势刚转）
    """
    if len(klines_4h_rows) < 2:
        return False, {"reason": "kline_insufficient"}
    cur = klines_4h_rows[-1]
    prev = klines_4h_rows[-2]
    qv = cur.get("qv", 0.0)
    taker_qv = cur.get("taker_buy_qv", 0.0)
    taker_buy_pct = (taker_qv / qv) if qv > 0 else 0.5
    bearish = cur["c"] < cur["o"]
    sell_dominant = taker_buy_pct < 0.50
    prev_bullish = prev["c"] >= prev["o"]
    ok = bearish and sell_dominant and prev_bullish
    return ok, {
        "taker_buy_pct_4h": round(taker_buy_pct, 4),
        "bearish_4h": bearish,
        "prev_bullish_4h": prev_bullish,
        "cur_close": cur["c"],
        "prev_close": prev["c"],
    }


def detect_1h_lower_high(
    rows_1h: list[dict],
    lookback: int = 8,
    min_drop_pct: float = 0.5,
) -> tuple[bool, dict]:
    """
    1H lower high 确认：
        在 lookback 根已收盘 1H 内找最高 high 的 K 线（peak），
        peak 之后存在一根 1H 收盘 high 比 peak high 低至少 min_drop_pct%。
    返回 (是否确认, 详情 dict)。
    """
    if len(rows_1h) < lookback:
        return False, {"reason": "kline_insufficient", "have": len(rows_1h)}

    window = rows_1h[-lookback:]
    peak_idx = max(range(len(window)), key=lambda i: window[i]["h"])
    peak_high = window[peak_idx]["h"]
    if peak_idx >= len(window) - 1:
        return False, {"reason": "peak_is_latest", "peak_high": peak_high}

    after = window[peak_idx + 1:]
    confirmed = False
    confirm_idx = -1
    for j, row in enumerate(after):
        # peak 是窗口内绝对最高点，所以此处不会出现等高/新高（保险起见仍然兜底）
        if row["h"] >= peak_high:
            continue
        drop_pct = (peak_high - row["h"]) / peak_high * 100.0
        if drop_pct >= min_drop_pct:
            confirmed = True
            confirm_idx = j
            break

    if not confirmed:
        return False, {
            "reason": "drop_too_small",
            "peak_high": peak_high,
            "max_drop_pct": (peak_high - max(r["h"] for r in after)) / peak_high * 100.0,
        }
    return True, {
        "peak_high": peak_high,
        "confirm_low": after[confirm_idx]["h"],
        "drop_pct": round((peak_high - after[confirm_idx]["h"]) / peak_high * 100.0, 4),
        "bars_after_peak": confirm_idx + 1,
    }


# ── FlashCrashBot 主体 ────────────────────────────────────────────────────

class FlashCrashBot:
    def __init__(self):
        self._stop_event = asyncio.Event()
        self.open_positions: dict[str, FlashPosition] = {}
        self._candidate_symbols: list[str] = []
        self._last_scan_at: float = 0.0
        self._last_kline_refresh_at: float = 0.0
        self._fstat: dict = {
            "scans": 0, "checked": 0,
            "blocked_no_kline": 0, "blocked_no_24h_gain": 0,
            "blocked_no_4h_exhaustion": 0, "blocked_no_lower_high": 0,
            "blocked_not_eligible": 0, "blocked_already_open": 0,
            "blocked_max_positions": 0, "blocked_low_volume": 0,
            "signals_emitted": 0, "entries_taken": 0,
        }
        flash_filter_stats.update(self._fstat)

    # ── 公开运行接口 ──────────────────────────────────────────────────────

    @property
    def cfg(self) -> dict:
        return config_manager.settings

    def stop(self) -> None:
        self._stop_event.set()

    def _make_trader(self, session: aiohttp.ClientSession) -> BinanceTrader:
        """返回 V4AF 专用 trader：优先 FLASH key，留空回退主账户。"""
        if BINANCE_FLASH_API_KEY and BINANCE_FLASH_API_SECRET:
            return BinanceTrader(
                session,
                api_key=BINANCE_FLASH_API_KEY,
                api_secret=BINANCE_FLASH_API_SECRET,
                label="flash",
            )
        return BinanceTrader(session, label="flash_fallback_main")

    async def run(self) -> None:
        logger.info("⚡ V4AF FlashCrashBot 启动")
        async with aiohttp.ClientSession(trust_env=True) as session:
            # 启动时拉一次 listing dates
            try:
                await refresh_listing_dates(session, force=True)
            except Exception as e:
                logger.warning("listing dates refresh failed: %s", e)

            kline_task = asyncio.create_task(self._kline_refresh_loop(session))
            try:
                while not self._stop_event.is_set():
                    try:
                        await self._scan_once(session)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error("FlashCrashBot _scan_once 异常: %s", e, exc_info=True)
                    interval = int(self.cfg.get("FLASH_SCAN_INTERVAL_SECONDS", 60) or 60)
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                    except asyncio.TimeoutError:
                        pass
            finally:
                kline_task.cancel()
                try:
                    await kline_task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("⚡ V4AF FlashCrashBot 退出")

    # ── 内部循环 ──────────────────────────────────────────────────────────

    async def _kline_refresh_loop(self, session: aiohttp.ClientSession) -> None:
        interval = int(self.cfg.get("FLASH_KLINE_REFRESH_SECONDS", 300) or 300)
        # 1h 和 4h 错峰
        await asyncio.gather(
            klines_1h.run_loop(session, interval),
            klines_4h.run_loop(session, interval),
        )

    async def _scan_once(self, session: aiohttp.ClientSession) -> None:
        self._fstat["scans"] += 1
        self._last_scan_at = time.time()

        # 1) 同步候选池 → 给 K 线 cache 设置关注列表
        symbols = await self._refresh_candidate_universe(session)
        if symbols:
            klines_1h.update_symbols(symbols)
            klines_4h.update_symbols(symbols)

        # 2) 更新已有持仓 P&L、检测 SL / Trail
        for symbol in list(self.open_positions.keys()):
            closed = await self._update_position(symbol, session)
            if closed:
                continue
            # 未触发 SL/trail 的，再看是否到了智能时间止损审核点
            await self._maybe_review_position(symbol, session)

        # 3) 检测新入场机会
        for symbol in symbols:
            self._fstat["checked"] += 1
            if symbol in self.open_positions:
                self._fstat["blocked_already_open"] += 1
                continue
            if len(self.open_positions) >= int(self.cfg.get("FLASH_MAX_POSITIONS", 3) or 3):
                self._fstat["blocked_max_positions"] += 1
                break
            candidate = candidates_map.get(symbol) or {}
            decision = self.detect_entry(symbol, candidate)
            if decision is None:
                continue
            self._fstat["signals_emitted"] += 1
            add_flash_signal({
                "symbol": symbol,
                "direction": "SHORT",
                "kind": "V4AF_FLASH_SHORT",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                **decision,
            })
            await self._open_position(symbol, candidate, decision, session)

        flash_filter_stats.update(self._fstat)

    async def _refresh_candidate_universe(self, session: aiohttp.ClientSession) -> list[str]:
        # 候选池 = 所有币安 USDT 永续合约里、enrich 后 flash_eligible=True 的
        # 没有 yaobi 的我们也跑（V4AF 是独立选币逻辑）
        try:
            await refresh_listing_dates(session)
        except Exception:
            pass
        cfg = self.cfg
        eligible: list[str] = []
        for c in candidates_map.values():
            symbol = (c.get("symbol") or "").upper()
            if not symbol or not symbol.endswith("USDT"):
                continue
            enrich_candidate(c, cfg)
            if not c.get("flash_eligible"):
                if c.get("flash_ban_reason") == "low_volume":
                    self._fstat["blocked_low_volume"] += 1
                continue
            eligible.append(symbol)
        # 限制扫描宽度，避免 220 个币 × K 线刷得过狠
        max_scan = 80
        eligible = eligible[:max_scan]
        self._candidate_symbols = eligible
        return eligible

    # ── 入场逻辑（纯函数）──────────────────────────────────────────────────

    def detect_entry(self, symbol: str, candidate: dict) -> dict | None:
        """
        返回 None = 不入场；返回 dict = 入场参数（peak_price / 三条件细节）。
        """
        cfg = self.cfg

        # 24H 涨幅 (V4AF 入场条件 1)
        gain_pct = float(candidate.get("price_change_24h", 0.0) or 0.0)
        if gain_pct < float(cfg.get("FLASH_24H_GAIN_MIN_PCT", 15.0)):
            self._fstat["blocked_no_24h_gain"] += 1
            return None

        # vesting 选币 (V4AF 标的池)
        if cfg.get("FLASH_REQUIRE_VESTING_GROUP", True) and not candidate.get("flash_eligible", False):
            self._fstat["blocked_not_eligible"] += 1
            return None

        rows_1h = klines_1h.closed_only(symbol, n=int(cfg.get("FLASH_1H_LOWER_HIGH_LOOKBACK", 8)) + 4)
        rows_4h = klines_4h.get(symbol, n=4)
        if not rows_1h or not rows_4h:
            self._fstat["blocked_no_kline"] += 1
            return None

        # 4H 多头衰竭 (V4AF 入场条件 2)
        ok_4h, detail_4h = detect_4h_exhaustion(rows_4h)
        if not ok_4h:
            self._fstat["blocked_no_4h_exhaustion"] += 1
            return None

        # 1H lower high (V4AF 入场条件 3)
        ok_1h, detail_1h = detect_1h_lower_high(
            rows_1h,
            lookback=int(cfg.get("FLASH_1H_LOWER_HIGH_LOOKBACK", 8)),
            min_drop_pct=float(cfg.get("FLASH_1H_LOWER_HIGH_MIN_DROP_PCT", 0.5)),
        )
        if not ok_1h:
            self._fstat["blocked_no_lower_high"] += 1
            return None

        return {
            "gain_24h_pct": gain_pct,
            "peak_price": detail_1h.get("peak_high", 0.0),
            "lower_high_drop_pct": detail_1h.get("drop_pct", 0.0),
            "exhaustion_4h": detail_4h,
            "lower_high_1h": detail_1h,
            "vesting_phase": candidate.get("vesting_phase", ""),
            "listing_age_days": candidate.get("listing_age_days", 0),
            "volume_24h": candidate.get("volume_24h", 0.0),
        }

    # ── 开仓 ──────────────────────────────────────────────────────────────

    async def _open_position(
        self,
        symbol: str,
        candidate: dict,
        decision: dict,
        session: aiohttp.ClientSession,
    ) -> None:
        cfg = self.cfg
        paper = bool(cfg.get("FLASH_PAPER_TRADE", True))
        auto_trade = bool(cfg.get("FLASH_AUTO_TRADE", False))
        if not paper and not auto_trade:
            return
        # 当前价
        latest_1h = klines_1h.peek(symbol)
        if not latest_1h:
            return
        entry_price = float(latest_1h["c"])
        if entry_price <= 0:
            return

        position_usdt = float(cfg.get("FLASH_POSITION_USDT", 100.0))
        leverage = int(cfg.get("FLASH_LEVERAGE", 5) or 5)
        notional = position_usdt * leverage
        quantity = notional / entry_price
        sl_pct = float(cfg.get("FLASH_SL_PCT", 4.0))
        sl_price = entry_price * (1.0 + sl_pct / 100.0)   # 做空 → SL 在上方

        review_hours = int(cfg.get("FLASH_REVIEW_HOURS", 8) or 8)
        next_review_ts = time.time() + review_hours * 3600

        pos = FlashPosition(
            symbol=symbol,
            direction="SHORT",
            entry_price=entry_price,
            quantity=quantity,
            sl_price=sl_price,
            trail_pct=float(cfg.get("FLASH_TRAIL_PCT", 1.5)),
            trail_activation_pct=float(cfg.get("FLASH_TRAIL_ACTIVATION_PCT", 1.0)),
            peak_price=float(decision.get("peak_price", 0.0)),
            paper=paper or not auto_trade,
            next_review_ts=next_review_ts,
            best_price=entry_price,
            risk_usdt=position_usdt * sl_pct / 100.0,
            setup_meta=decision,
        )

        # 实盘下单
        if auto_trade and not paper:
            trader = self._make_trader(session)
            await trader.set_leverage(symbol, leverage)
            order = await trader.place_market_order(symbol, "SELL", quantity)
            if not order or not order.get("orderId"):
                logger.warning("⚡ FLASH 实盘开仓失败 %s，回退 paper", symbol)
                pos.paper = True
            else:
                sl = await trader.place_stop_loss_order(symbol, "BUY", sl_price)
                if sl and sl.get("orderId"):
                    pos.sl_order_id = sl["orderId"]
                else:
                    logger.error("⚡ FLASH SL 挂单失败 %s，强制平仓避免裸仓", symbol)
                    await trader.place_reduce_only_market_order(symbol, "BUY", quantity)
                    return

        self.open_positions[symbol] = pos
        set_flash_position(symbol, pos.to_dict())
        self._fstat["entries_taken"] += 1
        logger.info(
            "⚡ FLASH 开空 %s @ %.6f SL=%.6f peak=%.6f paper=%s 24h=%.1f%%",
            symbol, entry_price, sl_price, pos.peak_price, pos.paper, decision["gain_24h_pct"]
        )

    # ── 持仓 P&L / SL / Trail 更新 ────────────────────────────────────────

    async def _update_position(
        self, symbol: str, session: aiohttp.ClientSession
    ) -> bool:
        """
        每次扫描更新一次持仓状态。
        返回 True = 已平仓（调用方应跳过后续审核）。

        做空逻辑：
            cur_price < entry_price → 浮盈
            cur_price > entry_price → 浮亏
            best_price = 迄今最低价（trail 参考点）
        """
        pos = self.open_positions.get(symbol)
        if not pos:
            return False
        latest = klines_1h.peek(symbol)
        if not latest:
            return False
        cur = float(latest["c"])
        if cur <= 0:
            return False

        favorable_pct = (pos.entry_price - cur) / pos.entry_price * 100.0
        adverse_pct = (cur - pos.entry_price) / pos.entry_price * 100.0

        if favorable_pct > pos.max_favorable_pct:
            pos.max_favorable_pct = favorable_pct
        if adverse_pct > pos.max_adverse_pct:
            pos.max_adverse_pct = adverse_pct
        if cur < pos.best_price or pos.best_price == 0:
            pos.best_price = cur

        # 1) 硬 SL 触发（做空：cur >= sl_price）
        if cur >= pos.sl_price:
            await self._close_position(symbol, "SL", f"cur={cur:.6f}>=sl={pos.sl_price:.6f}", session)
            return True

        # 2) Trail 启用 + 反弹出场
        if not pos.trail_active and favorable_pct >= pos.trail_activation_pct:
            pos.trail_active = True
            logger.info("⚡ FLASH %s trail 激活 best=%.6f favorable=%.2f%%", symbol, pos.best_price, favorable_pct)

        if pos.trail_active and pos.best_price > 0:
            rebound_pct = (cur - pos.best_price) / pos.best_price * 100.0
            if rebound_pct >= pos.trail_pct:
                await self._close_position(
                    symbol, "TRAIL",
                    f"rebound {rebound_pct:.2f}% from best {pos.best_price:.6f}",
                    session,
                )
                return True

        # 3) 状态写回 web 共享
        set_flash_position(symbol, pos.to_dict())
        return False

    # ── 智能时间止损：到期重评估 ────────────────────────────────────────

    async def _maybe_review_position(self, symbol: str, session: aiohttp.ClientSession) -> None:
        pos = self.open_positions.get(symbol)
        if not pos:
            return
        now = time.time()
        if now < pos.next_review_ts:
            return
        decision = self.evaluate_continuation(pos)
        review_record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "review_count": pos.review_count + 1,
            "elapsed_minutes": round((now - pos.entry_ts) / 60.0, 1),
            **decision,
        }
        add_flash_review(review_record)
        pos.review_count += 1

        if decision["keep"]:
            extend_hours = int(self.cfg.get("FLASH_REVIEW_EXTEND_HOURS", 4) or 4)
            pos.next_review_ts = now + extend_hours * 3600
            pos.extension_count += 1
            logger.info(
                "⚡ FLASH 持仓续期 %s reason=%s 已延 %d 次，下次 %sh 后再审",
                symbol, decision["reason"], pos.extension_count, extend_hours,
            )
            set_flash_position(symbol, pos.to_dict())
        else:
            logger.info(
                "⚡ FLASH 持仓平仓（智能时间止损）%s reason=%s",
                symbol, decision["reason"],
            )
            await self._close_position(symbol, "smart_time_stop", decision["reason"], session)

    def evaluate_continuation(self, pos: FlashPosition) -> dict:
        """
        到期重评估：维持条件检查（弱于入场条件）。

        维持条件全部满足 → 继续持有
            (a) 当前价 < peak_price（趋势仍在 peak 下方）
            (b) 最近 1H 没出现 higher high（与入场时 peak 比，没被刷穿）
            (c) 浮亏不超过 FLASH_REVIEW_HOLD_LOSS_MAX_PCT
            (d) 重评估次数未超过 FLASH_REVIEW_MAX_EXTENSIONS
        否则 → 平仓
        """
        cfg = self.cfg
        max_ext = int(cfg.get("FLASH_REVIEW_MAX_EXTENSIONS", 2) or 2)
        loss_max = float(cfg.get("FLASH_REVIEW_HOLD_LOSS_MAX_PCT", 1.0))

        if pos.extension_count >= max_ext:
            return {"keep": False, "reason": "max_extensions_reached"}

        latest = klines_1h.peek(pos.symbol)
        if not latest:
            return {"keep": False, "reason": "no_kline_data"}
        cur_price = float(latest["c"])
        rows_1h = klines_1h.closed_only(pos.symbol, n=8)

        # (a) 仍在 peak 下方
        if pos.peak_price > 0 and cur_price >= pos.peak_price:
            return {"keep": False, "reason": "price_back_above_peak", "cur": cur_price, "peak": pos.peak_price}

        # (b) 最近 1H 没刷出新 higher high（高于入场前 peak）
        recent_high = max((r["h"] for r in rows_1h[-4:]), default=0.0) if rows_1h else 0.0
        if pos.peak_price > 0 and recent_high >= pos.peak_price:
            return {"keep": False, "reason": "new_higher_high_1h", "recent_high": recent_high, "peak": pos.peak_price}

        # (c) 浮亏检查（做空：当前价 > 入场价 = 浮亏）
        if pos.entry_price > 0:
            adverse_pct = (cur_price - pos.entry_price) / pos.entry_price * 100.0
            if adverse_pct > loss_max:
                return {"keep": False, "reason": "loss_exceeds_threshold", "adverse_pct": round(adverse_pct, 3)}

        # 加分项：4H 卖压仍占优
        rows_4h = klines_4h.get(pos.symbol, n=2)
        taker_buy_pct = 0.5
        if rows_4h:
            cur_4h = rows_4h[-1]
            qv = cur_4h.get("qv", 0.0)
            taker_qv = cur_4h.get("taker_buy_qv", 0.0)
            taker_buy_pct = (taker_qv / qv) if qv > 0 else 0.5

        return {
            "keep": True,
            "reason": "trend_intact",
            "cur": cur_price,
            "favorable_pct": round((pos.entry_price - cur_price) / pos.entry_price * 100.0, 3) if pos.entry_price else 0.0,
            "taker_buy_pct_4h": round(taker_buy_pct, 4),
        }

    # ── 平仓 ──────────────────────────────────────────────────────────────

    async def _close_position(
        self,
        symbol: str,
        close_reason: str,
        detail: str,
        session: aiohttp.ClientSession,
    ) -> None:
        pos = self.open_positions.pop(symbol, None)
        if not pos:
            return

        latest = klines_1h.peek(symbol)
        exit_price = float(latest["c"]) if latest else pos.entry_price

        if not pos.paper:
            try:
                trader = self._make_trader(session)
                if pos.sl_order_id:
                    await trader.cancel_order(symbol, pos.sl_order_id)
                await trader.place_reduce_only_market_order(symbol, "BUY", pos.quantity)
            except Exception as e:
                logger.error("⚡ FLASH 实盘平仓异常 %s: %s", symbol, e)

        # 做空 P&L
        pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100.0 if pos.entry_price else 0.0
        cfg = self.cfg
        leverage = int(cfg.get("FLASH_LEVERAGE", 5) or 5)
        position_usdt = float(cfg.get("FLASH_POSITION_USDT", 100.0))
        pnl_usdt = position_usdt * leverage * pnl_pct / 100.0

        trade = {
            "symbol": symbol,
            "direction": "SHORT",
            "kind": "V4AF_FLASH_SHORT",
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "quantity": pos.quantity,
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usdt": round(pnl_usdt, 4),
            "entry_time": pos.entry_time,
            "exit_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_minutes": round((time.time() - pos.entry_ts) / 60.0, 1),
            "close_reason": close_reason,
            "close_detail": detail,
            "review_count": pos.review_count,
            "extension_count": pos.extension_count,
            "max_favorable_pct": pos.max_favorable_pct,
            "max_adverse_pct": pos.max_adverse_pct,
            "paper": pos.paper,
            "setup_meta": pos.setup_meta,
            "vesting_phase": pos.setup_meta.get("vesting_phase", ""),
        }
        add_flash_trade(trade)
        set_flash_position(symbol, None)
        logger.info(
            "⚡ FLASH 平仓 %s reason=%s pnl=%.2fU (%.2f%%) dur=%.0fmin",
            symbol, close_reason, pnl_usdt, pnl_pct, trade["duration_minutes"],
        )

    # ── 状态快照（给 web 复盘用）──────────────────────────────────────────

    def snapshot_status(self) -> dict:
        return {
            "running": True,
            "candidate_symbols": list(self._candidate_symbols),
            "open_positions": len(self.open_positions),
            "filter_stats": dict(self._fstat),
            "last_scan_at": self._last_scan_at,
            "klines_1h_status": klines_1h.status(),
            "klines_4h_status": klines_4h.status(),
        }
