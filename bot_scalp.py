"""
超短线动量跟进机器人
策略: N 分钟内涨跌超过 X% + 量能确认 → 市价开仓 + 三档分批止盈
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp

from config import config_manager
from market_hub import hub
import signals as _signals_mod
from signals import add_scalp_signal, set_scalp_position, add_scalp_trade
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
    trail_ref_price:    float      = 0.0   # TP2命中后追踪基准价
    signal_label:       str        = ""    # 信号类型：回调/反弹=顺势单，反转=逆势单
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
        self.open_positions:       dict[str, ScalpPosition] = {}
        self.kline_buffer:         dict[str, list]          = {}
        self.monitored_symbols:    list[str]                = []
        self.cooldown_symbols:     dict[str, float]         = {}  # 止损冷却（默认关闭）
        self.slope_at_cooldown:    dict[str, float]         = {}  # 止损时记录斜率方向，用于提前解冻
        self.pending_entries:      dict[str, dict]          = {}  # 等待右侧K线确认的信号
        self.last_realtime_check:  dict[str, float]         = {}  # 实时检测防抖（每5秒最多触发一次）
        self.running:              bool                     = False
        self.session:              aiohttp.ClientSession | None = None
        self.trader:               BinanceTrader | None         = None
        # ── 过滤统计（每5分钟输出一次，帮助调参）─────────────────────────
        self._fstat:               dict[str, int]           = {
            "checked": 0, "no_active": 0, "no_signal": 0,
            "btc_guard": 0, "taker": 0, "vwap": 0, "hub": 0, "passed": 0,
        }
        self._fstat_ts:            float                    = 0.0

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

        if not is_closed:
            # 实时更新仓位价格并检查 TP/SL
            if symbol in self.open_positions:
                self.open_positions[symbol].current_price = close_price
                set_scalp_position(symbol, self.open_positions[symbol].to_dict())
                await self._check_tp_sl(symbol, close_price)
            # 实时动量检测：不等K线关闭，每5秒触发一次（快速抓波动）
            elif self.cfg.get("SCALP_ENABLED", False) and symbol not in self.pending_entries:
                now = time.monotonic()
                last = self.last_realtime_check.get(symbol, 0)
                if now - last >= 5:
                    buf = self.kline_buffer.get(symbol, [])
                    window = self.cfg.get("SCALP_WINDOW_MINUTES", 3)
                    threshold = self.cfg.get("SCALP_TRIGGER_PCT", 4.0)
                    # 至少需要 window-1 根已闭合K线 + 当前价
                    if len(buf) >= window - 1 and window > 1:
                        op = buf[-(window - 1)]["o"]
                        if op > 0 and abs((close_price - op) / op * 100) >= threshold:
                            self.last_realtime_check[symbol] = now
                            await self._check_momentum(symbol, realtime_price=close_price)
        else:
            buf = self.kline_buffer.setdefault(symbol, [])
            buf.append({
                "o": float(k["o"]),
                "c": float(k["c"]),
                "q": float(k["q"]),
                "Q": float(k["Q"]),
            })
            if len(buf) > 60:
                buf.pop(0)
            self.last_realtime_check.pop(symbol, None)  # K线关闭后重置，让下根K线重新实时检测
            # 优先检查待确认信号；无论结果如何，本K线不重复触发动量扫描
            if symbol in self.pending_entries and symbol not in self.open_positions:
                await self._check_pending_entry(symbol, buf)
                return
            await self._check_momentum(symbol)

    # ─── 辅助：VWAP ────────────────────────────────────────────────────────────

    @staticmethod
    def _calc_slope(closes: list[float], n: int = 8) -> float:
        """
        计算最近 n 根K线收盘价的线性回归斜率（归一化为均价的%/根）
        正值=上升，负值=下降，绝对值越大趋势越陡峭（瀑布/火箭判断依据）
        """
        if len(closes) < n:
            return 0.0
        pts = closes[-n:]
        x_mean = (n - 1) / 2.0
        y_mean = sum(pts) / n
        if y_mean == 0:
            return 0.0
        num = sum((i - x_mean) * (pts[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        return (num / den) / y_mean * 100 if den > 0 else 0.0

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

    # ─── 三层选币框架 ─────────────────────────────────────────────────────────

    def _is_coin_active(self, buf: list, cp: float) -> bool:
        """
        第一层：15分钟内振幅是否足够大（币是否处于活跃行情中）
        只有活跃的币才进入后续分析，过滤掉大多数平静的币种
        """
        if len(buf) < 15:
            return False
        prices = [k["c"] for k in buf[-15:]] + [cp]
        min_p, max_p = min(prices), max(prices)
        if min_p <= 0:
            return False
        return (max_p - min_p) / min_p * 100 >= self.cfg.get("SCALP_ACTIVE_RANGE_PCT", 6.0)

    def _detect_trend(self, buf: list) -> str:
        """
        第二层：用均线排列 + 斜率判断趋势方向
        MA5 > MA10 > MA20 + 斜率陡峭 → 单边飙升（WATERFALL_UP）
        MA5 > MA10 > MA20             → 上行趋势（UP）
        MA5 < MA10 < MA20 + 斜率陡峭 → 单边瀑布（WATERFALL_DOWN）
        MA5 < MA10 < MA20             → 下行趋势（DOWN）
        其他                           → 震荡横盘（FLAT）
        """
        if len(buf) < 20:
            return "FLAT"
        closes = [k["c"] for k in buf]
        ma5  = sum(closes[-5:])  / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        n       = self.cfg.get("SCALP_SLOPE_LOOKBACK",  8)
        thresh  = self.cfg.get("SCALP_SLOPE_THRESHOLD", 0.20)
        slope   = self._calc_slope(closes, n=n)
        if ma5 > ma10 > ma20:
            return "WATERFALL_UP"   if slope >  thresh else "UP"
        if ma5 < ma10 < ma20:
            return "WATERFALL_DOWN" if slope < -thresh else "DOWN"
        return "FLAT"

    def _get_signal(self, buf: list, cp: float, trend: str) -> tuple[str | None, float, str]:
        """
        第三层：根据趋势决定入场信号
        返回 (direction, trigger_pct, signal_label)

        WATERFALL_UP / UP   → 回调买多（顺势）
        WATERFALL_DOWN / DOWN → 反弹做空（顺势）
        FLAT                → 偏离均线做反转

        瀑布状态（WATERFALL_*）：只允许顺势方向，禁止逆势。
        """
        cfg = self.cfg
        # 统一处理瀑布与普通趋势：瀑布只是信号标签不同
        is_waterfall    = trend.startswith("WATERFALL")
        effective_trend = trend.replace("WATERFALL_UP", "UP").replace("WATERFALL_DOWN", "DOWN")

        pullback_pct    = cfg.get("SCALP_PULLBACK_PCT",    2.5)
        mean_revert_pct = cfg.get("SCALP_MEAN_REVERT_PCT", 7.0)

        if effective_trend == "UP":
            if not cfg.get("SCALP_ENABLE_LONG", True):
                return None, 0.0, ""
            local_high = max(k["c"] for k in buf[-5:])
            if local_high <= 0:
                return None, 0.0, ""
            drop_pct = (local_high - cp) / local_high * 100
            if drop_pct < pullback_pct:
                return None, 0.0, ""
            if len(buf) >= 7:
                vol_recent = sum(k["q"] for k in buf[-2:]) / 2
                vol_upmove = sum(k["q"] for k in buf[-7:-2]) / 5
                if vol_upmove > 0 and vol_recent > vol_upmove * 1.8:
                    return None, 0.0, ""
            label = "飞升追多" if is_waterfall else "上行回调买"
            return "LONG", drop_pct, label

        if effective_trend == "DOWN":
            if not cfg.get("SCALP_ENABLE_SHORT", True):
                return None, 0.0, ""
            local_low = min(k["c"] for k in buf[-5:])
            if local_low <= 0:
                return None, 0.0, ""
            rise_pct = (cp - local_low) / local_low * 100
            if rise_pct < pullback_pct:
                return None, 0.0, ""
            if len(buf) >= 7:
                vol_recent   = sum(k["q"] for k in buf[-2:]) / 2
                vol_downmove = sum(k["q"] for k in buf[-7:-2]) / 5
                if vol_downmove > 0 and vol_recent > vol_downmove * 1.8:
                    return None, 0.0, ""
            label = "瀑布追空" if is_waterfall else "下行反弹空"
            return "SHORT", rise_pct, label

        # FLAT：均线偏离做反转（瀑布状态不会到达这里）
        if len(buf) < 20:
            return None, 0.0, ""
        ma20 = sum(k["c"] for k in buf[-20:]) / 20
        if ma20 <= 0:
            return None, 0.0, ""
        dev = (cp - ma20) / ma20 * 100
        if dev > mean_revert_pct and cfg.get("SCALP_ENABLE_SHORT", True):
            return "SHORT", dev, "超买反转空"
        if dev < -mean_revert_pct and cfg.get("SCALP_ENABLE_LONG", True):
            return "LONG", abs(dev), "超卖反转多"
        return None, 0.0, ""

    # ─── 主控：三层串联 ────────────────────────────────────────────────────────

    async def _check_momentum(self, symbol: str, realtime_price: float | None = None) -> None:
        """
        realtime_price 非空时：K线还没关闭，用当前价实时检测（每5秒最多触发一次）
        realtime_price 为空时 ：标准闭合K线检测
        """
        cfg = self.cfg
        if not cfg.get("SCALP_ENABLED", False):
            return
        if symbol in self.open_positions:
            return

        # 冷却（默认关闭；SCALP_SL_COOLDOWN_MINUTES > 0 时启用）
        # 额外：若斜率方向完全反转，可提前解冻（避免错过真正的趋势转换机会）
        cooldown_mins = cfg.get("SCALP_SL_COOLDOWN_MINUTES", 0)
        if symbol in self.cooldown_symbols:
            if cooldown_mins > 0:
                elapsed = (time.monotonic() - self.cooldown_symbols[symbol]) / 60
                if elapsed < cooldown_mins:
                    closes      = [k["c"] for k in buf]
                    cur_slope   = self._calc_slope(closes)
                    saved_slope = self.slope_at_cooldown.get(symbol, 0.0)
                    slope_reversed = (saved_slope < 0 < cur_slope) or (cur_slope < 0 < saved_slope)
                    if not slope_reversed:
                        return
                    logger.info("⚡ [%s] 斜率反转 (%.3f→%.3f)，提前解除冷却", symbol, saved_slope, cur_slope)
            del self.cooldown_symbols[symbol]
            self.slope_at_cooldown.pop(symbol, None)

        if len(self.open_positions) >= cfg.get("SCALP_MAX_POSITIONS", 3):
            return

        buf = self.kline_buffer.get(symbol, [])
        if len(buf) < 20:  # MA20 最少需要20根K线
            return

        is_realtime = realtime_price is not None
        cp = realtime_price if is_realtime else buf[-1]["c"]

        self._fstat["checked"] += 1

        # ── 第一层：这币活跃吗？──────────────────────────────────────────
        if not self._is_coin_active(buf, cp):
            self._fstat["no_active"] += 1
            self._maybe_print_fstat()
            return

        # ── 第二层：当前趋势方向？────────────────────────────────────────
        trend = self._detect_trend(buf)

        # ── 第三层：有没有入场信号？──────────────────────────────────────
        direction, trigger_pct, label = self._get_signal(buf, cp, trend)
        if not direction:
            self._fstat["no_signal"] += 1
            self._maybe_print_fstat()
            return

        # ── BTC 大盘方向过滤 ──────────────────────────────────────────────
        if not self._btc_guard(direction):
            self._fstat["btc_guard"] += 1
            self._maybe_print_fstat()
            return

        # ── Taker 比例过滤 ────────────────────────────────────────────────
        base_min  = cfg.get("SCALP_TAKER_RATIO_MIN", 0.55)
        taker_min = (base_min - 0.08) if "回调" in label or "反弹" in label else base_min
        taker_min = max(0.42, taker_min)

        taker_data  = buf[-3:] if len(buf) >= 3 else buf
        total_q     = sum(k["q"] for k in taker_data)
        total_qb    = sum(k["Q"] for k in taker_data)
        taker_ratio = total_qb / total_q if total_q > 0 else 0.5

        if direction == "LONG" and taker_ratio < taker_min:
            self._fstat["taker"] += 1
            if not is_realtime:
                logger.info("⚡ [%s] [%s] Taker买入%.1f%% 不足(需≥%.1f%%)，跳过",
                            symbol, label, taker_ratio * 100, taker_min * 100)
            self._maybe_print_fstat()
            return
        if direction == "SHORT" and taker_ratio > (1 - taker_min):
            self._fstat["taker"] += 1
            if not is_realtime:
                logger.info("⚡ [%s] [%s] Taker卖出%.1f%% 不足(需≥%.1f%%)，跳过",
                            symbol, label, (1 - taker_ratio) * 100, taker_min * 100)
            self._maybe_print_fstat()
            return

        # ── VWAP 过度追高防护（仅对非均线反转的做多生效）────────────────
        if direction == "LONG" and "反转" not in label and len(buf) >= 10:
            vwap    = self._calc_vwap(buf)
            dev_pct = (cp - vwap) / vwap * 100 if vwap > 0 else 0
            vwap_max = cfg.get("SCALP_VWAP_MAX_DEV", 5.0)
            if dev_pct > vwap_max:
                self._fstat["vwap"] += 1
                if not is_realtime:
                    logger.info("⚡ [%s] 价格偏离VWAP +%.1f%%，拒绝追高", symbol, dev_pct)
                self._maybe_print_fstat()
                return

        # ── MarketHub: 基差过滤 + Taker趋势辅助确认 ─────────────────────
        basis_pct = hub.basis(symbol)
        if direction == "LONG" and basis_pct > 2.0:
            self._fstat["hub"] += 1
            if not is_realtime:
                logger.info("⚡ [%s] 基差+%.2f%% 升水过高，拒绝追多", symbol, basis_pct)
            self._maybe_print_fstat()
            return
        if direction == "SHORT" and basis_pct < -1.5:
            self._fstat["hub"] += 1
            if not is_realtime:
                logger.info("⚡ [%s] 基差%.2f%% 贴水过深，空头已拥挤，跳过", symbol, basis_pct)
            self._maybe_print_fstat()
            return

        hub_taker = hub.taker(symbol)
        hub_trend = hub.taker_trend(symbol)
        if direction == "LONG" and hub_taker > 0 and hub_taker < 0.40 and hub_trend == "falling":
            self._fstat["hub"] += 1
            if not is_realtime:
                logger.info("⚡ [%s] Hub Taker买入仅%.0f%%且趋势下降，跳过多单", symbol, hub_taker * 100)
            self._maybe_print_fstat()
            return
        if direction == "SHORT" and hub_taker > 0 and hub_taker > 0.60 and hub_trend == "rising":
            self._fstat["hub"] += 1
            if not is_realtime:
                logger.info("⚡ [%s] Hub Taker买入%.0f%%且趋势上升，跳过空单", symbol, hub_taker * 100)
            self._maybe_print_fstat()
            return

        self._fstat["passed"] += 1
        src = "实时" if is_realtime else ""
        logger.info("⚡ [%s] %s[%s|%s趋势] %.2f%% → %s | Taker%.0f%%",
                    symbol, src, label, trend, trigger_pct, direction, taker_ratio * 100)

        # 瀑布趋势：高置信度顺势信号，直接入场
        # 实时信号：无法等待K线关闭，直接入场
        # 右侧确认关闭：直接入场
        is_waterfall    = trend.startswith("WATERFALL")
        confirm_enabled = cfg.get("SCALP_CONFIRM_ENABLED", True)

        if is_waterfall or is_realtime or not confirm_enabled:
            await self._execute_entry(symbol, direction, trigger_pct, label)
        elif symbol not in self.pending_entries:
            self.pending_entries[symbol] = {
                "direction":      direction,
                "trigger_pct":    trigger_pct,
                "label":          label,
                "ref_price":      cp,
                "candles_waited": 0,
            }
            logger.info("⚡ [%s] 📋 [%s] 触发阈值，等待右侧K线确认...", symbol, label)

    # ─── 右侧确认 ─────────────────────────────────────────────────────────────

    async def _check_pending_entry(self, symbol: str, buf: list) -> None:
        """
        右侧入场确认（每根闭合K线调用一次）
        确认条件（满足任一即可）：
          A. 价格未创新低/高（止跌/止涨） 且 Taker 买卖方向改善
          B. 当前收盘价站上/站下 EMA5（均线确认方向）
        超过 3 根K线未确认 → 信号作废
        """
        if symbol in self.open_positions:
            self.pending_entries.pop(symbol, None)
            return

        pending = self.pending_entries.get(symbol)
        if not pending:
            return

        pending["candles_waited"] = pending.get("candles_waited", 0) + 1
        if pending["candles_waited"] > 3:
            logger.debug("⚡ [%s] 右侧确认超时 (3根K线内未满足条件)，信号作废", symbol)
            del self.pending_entries[symbol]
            return

        if len(buf) < 5:
            return

        cp        = buf[-1]["c"]
        direction = pending["direction"]
        ref_price = pending["ref_price"]

        ema5 = sum(k["c"] for k in buf[-5:]) / 5

        # Taker 趋势：比较最近两根已闭合K线
        if len(buf) >= 3:
            taker_prev = buf[-3]["Q"] / (buf[-3]["q"] or 1)
            taker_curr = buf[-2]["Q"] / (buf[-2]["q"] or 1)
        else:
            taker_prev = taker_curr = 0.5

        if direction == "LONG":
            no_new_low      = cp >= ref_price * 0.997       # 未创新低
            taker_improving = taker_curr > taker_prev       # 买盘回升
            above_ema5      = cp >= ema5                    # 站上 EMA5
            confirmed = (no_new_low and taker_improving) or above_ema5
        else:
            no_new_high     = cp <= ref_price * 1.003       # 未创新高
            taker_improving = taker_curr < taker_prev       # 卖盘回升
            below_ema5      = cp <= ema5                    # 站下 EMA5
            confirmed = (no_new_high and taker_improving) or below_ema5

        if confirmed:
            logger.info("⚡ [%s] ✅ 右侧确认 [%s] (第%d根K线)",
                        symbol, pending["label"], pending["candles_waited"])
            del self.pending_entries[symbol]
            await self._execute_entry(symbol, direction, pending["trigger_pct"], pending["label"])

    # ─── 开仓 ──────────────────────────────────────────────────────────────────

    async def _execute_entry(self, symbol: str, direction: str, trigger_pct: float, signal_label: str = "") -> None:
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

        leverage      = cfg.get("SCALP_LEVERAGE",     10)
        position_usdt = cfg.get("SCALP_POSITION_USDT", 10.0)

        # ── SL/TP 以"保证金%"为单位，直观易懂 ──────────────────────────────
        # 例：SL_PCT=10 意味着最多亏损保证金的10%（10U保证金 → 亏1U）
        # 实际价格变动幅度 = 保证金% ÷ 杠杆倍数
        sl_margin_pct  = cfg.get("SCALP_STOP_LOSS_PCT", 10.0)   # 亏多少%保证金止损
        tp1_margin_pct = cfg.get("SCALP_TP1_PCT",       10.0)   # 赚多少%保证金触发TP1
        tp2_margin_pct = cfg.get("SCALP_TP2_PCT",       30.0)   # 赚多少%保证金触发TP2
        sl_pct  = sl_margin_pct  / leverage   # 换算成价格变动%
        tp1_pct = tp1_margin_pct / leverage
        tp2_pct = tp2_margin_pct / leverage

        if direction == "LONG":
            sl_price  = entry_price * (1 - sl_pct  / 100)
            tp1_price = entry_price * (1 + tp1_pct / 100)
            tp2_price = entry_price * (1 + tp2_pct / 100)
        else:
            sl_price  = entry_price * (1 + sl_pct  / 100)
            tp1_price = entry_price * (1 - tp1_pct / 100)
            tp2_price = entry_price * (1 - tp2_pct / 100)

        base_signal = {
            "type":         "scalp",
            "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":       symbol,
            "direction":    direction,
            "signal_label": signal_label,
            "trigger_pct":  round(trigger_pct, 2),
            "entry_price":  round(entry_price, 8),
            "sl_price":     round(sl_price, 8),
            "tp1_price":    round(tp1_price, 8),
            "tp2_price":    round(tp2_price, 8),
        }

        if not cfg.get("SCALP_AUTO_TRADE", False):
            add_scalp_signal({**base_signal, "auto_traded": False, "paper": False})
            logger.info("⚡ [%s] 信号发出 (自动交易关闭，未实际开仓)", symbol)
            return

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
                signal_label       = signal_label,
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
            signal_label       = signal_label,
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

    def _record_scalp_trade(self, pos, exit_price: float, close_reason: str, extra_pnl: float = 0.0) -> None:
        total_pnl = pos.realized_pnl + extra_pnl
        pnl_pct   = total_pnl / (pos.entry_price * pos.quantity / self.cfg.get("SCALP_LEVERAGE", 10)) * 100 \
                    if pos.entry_price > 0 and pos.quantity > 0 else 0.0
        add_scalp_trade({
            "symbol":       pos.symbol,
            "direction":    pos.direction,
            "entry_price":  round(pos.entry_price, 8),
            "exit_price":   round(exit_price, 8),
            "entry_time":   pos.entry_time,
            "exit_time":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pnl_usdt":     round(total_pnl, 4),
            "pnl_pct":      round(pnl_pct, 2),
            "close_reason": close_reason,
            "paper":        pos.paper,
            "leverage":     self.cfg.get("SCALP_LEVERAGE", 10),
            "quantity":     round(pos.quantity, 6),
        })

    async def _check_tp_sl(self, symbol: str, price: float) -> None:
        pos = self.open_positions.get(symbol)
        if not pos:
            return

        cfg       = self.cfg
        # TP1: 赚够 tp1_margin_pct% 保证金 → 平掉 50% 仓位，SL移至成本（保本）
        # TP2: 赚够 tp2_margin_pct% 保证金 → 再平 30% 仓位，剩余20%开始追踪
        # TP3: 剩余20% 跟踪止损，每次创新高/低就收紧止损，回撤 trail_pct% 平仓
        tp1_ratio = cfg.get("SCALP_TP1_RATIO",    0.5)   # TP1 平掉50%
        tp2_ratio = cfg.get("SCALP_TP2_RATIO",    0.3)   # TP2 再平30%，剩20%追踪
        trail_pct = cfg.get("SCALP_TP3_TRAIL_PCT", 1.5)  # 追踪止损：回撤1.5%触发
        exit_s    = "SELL" if pos.direction == "LONG" else "BUY"
        auto      = cfg.get("SCALP_AUTO_TRADE", False)
        is_paper  = pos.paper
        tag       = "📋 " if is_paper else ""

        def _tp_hit(tp_price: float) -> bool:
            return (pos.direction == "LONG"  and price >= tp_price) or \
                   (pos.direction == "SHORT" and price <= tp_price)

        # ── TP1：先锁第一档利润，SL移位逻辑 ────────────────────────────
        # 顺势单(回调买/反弹空): SL移至入场价与TP1价格的中点 → 让趋势继续跑
        # 逆势单(均线反转):     SL移至成本价保本 → 严格风控
        if not pos.tp1_hit and _tp_hit(pos.tp1_price):
            pos.tp1_hit = True
            qty = pos.quantity * tp1_ratio
            tp1_pnl = qty * abs(price - pos.entry_price)
            is_trend_trade = "回调" in pos.signal_label or "反弹" in pos.signal_label
            if is_trend_trade:
                # 顺势单：SL移至入场价与TP1之间的中点，给利润空间
                new_sl = (pos.entry_price + pos.tp1_price) / 2
            else:
                # 逆势单：严格保本
                new_sl = pos.entry_price
            if is_paper:
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp1_pnl
                pos.sl_price = new_sl
            elif auto:
                await self.trader.place_market_order(symbol, exit_s, qty)
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp1_pnl
                if pos.sl_order_id:
                    await self.trader.cancel_order(symbol, pos.sl_order_id)
                new_sl = await self.trader.place_stop_loss_order(symbol, exit_s, pos.entry_price)
                if new_sl and new_sl.get("orderId"):
                    pos.sl_order_id = new_sl["orderId"]
                    pos.sl_price    = new_sl
            pct_margin = tp1_pnl / (pos.entry_price * pos.quantity / cfg.get("SCALP_LEVERAGE", 10)) * 100
            sl_desc = "SL→中点保护" if is_trend_trade else "SL→成本保本"
            logger.info("⚡ [%s] %s🟡 TP1 @ %.6f | 锁定%.0f%%仓位 | 保证金+%.1f%% | %s",
                        symbol, tag, price, tp1_ratio * 100, pct_margin, sl_desc)
            set_scalp_position(symbol, pos.to_dict())

        # ── TP2：再锁30%，剩余20%开始追踪止损 ──────────────────────────
        elif pos.tp1_hit and not pos.tp2_hit and _tp_hit(pos.tp2_price):
            pos.tp2_hit = True
            qty = pos.quantity * tp2_ratio
            tp2_pnl = qty * abs(price - pos.entry_price)
            if is_paper:
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp2_pnl
            elif auto:
                await self.trader.place_market_order(symbol, exit_s, qty)
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp2_pnl
                if pos.quantity_remaining > 0:
                    activ = price * (1 - trail_pct / 200) if pos.direction == "LONG" \
                            else price * (1 + trail_pct / 200)
                    await self.trader.place_trailing_stop_order(
                        symbol, exit_s, activ, trail_pct, pos.quantity_remaining,
                    )
            pos.trail_ref_price = price  # 从当前价开始追踪
            margin_base = pos.entry_price * pos.quantity / cfg.get("SCALP_LEVERAGE", 10)
            pct_margin = (pos.realized_pnl + tp2_pnl) / margin_base * 100 if margin_base > 0 else 0.0
            logger.info("⚡ [%s] %s🟢 TP2 @ %.6f | 再锁30%%仓位 | 累计保证金+%.1f%% | 剩余20%%追踪止损中",
                        symbol, tag, price, pct_margin)
            set_scalp_position(symbol, pos.to_dict())

        # ── TP3：追踪止损更新（TP2命中后，跟随价格移动收紧止损）──────────
        if pos.tp2_hit and pos.quantity_remaining > 0 and pos.trail_ref_price > 0:
            if pos.direction == "SHORT" and price < pos.trail_ref_price:
                pos.trail_ref_price = price
                pos.sl_price = pos.trail_ref_price * (1 + trail_pct / 100)
            elif pos.direction == "LONG" and price > pos.trail_ref_price:
                pos.trail_ref_price = price
                pos.sl_price = pos.trail_ref_price * (1 - trail_pct / 100)

        # ── 止损检查（初始SL / 保本SL / TP3追踪SL 统一走这里）──────────
        sl_crossed = (pos.direction == "LONG"  and price <= pos.sl_price * 0.999) or \
                     (pos.direction == "SHORT" and price >= pos.sl_price * 1.001)
        if sl_crossed and symbol in self.open_positions:
            close_pnl = pos.quantity_remaining * (
                (price - pos.entry_price) if pos.direction == "LONG"
                else (pos.entry_price - price)
            )
            if pos.tp2_hit:
                reason = "TP3"
                logger.info("⚡ [%s] %s🏁 TP3追踪止损 @ %.6f | 剩余仓位锁利润平仓", symbol, tag, price)
            elif pos.tp1_hit:
                reason = "SL_保本"
                logger.info("⚡ [%s] %s🔵 SL_保本 @ %.6f | 回到成本价，保本出场", symbol, tag, price)
            else:
                reason = "SL"
                loss_margin_pct = abs(close_pnl + pos.realized_pnl) / \
                                  (pos.entry_price * pos.quantity / cfg.get("SCALP_LEVERAGE", 10)) * 100
                logger.info("⚡ [%s] %s🔴 SL @ %.6f | 保证金亏损 %.1f%%", symbol, tag, price, loss_margin_pct)
                self.cooldown_symbols[symbol] = time.monotonic()  # 止损后冷却，防连续踩坑
                closes = [k["c"] for k in self.kline_buffer.get(symbol, [])]
                self.slope_at_cooldown[symbol] = self._calc_slope(closes)
            self._record_scalp_trade(pos, price, reason, close_pnl)
            del self.open_positions[symbol]
            set_scalp_position(symbol, None)

    # ─── 心跳日志（每 5 分钟汇报一次运行状态）────────────────────────────────

    def _maybe_print_fstat(self) -> None:
        """每5分钟输出详细过滤统计，显示各层拦截率并给出调参建议"""
        now = time.monotonic()
        if now - self._fstat_ts < 300:
            return
        self._fstat_ts = now
        s   = self._fstat
        cfg = self.cfg
        n   = s["checked"] or 1

        def pct(k): return s[k] / n * 100

        # 判断哪一层是主要瓶颈
        bottleneck = max(
            ("no_active", pct("no_active")),
            ("no_signal", pct("no_signal")),
            key=lambda x: x[1]
        )[0]

        tip_active = (
            f"可降低 活跃振幅(当前{cfg.get('SCALP_ACTIVE_RANGE_PCT',3)}%)"
            if bottleneck == "no_active" else ""
        )
        tip_signal = (
            f"可降低 回调阈值(当前{cfg.get('SCALP_PULLBACK_PCT',1.5)}%) 或 "
            f"反转阈值(当前{cfg.get('SCALP_MEAN_REVERT_PCT',4.5)}%)"
            if bottleneck == "no_signal" else ""
        )

        lines = [
            f"⚡ 超短线过滤统计 (最近5分钟) ─────────────────────────────────",
            f"  总检测次数: {n}  (≈{n//300}次/秒, {len(self.monitored_symbols)}币)",
            f"  ❌ 振幅不足: {s['no_active']:>5}次 ({pct('no_active'):5.1f}%)"
            + (f"  ◄ 主瓶颈! {tip_active}" if tip_active else ""),
            f"  ❌ 无入场信号: {s['no_signal']:>4}次 ({pct('no_signal'):5.1f}%)"
            + (f"  ◄ 主瓶颈! {tip_signal}" if tip_signal else ""),
            f"  ❌ BTC大盘过滤: {s['btc_guard']:>3}次"
            + (f"  [可降低 BTC_GUARD_PCT(当前{cfg.get('BTC_GUARD_PCT',2)}%)]"
               if s["btc_guard"] > 3 else ""),
            f"  ❌ Taker比例: {s['taker']:>5}次"
            + (f"  [可降低 Taker下限(当前{cfg.get('SCALP_TAKER_RATIO_MIN',0.55)})]"
               if s["taker"] > 5 else ""),
            f"  ❌ VWAP追高: {s['vwap']:>5}次"
            + (f"  [可调高 VWAP_MAX_DEV(当前{cfg.get('SCALP_VWAP_MAX_DEV',5)}%)]"
               if s["vwap"] > 5 else ""),
            f"  ❌ Hub市场过滤: {s['hub']:>3}次",
            f"  ✅ 通过→触发开仓: {s['passed']}次"
            + (" ★" if s["passed"] > 0 else " (等待行情)"),
            f"  ────────────────────────────────────────────────────────────",
        ]
        logger.info("\n".join(lines))
        # 共享到 signals 模块供 Web 面板读取
        _signals_mod.scalp_filter_stats = {
            "checked":    s["checked"],
            "no_active":  s["no_active"],
            "no_signal":  s["no_signal"],
            "btc_guard":  s["btc_guard"],
            "taker":      s["taker"],
            "vwap":       s["vwap"],
            "hub":        s["hub"],
            "passed":     s["passed"],
            "bottleneck": bottleneck,
            "cfg_active_pct":   cfg.get("SCALP_ACTIVE_RANGE_PCT", 3),
            "cfg_pullback_pct": cfg.get("SCALP_PULLBACK_PCT", 1.5),
            "cfg_revert_pct":   cfg.get("SCALP_MEAN_REVERT_PCT", 4.5),
            "cfg_btc_guard":    cfg.get("BTC_GUARD_PCT", 2),
            "cfg_taker_min":    cfg.get("SCALP_TAKER_RATIO_MIN", 0.55),
            "cfg_vwap_dev":     cfg.get("SCALP_VWAP_MAX_DEV", 3),
            "updated_at":       time.time(),
        }
        for k in self._fstat:
            self._fstat[k] = 0

    async def _heartbeat_loop(self) -> None:
        await asyncio.sleep(30)  # 启动后稍等，让 WS 先连上
        while self.running:
            cfg        = self.cfg
            buffered   = sum(1 for b in self.kline_buffer.values() if len(b) >= 20)
            positions  = len(self.open_positions)
            paper_cnt  = sum(1 for p in self.open_positions.values() if p.paper)
            real_cnt   = positions - paper_cnt
            enabled    = cfg.get("SCALP_ENABLED", False)
            auto       = cfg.get("SCALP_AUTO_TRADE", False)
            paper_mode = cfg.get("SCALP_PAPER_TRADE", False)

            mode_str = "自动下单" if auto else ("模拟开仓" if paper_mode else "仅信号")
            logger.info(
                "⚡ 超短线心跳 | 策略%s | 监控%d个 | 已就绪%d个 | "
                "活跃仓位%d个(真实%d/模拟%d) | "
                "回调阈值%.1f%% 反转阈值%.1f%% 活跃振幅%.1f%% | 模式:%s",
                "开启" if enabled else "关闭",
                len(self.monitored_symbols), buffered,
                positions, real_cnt, paper_cnt,
                cfg.get("SCALP_PULLBACK_PCT", 1.5),
                cfg.get("SCALP_MEAN_REVERT_PCT", 4.5),
                cfg.get("SCALP_ACTIVE_RANGE_PCT", 3.0),
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
