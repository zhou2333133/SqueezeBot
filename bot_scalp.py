"""
超短线动量跟进机器人
策略: N 分钟内涨跌超过 X% + 量能确认 → 市价开仓 + 三档分批止盈
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp

from config import config_manager
from signals import add_scalp_signal, set_scalp_position
from trader import BinanceTrader

logger = logging.getLogger("bot_scalp")

_REST_BASE = "https://fapi.binance.com"
_WS_URL    = "wss://fstream.binance.com/ws"


@dataclass
class ScalpPosition:
    symbol:             str
    direction:          str
    entry_price:        float
    quantity:           float
    quantity_remaining: float
    sl_price:           float
    tp1_price:          float
    tp2_price:          float
    tp1_hit:            bool       = False
    tp2_hit:            bool       = False
    entry_time:         str        = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    sl_order_id:        int | None = None
    realized_pnl:       float      = 0.0
    current_price:      float      = 0.0

    def to_dict(self) -> dict:
        unreal = 0.0
        if self.current_price and self.entry_price:
            if self.direction == "LONG":
                unreal = (self.current_price - self.entry_price) / self.entry_price * 100
            else:
                unreal = (self.entry_price - self.current_price) / self.entry_price * 100
        return {
            "symbol":             self.symbol,
            "direction":          self.direction,
            "entry_price":        round(self.entry_price, 8),
            "quantity":           round(self.quantity, 6),
            "quantity_remaining": round(self.quantity_remaining, 6),
            "sl_price":           round(self.sl_price, 8),
            "tp1_price":          round(self.tp1_price, 8),
            "tp2_price":          round(self.tp2_price, 8),
            "tp1_hit":            self.tp1_hit,
            "tp2_hit":            self.tp2_hit,
            "entry_time":         self.entry_time,
            "realized_pnl":       round(self.realized_pnl, 4),
            "unrealized_pct":     round(unreal, 2),
            "current_price":      round(self.current_price, 8),
        }


class BinanceScalpBot:
    def __init__(self):
        self.open_positions:     dict[str, ScalpPosition] = {}
        self.kline_buffer:       dict[str, list]          = {}
        self.monitored_symbols:  list[str]                = []
        self.running:            bool                     = False
        self.session:            aiohttp.ClientSession | None = None
        self.trader:             BinanceTrader | None         = None

    @property
    def cfg(self) -> dict:
        return config_manager.settings

    # ─── 启动 ──────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.running = True
        logger.info("⚡ 超短线机器人启动")
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                self.session = session
                self.trader  = BinanceTrader(session)
                await self.refresh_symbols()
                await asyncio.gather(
                    self._ws_loop(),
                    self._position_monitor_loop(),
                )
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            logger.info("⚡ 超短线机器人已停止")

    # ─── 币种列表 ──────────────────────────────────────────────────────────────

    async def refresh_symbols(self) -> None:
        custom = self.cfg.get("SCALP_WATCHLIST", "").strip()
        if custom:
            self.monitored_symbols = [s.strip().upper() for s in custom.split(",") if s.strip()]
            logger.info("⚡ 自定义监控列表: %d 个币种", len(self.monitored_symbols))
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
                    logger.info("⚡ 自动检测: %d 个 USDT 永续合约", len(self.monitored_symbols))
        except Exception as e:
            logger.error("⚡ 获取币种列表失败: %s，使用兜底列表", e)
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
                    logger.warning("⚡ WS 断线: %s，%ds 后重连...", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def _ws_connect(self) -> None:
        async with self.session.ws_connect(_WS_URL, heartbeat=20) as ws:
            # 分批订阅（Binance 单次最多 200 个 stream）
            params = [f"{s.lower()}@kline_1m" for s in self.monitored_symbols]
            for chunk in [params[i:i + 200] for i in range(0, len(params), 200)]:
                await ws.send_str(json.dumps({"method": "SUBSCRIBE", "params": chunk, "id": 1}))

            logger.info("⚡ WS 已连接，监控 %d 个币种", len(self.monitored_symbols))
            async for msg in ws:
                if not self.running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._on_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

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

        # 更新活跃仓位的当前价格（每 tick）
        if symbol in self.open_positions and not is_closed:
            self.open_positions[symbol].current_price = close_price
            set_scalp_position(symbol, self.open_positions[symbol].to_dict())
            await self._check_tp_sl(symbol, close_price)

        if is_closed:
            buf = self.kline_buffer.setdefault(symbol, [])
            buf.append({
                "o": float(k["o"]),
                "c": float(k["c"]),
                "q": float(k["q"]),  # USDT 成交额
            })
            if len(buf) > 30:
                buf.pop(0)
            await self._check_momentum(symbol)

    # ─── 动量检测 ──────────────────────────────────────────────────────────────

    async def _check_momentum(self, symbol: str) -> None:
        cfg = self.cfg
        if not cfg.get("SCALP_ENABLED", False):
            return
        if symbol in self.open_positions:
            return
        if len(self.open_positions) >= cfg.get("SCALP_MAX_POSITIONS", 3):
            return

        buf    = self.kline_buffer.get(symbol, [])
        window = cfg.get("SCALP_WINDOW_MINUTES", 3)
        if len(buf) < window:
            return

        recent = buf[-window:]
        op     = recent[0]["o"]
        cp     = recent[-1]["c"]
        if op <= 0:
            return

        pct       = (cp - op) / op * 100
        threshold = cfg.get("SCALP_TRIGGER_PCT", 4.0)

        # 量能确认
        vol_ok   = True
        vol_mult = cfg.get("SCALP_VOLUME_MULTIPLIER", 3.0)
        if len(buf) > window:
            past    = buf[:-window]
            avg_vol = sum(k["q"] for k in past) / len(past)
            cur_vol = sum(k["q"] for k in recent) / window
            vol_ok  = cur_vol >= avg_vol * vol_mult if avg_vol > 0 else True

        direction = None
        if pct >= threshold and cfg.get("SCALP_ENABLE_LONG", True):
            direction = "LONG"
        elif pct <= -threshold and cfg.get("SCALP_ENABLE_SHORT", True):
            direction = "SHORT"

        if not direction:
            return

        if not vol_ok:
            logger.info("⚡ [%s] 动量 %+.2f%% 触发，量能不足 (需 %.1fx 均量)，跳过",
                        symbol, pct, vol_mult)
            return

        logger.info("⚡ [%s] 动量触发 %+.2f%% | %s | 量能 ✅", symbol, pct, direction)
        await self._execute_entry(symbol, direction, pct)

    # ─── 开仓 ──────────────────────────────────────────────────────────────────

    async def _execute_entry(self, symbol: str, direction: str, trigger_pct: float) -> None:
        cfg = self.cfg

        try:
            async with self.session.get(
                f"{_REST_BASE}/fapi/v1/ticker/price",
                params={"symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                entry_price = float((await resp.json())["price"])
        except Exception as e:
            logger.error("⚡ [%s] 获取入场价失败: %s", symbol, e)
            return

        sl_pct  = cfg.get("SCALP_STOP_LOSS_PCT", 1.5)
        tp1_pct = cfg.get("SCALP_TP1_PCT",       1.5)
        tp2_pct = cfg.get("SCALP_TP2_PCT",       3.0)

        if direction == "LONG":
            sl_price  = entry_price * (1 - sl_pct  / 100)
            tp1_price = entry_price * (1 + tp1_pct / 100)
            tp2_price = entry_price * (1 + tp2_pct / 100)
        else:
            sl_price  = entry_price * (1 + sl_pct  / 100)
            tp1_price = entry_price * (1 - tp1_pct / 100)
            tp2_price = entry_price * (1 - tp2_pct / 100)

        base_signal = {
            "type":        "scalp",
            "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":      symbol,
            "direction":   direction,
            "trigger_pct": round(trigger_pct, 2),
            "entry_price": round(entry_price, 8),
            "sl_price":    round(sl_price, 8),
            "tp1_price":   round(tp1_price, 8),
            "tp2_price":   round(tp2_price, 8),
        }

        if not cfg.get("SCALP_AUTO_TRADE", False):
            add_scalp_signal({**base_signal, "auto_traded": False})
            logger.info("⚡ [%s] 信号发出 (自动交易关闭，未实际开仓)", symbol)
            return

        leverage      = cfg.get("SCALP_LEVERAGE",     10)
        position_usdt = cfg.get("SCALP_POSITION_USDT", 50.0)

        await self.trader.set_leverage(symbol, leverage)
        quantity = position_usdt * leverage / entry_price
        side     = "BUY"  if direction == "LONG" else "SELL"
        exit_s   = "SELL" if direction == "LONG" else "BUY"

        trade_resp = await self.trader.place_market_order(symbol, side, quantity)
        if not trade_resp:
            logger.error("⚡ [%s] 市价开仓失败", symbol)
            return

        actual = float(trade_resp.get("avgPrice") or entry_price)
        if actual > 0:
            entry_price = actual

        sl_resp     = await self.trader.place_stop_loss_order(symbol, exit_s, sl_price)
        sl_order_id = sl_resp.get("orderId") if sl_resp else None

        pos = ScalpPosition(
            symbol             = symbol,
            direction          = direction,
            entry_price        = entry_price,
            quantity           = quantity,
            quantity_remaining = quantity,
            sl_price           = sl_price,
            tp1_price          = tp1_price,
            tp2_price          = tp2_price,
            sl_order_id        = sl_order_id,
            current_price      = entry_price,
        )
        self.open_positions[symbol] = pos
        set_scalp_position(symbol, pos.to_dict())

        add_scalp_signal({
            **base_signal,
            "auto_traded": True,
            "quantity":    round(quantity, 6),
            "leverage":    leverage,
            "order_id":    trade_resp.get("orderId"),
        })
        logger.info(
            "⚡ [%s] 开仓成功 %s ×%.6f @ %.6f | SL %.6f | TP1 %.6f | TP2 %.6f",
            symbol, direction, quantity, entry_price, sl_price, tp1_price, tp2_price,
        )

    # ─── TP / SL 实时检查 ──────────────────────────────────────────────────────

    async def _check_tp_sl(self, symbol: str, price: float) -> None:
        pos = self.open_positions.get(symbol)
        if not pos:
            return

        cfg       = self.cfg
        tp1_ratio = cfg.get("SCALP_TP1_RATIO",    0.4)
        tp2_ratio = cfg.get("SCALP_TP2_RATIO",    0.4)
        trail_pct = cfg.get("SCALP_TP3_TRAIL_PCT", 1.0)
        exit_s    = "SELL" if pos.direction == "LONG" else "BUY"
        auto      = cfg.get("SCALP_AUTO_TRADE", False)

        def _tp_hit(tp_price: float) -> bool:
            return (pos.direction == "LONG"  and price >= tp_price) or \
                   (pos.direction == "SHORT" and price <= tp_price)

        if not pos.tp1_hit and _tp_hit(pos.tp1_price):
            pos.tp1_hit = True
            if auto:
                qty = pos.quantity * tp1_ratio
                await self.trader.place_market_order(symbol, exit_s, qty)
                pos.quantity_remaining -= qty
                pos.realized_pnl += qty * abs(price - pos.entry_price)
            pct = abs(price - pos.entry_price) / pos.entry_price * 100
            logger.info("⚡ [%s] TP1 命中 @ %.6f (+%.2f%%) | 平 %.0f%%", symbol, price, pct, tp1_ratio * 100)
            set_scalp_position(symbol, pos.to_dict())

        elif pos.tp1_hit and not pos.tp2_hit and _tp_hit(pos.tp2_price):
            pos.tp2_hit = True
            if auto:
                qty = pos.quantity * tp2_ratio
                await self.trader.place_market_order(symbol, exit_s, qty)
                pos.quantity_remaining -= qty
                pos.realized_pnl += qty * abs(price - pos.entry_price)
                if pos.quantity_remaining > 0:
                    activ = price * (1 - trail_pct / 200) if pos.direction == "LONG" \
                            else price * (1 + trail_pct / 200)
                    await self.trader.place_trailing_stop_order(
                        symbol, exit_s, activ, trail_pct, pos.quantity_remaining,
                    )
            pct = abs(price - pos.entry_price) / pos.entry_price * 100
            logger.info("⚡ [%s] TP2 命中 @ %.6f (+%.2f%%) | 余仓追踪止损", symbol, price, pct)
            del self.open_positions[symbol]
            set_scalp_position(symbol, None)
            return

        # 止损触发检测（交易所已执行 STOP_MARKET，本地同步清除）
        sl_crossed = (pos.direction == "LONG"  and price <= pos.sl_price * 0.999) or \
                     (pos.direction == "SHORT" and price >= pos.sl_price * 1.001)
        if sl_crossed and symbol in self.open_positions:
            logger.info("⚡ [%s] SL 触发 @ %.6f", symbol, price)
            del self.open_positions[symbol]
            set_scalp_position(symbol, None)

    # ─── REST 仓位同步（防 WS 漏消息）────────────────────────────────────────

    async def _position_monitor_loop(self) -> None:
        while self.running:
            await asyncio.sleep(30)
            if not self.open_positions or not self.trader:
                continue
            for symbol in list(self.open_positions.keys()):
                try:
                    pos_data = await self.trader.get_position(symbol)
                    if pos_data is None:
                        logger.info("⚡ [%s] 仓位已关闭 (REST 同步)", symbol)
                        self.open_positions.pop(symbol, None)
                        set_scalp_position(symbol, None)
                except Exception as e:
                    logger.debug("⚡ 仓位同步异常 [%s]: %s", symbol, e)
