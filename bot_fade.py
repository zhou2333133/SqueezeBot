"""
一直做空 (Fade the Pump) — 逆势短线机器人
策略: 15m 涨幅 >X% + 动量衰减 (高点不创新高) + BB 上轨触碰 + 量能萎缩 + MTF 确认 → 做空

扫描机制: 1m WebSocket (全币种) 做快速触发检测, 触发后 REST 拉取 15m/4H/1H 做精确验证.
关于 "1秒" 入场: 策略衰减信号需要数分钟积累, 1m 精度已足够; 若需更快入场, 可在信号发出后
手动进一步观察, 或调低 FADE_TRIGGER_PCT.
"""
from __future__ import annotations
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp

from config import config_manager
from signals import add_fade_signal, set_fade_position, add_fade_trade
from trader import BinanceTrader

logger = logging.getLogger("bot_fade")

_REST_BASE = "https://fapi.binance.com"
_WS_URL    = "wss://fstream.binance.com/ws"


@dataclass
class FadePosition:
    symbol:             str
    entry_price:        float
    quantity:           float
    quantity_remaining: float
    sl_price:           float
    tp1_price:          float
    tp2_price:          float
    tp1_hit:            bool       = False
    tp2_hit:            bool       = False
    paper:              bool       = False
    entry_time:         str        = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    sl_order_id:        int | None = None
    realized_pnl:       float      = 0.0
    current_price:      float      = 0.0

    def to_dict(self) -> dict:
        unreal = 0.0
        if self.current_price and self.entry_price:
            unreal = (self.entry_price - self.current_price) / self.entry_price * 100
        return {
            "symbol":             self.symbol,
            "direction":          "SHORT",
            "entry_price":        round(self.entry_price, 8),
            "quantity":           round(self.quantity, 6),
            "quantity_remaining": round(self.quantity_remaining, 6),
            "sl_price":           round(self.sl_price, 8),
            "tp1_price":          round(self.tp1_price, 8),
            "tp2_price":          round(self.tp2_price, 8),
            "tp1_hit":            self.tp1_hit,
            "tp2_hit":            self.tp2_hit,
            "paper":              self.paper,
            "entry_time":         self.entry_time,
            "realized_pnl":       round(self.realized_pnl, 4),
            "unrealized_pct":     round(unreal, 2),
            "current_price":      round(self.current_price, 8),
        }


class BinanceFadeBot:
    def __init__(self):
        self.open_positions:     dict[str, FadePosition] = {}
        self.kline_buffer:       dict[str, list]         = {}
        self.monitored_symbols:  list[str]               = []
        self.running:            bool                    = False
        self.session:            aiohttp.ClientSession | None = None
        self.trader:             BinanceTrader | None         = None
        self._checking:          set[str]                = set()
        self._last_signal_time:  dict[str, datetime]    = {}  # 冷却计时

    @property
    def cfg(self) -> dict:
        return config_manager.settings

    # ─── 启动 ──────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.running = True
        logger.info("📉 一直做空机器人启动")
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                self.session = session
                self.trader  = BinanceTrader(session)
                await self.refresh_symbols()
                await asyncio.gather(
                    self._ws_loop(),
                    self._position_monitor_loop(),
                    self._heartbeat_loop(),
                )
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            logger.info("📉 一直做空机器人已停止")

    async def refresh_symbols(self) -> None:
        custom = self.cfg.get("FADE_WATCHLIST", "").strip()
        if custom:
            self.monitored_symbols = [s.strip().upper() for s in custom.split(",") if s.strip()]
            logger.info("📉 自定义监控列表: %d 个币种", len(self.monitored_symbols))
            return
        try:
            async with self.session.get(
                f"{_REST_BASE}/fapi/v1/exchangeInfo",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.monitored_symbols = [
                        s["symbol"] for s in data.get("symbols", [])
                        if s["quoteAsset"]    == "USDT"
                        and s["status"]       == "TRADING"
                        and s["contractType"] == "PERPETUAL"
                    ]
                    logger.info("📉 自动检测: %d 个 USDT 永续合约", len(self.monitored_symbols))
        except Exception as e:
            logger.error("📉 获取币种列表失败: %s，使用兜底列表", e)
            self.monitored_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

    # ─── WebSocket ─────────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        backoff = 1
        while self.running:
            try:
                await self._ws_connect()
                backoff = 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.running:
                    logger.warning("📉 WS 断线: %s，%ds 后重连...", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def _ws_connect(self) -> None:
        async with self.session.ws_connect(_WS_URL, heartbeat=20) as ws:
            params = [f"{s.lower()}@kline_1m" for s in self.monitored_symbols]
            for i, chunk in enumerate([params[j:j + 200] for j in range(0, len(params), 200)]):
                await ws.send_str(json.dumps({"method": "SUBSCRIBE", "params": chunk, "id": i + 1}))
                await asyncio.sleep(0.3)
            logger.info("📉 WS 已连接，监控 %d 个币种", len(self.monitored_symbols))
            close_code = None
            async for msg in ws:
                if not self.running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._on_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    close_code = ws.close_code
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
            if self.running:
                logger.warning("📉 WS 连接关闭 (code=%s)，即将重连...", close_code)

    async def _on_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        stream_data = data.get("data", data)
        if stream_data.get("e") != "kline":
            return

        k           = stream_data["k"]
        symbol      = k["s"]
        is_closed   = k["x"]
        close_price = float(k["c"])

        if symbol in self.open_positions and not is_closed:
            self.open_positions[symbol].current_price = close_price
            set_fade_position(symbol, self.open_positions[symbol].to_dict())
            await self._check_tp_sl(symbol, close_price)

        if is_closed:
            buf = self.kline_buffer.setdefault(symbol, [])
            buf.append({
                "o": float(k["o"]),
                "h": float(k["h"]),
                "l": float(k["l"]),
                "c": float(k["c"]),
                "q": float(k["q"]),
            })
            if len(buf) > 60:
                buf.pop(0)
            if symbol not in self._checking:
                asyncio.create_task(self._check_wrapper(symbol))

    # ─── 辅助：BTC 方向过滤 ────────────────────────────────────────────────────

    def _btc_guard_short(self) -> bool:
        """做空前确认 BTC 没有在急涨（急涨时市场偏多，做空风险高）"""
        guard_pct = self.cfg.get("BTC_GUARD_PCT", 2.0)
        btc_buf   = self.kline_buffer.get("BTCUSDT", [])
        if len(btc_buf) < 5:
            return True
        btc_5m = (btc_buf[-1]["c"] - btc_buf[-5]["o"]) / btc_buf[-5]["o"] * 100
        if btc_5m > guard_pct:
            logger.debug("📉 BTC急拉 +%.2f%%，暂缓做空信号", btc_5m)
            return False
        return True

    # ─── 信号检测 ──────────────────────────────────────────────────────────────

    async def _check_wrapper(self, symbol: str) -> None:
        if symbol in self._checking:
            return
        self._checking.add(symbol)
        try:
            await self._check_fade_signal(symbol)
        except Exception as e:
            logger.debug("📉 [%s] 检测异常: %s", symbol, e)
        finally:
            self._checking.discard(symbol)

    async def _check_fade_signal(self, symbol: str) -> None:
        cfg = self.cfg
        if not cfg.get("FADE_ENABLED", False):
            return
        if symbol in self.open_positions:
            return
        if len(self.open_positions) >= cfg.get("FADE_MAX_POSITIONS", 3):
            return

        # ── 冷却期：同一币种 N 分钟内不重复触发 ────────────────────────────
        cooldown = int(cfg.get("FADE_COOLDOWN_MINUTES", 30)) * 60
        last = self._last_signal_time.get(symbol)
        if last and (datetime.now() - last).total_seconds() < cooldown:
            return

        buf    = self.kline_buffer.get(symbol, [])
        window = 15
        if len(buf) < window:
            return

        # ── 快速 15m 涨幅检测 ──────────────────────────────────────────────
        recent_15 = buf[-window:]
        op = recent_15[0]["o"]
        cp = recent_15[-1]["c"]
        if op <= 0:
            return
        pct_15m   = (cp - op) / op * 100
        threshold = cfg.get("FADE_TRIGGER_PCT", 10.0)
        if pct_15m < threshold:
            return

        # ── 阴线确认：最新 1m K 线必须收阴，说明动量已经开始反转 ────────────
        if buf[-1]["c"] >= buf[-1]["o"]:
            return  # 还在收阳，等下一根

        # ── BTC 方向过滤 ────────────────────────────────────────────────────
        if not self._btc_guard_short():
            return

        # ── REST 精确验证 (15m K 线 + VWAP + MTF) ──────────────────────────
        ok, bb_upper = await self._analyze_15m(symbol)
        if not ok:
            return

        self._last_signal_time[symbol] = datetime.now()
        logger.info(
            "📉 [%s] 反转信号 | 15m涨幅 +%.2f%% | BB上轨 %.6f | 阴线确认",
            symbol, pct_15m, bb_upper,
        )
        await self._execute_short(symbol, pct_15m, bb_upper)

    async def _fetch_klines(self, symbol: str, interval: str, limit: int):
        try:
            async with self.session.get(
                f"{_REST_BASE}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                return await resp.json() if resp.status == 200 else None
        except Exception:
            return None

    async def _analyze_15m(self, symbol: str) -> tuple[bool, float]:
        """拉取 15m/4H/1H K 线做精确验证; 返回 (通过?, BB上轨价)"""
        klines_15m, klines_4h, klines_1h = await asyncio.gather(
            self._fetch_klines(symbol, "15m", 30),
            self._fetch_klines(symbol, "4h",  22),
            self._fetch_klines(symbol, "1h",  22),
        )

        if not klines_15m or len(klines_15m) < 23:
            return False, 0.0

        closes = [float(k[4]) for k in klines_15m]
        highs  = [float(k[2]) for k in klines_15m]
        lows   = [float(k[3]) for k in klines_15m]
        vols   = [float(k[5]) for k in klines_15m]

        # ── 布林上轨 (20期, 2σ)，用最高价判断是否触碰 ────────────────────
        bb_closes = closes[-20:]
        mean      = sum(bb_closes) / 20
        std       = (sum((c - mean) ** 2 for c in bb_closes) / 20) ** 0.5
        bb_upper  = mean + 2 * std
        if highs[-1] < bb_upper * 0.99:   # 最高价未触碰上轨 (用high，更准确)
            return False, bb_upper

        # ── 最近 3 根 15m 高点不创新高（动量衰减）──────────────────────────
        h1, h2, h3 = highs[-1], highs[-2], highs[-3]
        if h1 >= h2:
            return False, bb_upper

        # ── 量能萎缩：最近 3 根均量 < 前 5 根均量 ──────────────────────────
        if len(vols) >= 8:
            avg_recent = sum(vols[-3:]) / 3
            avg_prev   = sum(vols[-8:-3]) / 5
            if avg_prev > 0 and avg_recent >= avg_prev:
                return False, bb_upper

        # ── VWAP 确认：价格显著高于 VWAP 才有做空价值 ───────────────────────
        if len(vols) >= 10 and sum(vols) > 0:
            typical = [(float(k[2]) + float(k[3]) + float(k[4])) / 3
                       for k in klines_15m]
            vwap_15m = sum(tp * v for tp, v in zip(typical, vols)) / sum(vols)
            if vwap_15m > 0 and closes[-1] < vwap_15m * 1.01:
                # 价格未显著高于 VWAP，说明没有足够的"泡沫"可以做空
                return False, bb_upper

        # ── MTF 确认：非强势上升趋势 ───────────────────────────────────────
        if not self._check_mtf_data(symbol, klines_4h, klines_1h):
            return False, bb_upper

        return True, bb_upper

    def _check_mtf_data(self, symbol: str, klines_4h, klines_1h) -> bool:
        def ema20_last(klines) -> tuple[float, float]:
            closes = [float(k[4]) for k in klines[-21:]]
            ema = closes[0]
            alpha = 2 / (20 + 1)
            for c in closes[1:]:
                ema = c * alpha + ema * (1 - alpha)
            return ema, closes[-1]

        if klines_4h and len(klines_4h) >= 21:
            ema_4h, close_4h = ema20_last(klines_4h)
            if close_4h > ema_4h * 1.05:
                logger.debug("📉 [%s] MTF: 4H 强势上涨 (%.4f > EMA20 %.4f ×1.05), 跳过",
                             symbol, close_4h, ema_4h)
                return False

        if klines_1h and len(klines_1h) >= 21:
            ema_1h, close_1h = ema20_last(klines_1h)
            if close_1h > ema_1h * 1.03:
                logger.debug("📉 [%s] MTF: 1H 强势上涨 (%.4f > EMA20 %.4f ×1.03), 跳过",
                             symbol, close_1h, ema_1h)
                return False

        return True

    # ─── 开仓 ──────────────────────────────────────────────────────────────────

    async def _execute_short(self, symbol: str, trigger_pct: float, bb_upper: float) -> None:
        cfg = self.cfg
        try:
            async with self.session.get(
                f"{_REST_BASE}/fapi/v1/ticker/price",
                params={"symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                entry_price = float((await resp.json())["price"])
        except Exception as e:
            logger.error("📉 [%s] 获取入场价失败: %s", symbol, e)
            return

        sl_pct  = cfg.get("FADE_STOP_LOSS_PCT", 1.5)
        tp1_pct = cfg.get("FADE_TP1_PCT",       1.5)
        tp2_pct = cfg.get("FADE_TP2_PCT",       3.0)
        sl_price  = entry_price * (1 + sl_pct  / 100)
        tp1_price = entry_price * (1 - tp1_pct / 100)
        tp2_price = entry_price * (1 - tp2_pct / 100)

        base_signal = {
            "type":        "fade",
            "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":      symbol,
            "direction":   "SHORT",
            "trigger_pct": round(trigger_pct, 2),
            "entry_price": round(entry_price, 8),
            "sl_price":    round(sl_price, 8),
            "tp1_price":   round(tp1_price, 8),
            "tp2_price":   round(tp2_price, 8),
            "bb_upper":    round(bb_upper, 8),
        }

        if not cfg.get("FADE_AUTO_TRADE", False):
            add_fade_signal({**base_signal, "auto_traded": False, "paper": False})
            logger.info("📉 [%s] 信号发出 (自动交易关闭，未实际开仓)", symbol)
            return

        leverage      = cfg.get("FADE_LEVERAGE",     10)
        position_usdt = cfg.get("FADE_POSITION_USDT", 50.0)
        quantity      = position_usdt * leverage / entry_price
        tp1_ratio     = cfg.get("FADE_TP1_RATIO",    0.5)
        tp2_ratio     = cfg.get("FADE_TP2_RATIO",    0.4)

        # ── 模拟开仓 ──────────────────────────────────────────────────────
        if cfg.get("FADE_PAPER_TRADE", False):
            pos = FadePosition(
                symbol             = symbol,
                entry_price        = entry_price,
                quantity           = quantity,
                quantity_remaining = quantity,
                sl_price           = sl_price,
                tp1_price          = tp1_price,
                tp2_price          = tp2_price,
                current_price      = entry_price,
                paper              = True,
            )
            self.open_positions[symbol] = pos
            set_fade_position(symbol, pos.to_dict())
            add_fade_signal({
                **base_signal,
                "auto_traded": True,
                "paper":       True,
                "quantity":    round(quantity, 6),
                "leverage":    leverage,
            })
            logger.info(
                "📉 [%s] 📋 模拟做空 ×%.6f @ %.6f | SL %.6f | TP1 %.6f | TP2 %.6f",
                symbol, quantity, entry_price, sl_price, tp1_price, tp2_price,
            )
            return

        # ── 真实开仓 ──────────────────────────────────────────────────────
        await self.trader.set_leverage(symbol, leverage)
        trade_resp = await self.trader.place_market_order(symbol, "SELL", quantity)
        if not trade_resp:
            logger.error("📉 [%s] 市价做空失败", symbol)
            return
        actual = float(trade_resp.get("avgPrice") or entry_price)
        if actual > 0:
            entry_price = actual

        sl_resp     = await self.trader.place_stop_loss_order(symbol, "BUY", sl_price)
        sl_order_id = sl_resp.get("orderId") if sl_resp else None

        pos = FadePosition(
            symbol             = symbol,
            entry_price        = entry_price,
            quantity           = quantity,
            quantity_remaining = quantity,
            sl_price           = sl_price,
            tp1_price          = tp1_price,
            tp2_price          = tp2_price,
            sl_order_id        = sl_order_id,
            current_price      = entry_price,
            paper              = False,
        )
        self.open_positions[symbol] = pos
        set_fade_position(symbol, pos.to_dict())
        add_fade_signal({
            **base_signal,
            "auto_traded": True,
            "paper":       False,
            "quantity":    round(quantity, 6),
            "leverage":    leverage,
            "order_id":    trade_resp.get("orderId"),
        })
        logger.info(
            "📉 [%s] 做空开仓成功 ×%.6f @ %.6f | SL %.6f | TP1 %.6f | TP2 %.6f",
            symbol, quantity, entry_price, sl_price, tp1_price, tp2_price,
        )

    # ─── TP / SL 检查 ──────────────────────────────────────────────────────────

    def _record_fade_trade(self, pos, exit_price: float, close_reason: str, extra_pnl: float = 0.0) -> None:
        total_pnl = pos.realized_pnl + extra_pnl
        position_usdt = pos.entry_price * pos.quantity / self.cfg.get("FADE_LEVERAGE", 10)
        pnl_pct = total_pnl / position_usdt * 100 if position_usdt > 0 else 0.0
        add_fade_trade({
            "symbol":       pos.symbol,
            "direction":    "SHORT",
            "entry_price":  round(pos.entry_price, 8),
            "exit_price":   round(exit_price, 8),
            "entry_time":   pos.entry_time,
            "exit_time":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pnl_usdt":     round(total_pnl, 4),
            "pnl_pct":      round(pnl_pct, 2),
            "close_reason": close_reason,
            "paper":        pos.paper,
            "leverage":     self.cfg.get("FADE_LEVERAGE", 10),
            "quantity":     round(pos.quantity, 6),
        })

    async def _check_tp_sl(self, symbol: str, price: float) -> None:
        pos = self.open_positions.get(symbol)
        if not pos:
            return
        cfg       = self.cfg
        tp1_ratio = cfg.get("FADE_TP1_RATIO", 0.5)
        tp2_ratio = cfg.get("FADE_TP2_RATIO", 0.4)
        auto      = cfg.get("FADE_AUTO_TRADE", False)
        is_paper  = pos.paper
        tag       = "📋 " if is_paper else ""

        if not pos.tp1_hit and price <= pos.tp1_price:
            pos.tp1_hit = True
            qty = pos.quantity * tp1_ratio
            tp1_pnl = qty * (pos.entry_price - price)
            if is_paper:
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp1_pnl
            elif auto:
                await self.trader.place_market_order(symbol, "BUY", qty)
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp1_pnl
            pct = (pos.entry_price - price) / pos.entry_price * 100
            logger.info("📉 [%s] %sTP1 命中 @ %.6f (+%.2f%%)", symbol, tag, price, pct)
            set_fade_position(symbol, pos.to_dict())

        elif pos.tp1_hit and not pos.tp2_hit and price <= pos.tp2_price:
            pos.tp2_hit = True
            qty = pos.quantity * tp2_ratio
            tp2_pnl = qty * (pos.entry_price - price)
            if is_paper:
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp2_pnl
            elif auto:
                await self.trader.place_market_order(symbol, "BUY", qty)
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp2_pnl
            pct = (pos.entry_price - price) / pos.entry_price * 100
            logger.info("📉 [%s] %sTP2 命中 @ %.6f (+%.2f%%) | 全平", symbol, tag, price, pct)
            self._record_fade_trade(pos, price, "TP2", tp2_pnl)
            del self.open_positions[symbol]
            set_fade_position(symbol, None)
            return

        sl_crossed = price >= pos.sl_price * 1.001
        if sl_crossed and symbol in self.open_positions:
            sl_pnl = pos.quantity_remaining * (pos.entry_price - price)
            reason = "SL_保本" if pos.tp1_hit else "SL"
            logger.info("📉 [%s] %s%s 触发 @ %.6f", symbol, tag, reason, price)
            if not is_paper and auto and pos.quantity_remaining > 0:
                await self.trader.place_market_order(symbol, "BUY", pos.quantity_remaining)
            self._record_fade_trade(pos, price, reason, sl_pnl)
            del self.open_positions[symbol]
            set_fade_position(symbol, None)

    # ─── 心跳日志（每 5 分钟汇报一次运行状态）────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        await asyncio.sleep(30)
        while self.running:
            cfg       = self.cfg
            buffered  = sum(1 for b in self.kline_buffer.values() if len(b) >= 15)
            positions = len(self.open_positions)
            paper_cnt = sum(1 for p in self.open_positions.values() if p.paper)
            real_cnt  = positions - paper_cnt
            cooldown  = cfg.get("FADE_COOLDOWN_MINUTES", 30)
            in_cool   = sum(
                1 for sym, t in self._last_signal_time.items()
                if (datetime.now() - t).total_seconds() < cooldown * 60
            )
            enabled   = cfg.get("FADE_ENABLED", False)
            auto      = cfg.get("FADE_AUTO_TRADE", False)
            paper_m   = cfg.get("FADE_PAPER_TRADE", False)
            mode_str  = "自动下单" if auto else ("模拟开仓" if paper_m else "仅信号")

            logger.info(
                "📉 一直做空心跳 | 策略%s | 监控%d个 | 已就绪%d个 | "
                "活跃仓位%d个(真实%d/模拟%d) | 冷却中%d个 | 触发阈值%.0f%% | 模式:%s",
                "开启" if enabled else "关闭",
                len(self.monitored_symbols), buffered,
                positions, real_cnt, paper_cnt,
                in_cool,
                cfg.get("FADE_TRIGGER_PCT", 10.0),
                mode_str,
            )
            await asyncio.sleep(300)

    # ─── REST 仓位同步 ─────────────────────────────────────────────────────────

    async def _position_monitor_loop(self) -> None:
        while self.running:
            await asyncio.sleep(30)
            if not self.open_positions or not self.trader:
                continue
            for symbol in list(self.open_positions.keys()):
                if self.open_positions[symbol].paper:
                    continue
                try:
                    pos_data = await self.trader.get_position(symbol)
                    if pos_data is None:
                        logger.info("📉 [%s] 仓位已关闭 (REST 同步)", symbol)
                        self.open_positions.pop(symbol, None)
                        set_fade_position(symbol, None)
                except Exception as e:
                    logger.debug("📉 仓位同步异常 [%s]: %s", symbol, e)
