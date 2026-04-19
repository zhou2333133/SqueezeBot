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
    paper:              bool       = False
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
            "paper":              self.paper,
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
                    self._heartbeat_loop(),
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
            # 分批订阅（Binance 单次最多 200 个 stream，总上限 1024）
            params = [f"{s.lower()}@kline_1m" for s in self.monitored_symbols]
            for i, chunk in enumerate([params[j:j + 200] for j in range(0, len(params), 200)]):
                await ws.send_str(json.dumps({"method": "SUBSCRIBE", "params": chunk, "id": i + 1}))
                await asyncio.sleep(0.3)  # 避免服务端来不及处理

            logger.info("⚡ WS 已连接，监控 %d 个币种", len(self.monitored_symbols))
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
                logger.warning("⚡ WS 连接关闭 (code=%s)，即将重连...", close_code)

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
                "q": float(k["q"]),   # 总 USDT 成交额
                "Q": float(k["Q"]),   # 主动买入 USDT 成交额 (taker buy)
            })
            if len(buf) > 60:
                buf.pop(0)
            await self._check_momentum(symbol)

    # ─── 辅助：VWAP ────────────────────────────────────────────────────────────

    @staticmethod
    def _calc_vwap(buf: list) -> float:
        """从 1m kline buffer 计算 VWAP (成交量加权均价)"""
        total_q    = sum(k["q"] for k in buf)
        total_base = sum(2 * k["q"] / (k["o"] + k["c"])
                        for k in buf if k["o"] + k["c"] > 0)
        return total_q / total_base if total_base > 0 else 0.0

    def _btc_guard(self, direction: str) -> bool:
        """BTC 5分钟方向过滤：急涨时不做空，急跌时不做多"""
        guard_pct = self.cfg.get("BTC_GUARD_PCT", 2.0)
        btc_buf   = self.kline_buffer.get("BTCUSDT", [])
        if len(btc_buf) < 5:
            return True
        btc_5m = (btc_buf[-1]["c"] - btc_buf[-5]["o"]) / btc_buf[-5]["o"] * 100
        if direction == "LONG"  and btc_5m < -guard_pct:
            logger.debug("⚡ BTC急跌 %.2f%%，跳过做多", btc_5m)
            return False
        if direction == "SHORT" and btc_5m >  guard_pct:
            logger.debug("⚡ BTC急拉 +%.2f%%，跳过做空", btc_5m)
            return False
        return True

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

        # ── 量能确认 ──────────────────────────────────────────────────────
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

        if not direction or not vol_ok:
            if not vol_ok and direction:
                logger.info("⚡ [%s] 动量 %+.2f%% 触发，量能不足，跳过", symbol, pct)
            return

        # ── BTC 方向过滤 ──────────────────────────────────────────────────
        if not self._btc_guard(direction):
            return

        # ── Taker 主动买入比例确认 ────────────────────────────────────────
        taker_min = cfg.get("SCALP_TAKER_RATIO_MIN", 0.55)
        total_q  = sum(k["q"] for k in recent)
        total_qb = sum(k["Q"] for k in recent)
        taker_ratio = total_qb / total_q if total_q > 0 else 0.5
        if direction == "LONG"  and taker_ratio < taker_min:
            logger.info("⚡ [%s] 动量 +%.2f%%，Taker买入 %.0f%% 不足，跳过做多",
                        symbol, pct, taker_ratio * 100)
            return
        if direction == "SHORT" and taker_ratio > (1 - taker_min):
            logger.info("⚡ [%s] 动量 %.2f%%，Taker卖出 %.0f%% 不足，跳过做空",
                        symbol, pct, (1 - taker_ratio) * 100)
            return

        # ── VWAP 偏离过滤（只防追高做多，不限制做空）────────────────────
        # SHORT 方向：暴跌时价格必然低于 VWAP，过滤做空没有意义
        vwap_max = cfg.get("SCALP_VWAP_MAX_DEV", 3.0)
        if direction == "LONG" and len(buf) >= 10:
            vwap    = self._calc_vwap(buf)
            dev_pct = (cp - vwap) / vwap * 100 if vwap > 0 else 0
            if dev_pct > vwap_max:
                logger.info("⚡ [%s] 价格偏离VWAP +%.1f%%，拒绝追高做多", symbol, dev_pct)
                return

        logger.info("⚡ [%s] 动量触发 %+.2f%% | %s | 量能✅ Taker%.0f%%",
                    symbol, pct, direction, taker_ratio * 100)
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
            add_scalp_signal({**base_signal, "auto_traded": False, "paper": False})
            logger.info("⚡ [%s] 信号发出 (自动交易关闭，未实际开仓)", symbol)
            return

        leverage      = cfg.get("SCALP_LEVERAGE",     10)
        position_usdt = cfg.get("SCALP_POSITION_USDT", 50.0)
        quantity      = position_usdt * leverage / entry_price
        side          = "BUY"  if direction == "LONG" else "SELL"
        exit_s        = "SELL" if direction == "LONG" else "BUY"

        # ── 模拟开仓 ──────────────────────────────────────────────────────────
        if cfg.get("SCALP_PAPER_TRADE", False):
            pos = ScalpPosition(
                symbol             = symbol,
                direction          = direction,
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
            set_scalp_position(symbol, pos.to_dict())
            add_scalp_signal({
                **base_signal,
                "auto_traded": True,
                "paper":       True,
                "quantity":    round(quantity, 6),
                "leverage":    leverage,
            })
            logger.info(
                "⚡ [%s] 📋 模拟开仓 %s ×%.6f @ %.6f | SL %.6f | TP1 %.6f | TP2 %.6f",
                symbol, direction, quantity, entry_price, sl_price, tp1_price, tp2_price,
            )
            return

        # ── 真实开仓 ──────────────────────────────────────────────────────────
        await self.trader.set_leverage(symbol, leverage)
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
            paper              = False,
        )
        self.open_positions[symbol] = pos
        set_scalp_position(symbol, pos.to_dict())

        add_scalp_signal({
            **base_signal,
            "auto_traded": True,
            "paper":       False,
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
        is_paper  = pos.paper
        tag       = "📋 " if is_paper else ""

        def _tp_hit(tp_price: float) -> bool:
            return (pos.direction == "LONG"  and price >= tp_price) or \
                   (pos.direction == "SHORT" and price <= tp_price)

        if not pos.tp1_hit and _tp_hit(pos.tp1_price):
            pos.tp1_hit = True
            qty = pos.quantity * tp1_ratio
            if is_paper:
                pos.quantity_remaining -= qty
                pos.realized_pnl += qty * abs(price - pos.entry_price)
                pos.sl_price = pos.entry_price  # 移止损到成本价
            elif auto:
                await self.trader.place_market_order(symbol, exit_s, qty)
                pos.quantity_remaining -= qty
                pos.realized_pnl += qty * abs(price - pos.entry_price)
                # 取消原止损单，在成本价挂新止损（保本）
                if pos.sl_order_id:
                    await self.trader.cancel_order(symbol, pos.sl_order_id)
                new_sl = await self.trader.place_stop_loss_order(
                    symbol, exit_s, pos.entry_price
                )
                if new_sl and new_sl.get("orderId"):
                    pos.sl_order_id = new_sl["orderId"]
                    pos.sl_price    = pos.entry_price
            pct = abs(price - pos.entry_price) / pos.entry_price * 100
            logger.info("⚡ [%s] %sTP1 命中 @ %.6f (+%.2f%%) | 止损移至成本价 %.6f",
                        symbol, tag, price, pct, pos.entry_price)
            set_scalp_position(symbol, pos.to_dict())

        elif pos.tp1_hit and not pos.tp2_hit and _tp_hit(pos.tp2_price):
            pos.tp2_hit = True
            qty = pos.quantity * tp2_ratio
            if is_paper:
                pos.quantity_remaining -= qty
                pos.realized_pnl += qty * abs(price - pos.entry_price)
            elif auto:
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
            logger.info("⚡ [%s] %sTP2 命中 @ %.6f (+%.2f%%) | 余仓追踪止损",
                        symbol, tag, price, pct)
            del self.open_positions[symbol]
            set_scalp_position(symbol, None)
            return

        sl_crossed = (pos.direction == "LONG"  and price <= pos.sl_price * 0.999) or \
                     (pos.direction == "SHORT" and price >= pos.sl_price * 1.001)
        if sl_crossed and symbol in self.open_positions:
            logger.info("⚡ [%s] %sSL 触发 @ %.6f", symbol, tag, price)
            del self.open_positions[symbol]
            set_scalp_position(symbol, None)

    # ─── 心跳日志（每 5 分钟汇报一次运行状态）────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        await asyncio.sleep(30)  # 启动后稍等，让 WS 先连上
        while self.running:
            cfg        = self.cfg
            buffered   = sum(1 for b in self.kline_buffer.values() if len(b) >= cfg.get("SCALP_WINDOW_MINUTES", 3))
            positions  = len(self.open_positions)
            paper_cnt  = sum(1 for p in self.open_positions.values() if p.paper)
            real_cnt   = positions - paper_cnt
            enabled    = cfg.get("SCALP_ENABLED", False)
            auto       = cfg.get("SCALP_AUTO_TRADE", False)
            paper_mode = cfg.get("SCALP_PAPER_TRADE", False)

            mode_str = "自动下单" if auto else ("模拟开仓" if paper_mode else "仅信号")
            logger.info(
                "⚡ 超短线心跳 | 策略%s | 监控%d个 | 已就绪%d个 | "
                "活跃仓位%d个(真实%d/模拟%d) | 触发阈值%.1f%% %dm | 模式:%s",
                "开启" if enabled else "关闭",
                len(self.monitored_symbols), buffered,
                positions, real_cnt, paper_cnt,
                cfg.get("SCALP_TRIGGER_PCT", 4.0),
                cfg.get("SCALP_WINDOW_MINUTES", 3),
                mode_str,
            )
            await asyncio.sleep(300)  # 每 5 分钟

    # ─── REST 仓位同步（防 WS 漏消息）────────────────────────────────────────

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
                        logger.info("⚡ [%s] 仓位已关闭 (REST 同步)", symbol)
                        self.open_positions.pop(symbol, None)
                        set_scalp_position(symbol, None)
                except Exception as e:
                    logger.debug("⚡ 仓位同步异常 [%s]: %s", symbol, e)
