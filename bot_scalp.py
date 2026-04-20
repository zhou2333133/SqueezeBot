"""
超短线动量猎杀机器人 V3.0 — Squeeze Hunter
只做两种高确定性信号：
  1. 轧空/轧多猎杀 (Squeeze Hunter): OI暴跌 + Taker爆买/卖 → 捕捉爆仓后的反向行情
  2. 动能突破 (Trend Breakout):  MA5>MA10>MA20 + 突破前高 + Taker确认 → 右侧顺势
架构：Binance WS (Tick驱动) + OI REST轮询(10s) + Surf新闻(5min急救平仓)
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

from config import config_manager
from market_hub import hub
import signals as _signals_mod
from signals import add_scalp_signal, set_scalp_position, add_scalp_trade
from trader import BinanceTrader

logger = logging.getLogger("bot_scalp")

_REST_BASE = "https://fapi.binance.com"
_WS_URL    = "wss://fstream.binance.com/ws"

# 大币静态白名单（不随日成交量漂移，OI阈值最低）
_MAJOR_SYMBOLS = frozenset({
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "TRXUSDT", "AVAXUSDT", "LINKUSDT",
})


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
    trail_ref_price:    float      = 0.0
    signal_label:       str        = ""
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
        self.open_positions:    dict[str, ScalpPosition] = {}
        self.kline_buffer:      dict[str, list]          = {}
        self.monitored_symbols: list[str]                = []
        self.observe_symbols:   dict[str, dict]          = {}
        self.candidate_symbols: list[str]                = []
        self.candidate_meta:    dict[str, dict]          = {}
        self.daily_loss_usdt:   float                    = 0.0
        self._daily_loss_date:  object                   = datetime.now(timezone.utc).date()
        self.running:           bool                     = False
        self.session:           aiohttp.ClientSession | None = None
        self.trader:            BinanceTrader | None         = None
        # OI 缓存：{sym: [(monotonic_ts, oi_value), ...]}，保留最近3分钟
        self._oi_cache:         dict[str, list]          = {}
        # 实时当前未闭合K线（随每个WS Tick更新）
        self._live_candle:      dict[str, dict]          = {}
        # 突破信号状态锁（每根K线收盘时重置为False）
        self._breakout_fired:   dict[str, bool]          = {}
        # 信号冷却：同一币5秒内不重复触发
        self._signal_cooldown:  dict[str, float]         = {}
        # 平均K线成交量（用于Taker比率噪声过滤）
        self._avg_vol:          dict[str, float]         = {}
        # 过滤统计（每5分钟输出）
        self._fstat:            dict[str, int]           = {
            "checked": 0, "no_candidate": 0, "oi_miss": 0,
            "btc_guard": 0, "cooldown": 0,
            "squeeze": 0, "breakout": 0, "passed": 0,
        }
        self._fstat_ts:         float                    = 0.0

    @property
    def cfg(self) -> dict:
        return config_manager.settings

    # ─── 启动 ──────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.running = True
        logger.info("⚡ 超短线机器人 V3.0 启动 (Squeeze Hunter)")
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                self.session = session
                self.trader  = BinanceTrader(session)
                await self.refresh_symbols()
                await self._do_refresh_candidates()
                await asyncio.gather(
                    self._ws_loop(),
                    self._poll_oi_loop(),
                    self._position_monitor_loop(),
                    self._heartbeat_loop(),
                    self._refresh_candidates_loop(),
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

    # ─── 候选币预筛选（每5分钟）───────────────────────────────────────────────

    async def _refresh_candidates_loop(self) -> None:
        while self.running:
            await asyncio.sleep(300)
            await self._do_refresh_candidates()
            await self._emergency_position_news_check()

    async def _do_refresh_candidates(self) -> None:
        """
        拉取24h行情筛选候选池（最多80个）：
          - 24h成交量 > 500万USDT
          - 涨跌幅 < -60% → 完全排除（rug/exploit概率高）
          - 涨跌幅偏置标记（SHORT_ONLY / LONG_ONLY / ANY）
        """
        cfg = self.cfg
        custom = cfg.get("SCALP_WATCHLIST", "").strip()
        if custom:
            syms = [s.strip().upper() for s in custom.split(",") if s.strip()]
            self.candidate_symbols = syms
            self.candidate_meta    = {s: {"change_24h": 0.0, "volume_24h": 0.0} for s in syms}
            return

        try:
            async with self.session.get(
                f"{_REST_BASE}/fapi/v1/ticker/24hr",
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.warning("⚡ 候选币刷新失败: HTTP %s", resp.status)
                    return
                tickers = await resp.json()
        except Exception as e:
            logger.warning("⚡ 候选币刷新异常: %s", e)
            return

        usdt = [t for t in tickers if str(t.get("symbol", "")).endswith("USDT")]
        usdt.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)

        limit    = cfg.get("SCALP_CANDIDATE_LIMIT", 80)
        min_vol  = 5_000_000.0
        candidates = {}
        excluded   = []

        for i, t in enumerate(usdt[:limit]):
            sym    = t["symbol"]
            change = float(t.get("priceChangePercent", 0))
            vol    = float(t.get("quoteVolume", 0))

            if vol < min_vol:
                excluded.append(f"{sym}(vol不足)")
                continue

            if change < -60:
                excluded.append(f"{sym}({change:+.0f}%崩跌)")
                continue
            elif change < -25:
                bias = "SHORT_ONLY"
            elif change > 150 or change > 60:
                bias = "LONG_ONLY"
            else:
                bias = "ANY"

            candidates[sym] = {
                "change_24h":     change,
                "volume_24h":     vol,
                "rank":           i + 1,
                "direction_bias": bias,
                "news_sentiment": self.candidate_meta.get(sym, {}).get("news_sentiment", "neutral"),
            }

        # 计算vol_surge（异动倍数，用于heartbeat展示）
        for sym, meta in candidates.items():
            buf = self.kline_buffer.get(sym, [])
            if len(buf) >= 10 and meta["volume_24h"] > 0:
                n = len(buf)
                vol_1h_est = sum(k["Q"] for k in buf) * (60 / n)
                vol_24h_1h = meta["volume_24h"] / 24
                meta["vol_surge"] = round(vol_1h_est / vol_24h_1h, 1) if vol_24h_1h > 0 else 1.0
            else:
                meta["vol_surge"] = 1.0

        old_set = set(self.candidate_symbols)
        new_set = set(candidates.keys())
        self.candidate_symbols = list(candidates.keys())
        self.candidate_meta    = candidates

        bias_cnt = {}
        for v in candidates.values():
            bias_cnt[v["direction_bias"]] = bias_cnt.get(v["direction_bias"], 0) + 1

        changes = len(new_set - old_set) + len(old_set - new_set)
        logger.info(
            "⚡ 候选币: %d个 | 偏置分布%s | 排除%d个 | 涨跌[%.1f%%~%.1f%%]%s",
            len(candidates),
            {k: v for k, v in bias_cnt.items() if k != "ANY"} or "全部ANY",
            len(excluded),
            min((v["change_24h"] for v in candidates.values()), default=0),
            max((v["change_24h"] for v in candidates.values()), default=0),
            f" | 变化{changes}个" if changes else "",
        )
        if excluded:
            logger.info("⚡ 排除: %s", ", ".join(excluded[:8]))

        await self._fetch_funding_rates(list(candidates.keys()))
        await self._surf_news_scan(list(candidates.keys()))

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
            params = [f"{s.lower()}@kline_1m" for s in self.monitored_symbols]
            for i, chunk in enumerate([params[j:j + 200] for j in range(0, len(params), 200)]):
                await ws.send_str(json.dumps({"method": "SUBSCRIBE", "params": chunk, "id": i + 1}))
                await asyncio.sleep(0.3)
            logger.info("⚡ WS 已连接，监控 %d 个币种", len(self.monitored_symbols))
            async for msg in ws:
                if not self.running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._on_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            if self.running:
                logger.warning("⚡ WS 连接关闭，即将重连...")

    async def _on_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        stream_data = data.get("data", data)
        if stream_data.get("e") != "kline":
            return

        k         = stream_data["k"]
        symbol    = k["s"]
        is_closed = k["x"]
        price     = float(k["c"])

        # 实时更新当前未闭合K线（h/l/taker随每个Tick更新）
        self._live_candle[symbol] = {
            "h":         float(k["h"]),
            "l":         float(k["l"]),
            "taker_buy": float(k["Q"]),
            "total_vol": float(k["q"]),
            "close":     price,
            "open":      float(k["o"]),
        }

        if is_closed:
            buf = self.kline_buffer.setdefault(symbol, [])
            buf.append({
                "o": float(k["o"]),
                "h": float(k["h"]),
                "l": float(k["l"]),
                "c": price,
                "q": float(k["q"]),
                "Q": float(k["Q"]),
            })
            if len(buf) > 60:
                buf.pop(0)
            # 更新平均K线成交量（最近10根，用于Taker噪声过滤）
            if len(buf) >= 2:
                self._avg_vol[symbol] = sum(k2["q"] for k2 in buf[-10:]) / min(10, len(buf))
            # 重置突破锁（每根新K线允许重新触发一次Breakout信号）
            self._breakout_fired[symbol] = False

        # 更新持仓实时价格 + 检查TP/SL
        if symbol in self.open_positions:
            self.open_positions[symbol].current_price = price
            set_scalp_position(symbol, self.open_positions[symbol].to_dict())
            await self._check_tp_sl(symbol, price)
            return

        # Tick级信号检测
        if not self.cfg.get("SCALP_ENABLED", False):
            return
        if self.candidate_symbols and symbol not in self.candidate_symbols:
            self._fstat["no_candidate"] += 1
            return
        if len(self.open_positions) >= self.cfg.get("SCALP_MAX_POSITIONS", 3):
            return

        # 观察模式：止损后等待趋势确认（只在K线收盘时更新计数）
        if symbol in self.observe_symbols:
            if is_closed:
                await self._check_observe_mode(symbol)
            return

        # 信号冷却（同一币5秒内不重复）
        now      = time.monotonic()
        cooldown = self.cfg.get("SIGNAL_COOLDOWN_SECONDS", 5)
        if now - self._signal_cooldown.get(symbol, 0) < cooldown:
            self._fstat["cooldown"] += 1
            return

        self._fstat["checked"] += 1
        await self._detect_signal_v3(symbol, price)

    # ─── OI 轮询（每10秒，维护3分钟缓存）────────────────────────────────────

    async def _poll_oi_loop(self) -> None:
        await asyncio.sleep(15)  # 等WS连接稳定后再开始
        sem = asyncio.Semaphore(20)

        async def _poll_one(sym: str) -> None:
            async with sem:
                try:
                    async with self.session.get(
                        f"{_REST_BASE}/fapi/v1/openInterest",
                        params={"symbol": sym},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            d  = await resp.json()
                            oi = float(d["openInterest"])
                            ts = time.monotonic()
                            cache = self._oi_cache.setdefault(sym, [])
                            cache.append((ts, oi))
                            cutoff = ts - 180  # 只保留最近3分钟
                            self._oi_cache[sym] = [(t, v) for t, v in cache if t >= cutoff]
                except Exception:
                    pass

        while self.running:
            interval = self.cfg.get("OI_POLL_INTERVAL", 10)
            await asyncio.sleep(interval)
            if self.candidate_symbols:
                await asyncio.gather(*[_poll_one(s) for s in self.candidate_symbols])

    # ─── Surf 新闻扫描 + 急救平仓 ─────────────────────────────────────────────

    async def _surf_news_scan(self, symbols: list[str]) -> None:
        """每5分钟批量扫描新闻，更新 candidate_meta[sym]['news_sentiment']"""
        from config import SURF_API_KEY
        if not SURF_API_KEY or SURF_API_KEY == "YOUR_SURF_API_KEY":
            return

        NEG = {"hack","exploit","rug","scam","delist","suspend","crash","fraud","lawsuit","ponzi"}
        POS = {"partnership","listing","upgrade","adoption","mainnet","launch","integration","backed"}
        NEGATIONS = {"not","no","never","successfully","patches","fixed","resolved","mitigated",
                     "prevents","avoids","avoided","blocked","halted","upgraded"}

        def _has_genuine_neg(text: str) -> bool:
            for kw in NEG:
                idx = text.find(kw)
                while idx != -1:
                    window = text[max(0, idx - 60): idx]
                    if not any(n in window for n in NEGATIONS):
                        return True
                    idx = text.find(kw, idx + len(kw))
            return False

        headers = {"Authorization": f"Bearer {SURF_API_KEY}"}

        async def _check(sym: str) -> None:
            base = sym.replace("USDT", "")
            try:
                async with self.session.get(
                    "https://api.asksurf.ai/v1/news/curated",
                    params={"q": base, "limit": 3},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as resp:
                    if resp.status != 200:
                        return
                    items = (await resp.json()).get("data", [])
                    if not items:
                        return
                    text = " ".join(i.get("title", "").lower() for i in items)
                    if _has_genuine_neg(text):
                        sentiment = "negative"
                    elif any(k in text for k in POS):
                        sentiment = "positive"
                    else:
                        return
                    if sym in self.candidate_meta:
                        old = self.candidate_meta[sym].get("news_sentiment", "neutral")
                        if old != sentiment:
                            logger.info("⚡ [%s] 📰 新闻情绪: %s → %s", sym, old, sentiment)
                        self.candidate_meta[sym]["news_sentiment"] = sentiment
            except Exception:
                pass

        sem = asyncio.Semaphore(8)
        async def bounded(s):
            async with sem:
                await _check(s)
        await asyncio.gather(*[bounded(s) for s in symbols])

    async def _surf_entry_check(self, symbol: str, direction: str, price: float) -> tuple[bool, int, str]:
        """仅极端行情（24h涨跌 > ±30%）调用 Surf AI 深度审查，普通行情直接放行"""
        from config import SURF_API_KEY
        if not SURF_API_KEY or SURF_API_KEY == "YOUR_SURF_API_KEY":
            return True, 100, "未配置"

        meta       = self.candidate_meta.get(symbol, {})
        change_24h = meta.get("change_24h", 0.0)
        vol_24h    = meta.get("volume_24h", 0.0)

        if abs(change_24h) < 30:
            return True, 100, "正常行情跳过"

        buf   = self.kline_buffer.get(symbol, [])
        slope = self._calc_slope([k["c"] for k in buf]) if len(buf) >= 6 else 0.0
        td    = buf[-3:] if len(buf) >= 3 else buf
        tq    = sum(k["q"] for k in td) or 1
        taker = sum(k["Q"] for k in td) / tq
        btc   = self.kline_buffer.get("BTCUSDT", [])
        btc5  = (btc[-1]["c"] - btc[-5]["o"]) / btc[-5]["o"] * 100 if len(btc) >= 5 else 0.0
        trend_desc = "bull" if (len(buf) >= 20 and
                                self._detect_trend(buf) in ("UP", "WATERFALL_UP")) else "bear/flat"
        prompt = (
            f"You are a crypto futures risk analyst. Evaluate opening a {direction} position on {symbol}.\n"
            f"Key data: price={price:.6f}, 24h_change={change_24h:+.1f}%, "
            f"24h_volume_usdt={vol_24h:,.0f}, trend={trend_desc}, slope={slope:.3f}%/candle, "
            f"taker_buy_ratio={taker:.2f}, btc_5min={btc5:+.2f}%\n"
            f"Answer these 3 questions:\n"
            f"1. Any rug pull, exploit, delisting, or major negative event for {symbol.replace('USDT','')}?\n"
            f"2. Is the price action dangerous for a {direction} trade?\n"
            f"3. Does momentum support {direction}?\n"
            f"Reply ONLY with JSON: "
            f'{{\"score\":0-100,\"risk\":\"LOW|MED|HIGH\",\"reason\":\"max 15 words\"}}'
        )
        try:
            headers = {"Authorization": f"Bearer {SURF_API_KEY}"}
            async with self.session.post(
                "https://api.asksurf.ai/v1/chat/completions",
                json={"model": "surf-1.5-turbo", "messages": [{"role": "user", "content": prompt}]},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    return True, 100, f"HTTP {resp.status}"
                data   = await resp.json()
                text   = data["choices"][0]["message"]["content"].strip()
                if "```" in text:
                    text = text.split("```")[1].lstrip("json").strip()
                result = json.loads(text)
                score  = max(0, min(100, int(result.get("score", 50))))
                risk   = result.get("risk", "?")
                reason = result.get("reason", "")
                ok     = score >= 50
                logger.info("⚡ [%s] 🤖 Surf score=%d [%s] %s → %s",
                            symbol, score, risk, reason, "✅入场" if ok else "❌拒绝")
                return ok, score, reason
        except json.JSONDecodeError as e:
            logger.warning("⚡ [%s] Surf解析失败(%s)，放行", symbol, e)
            return True, 100, "解析失败"
        except Exception as e:
            logger.warning("⚡ [%s] Surf异常: %s，放行", symbol, e)
            return True, 100, str(e)

    async def _fetch_funding_rates(self, symbols: list[str]) -> None:
        """拉取资金费率，标记极端负费率（轧空机会）"""
        try:
            async with self.session.get(
                f"{_REST_BASE}/fapi/v1/premiumIndex",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
            sym_set = set(symbols)
            squeeze_list = []
            for item in data:
                sym = item.get("symbol", "")
                if sym not in sym_set or sym not in self.candidate_meta:
                    continue
                fr = float(item.get("lastFundingRate", 0))
                self.candidate_meta[sym]["funding_rate"] = round(fr * 100, 4)
                if fr < -0.0005:
                    self.candidate_meta[sym]["fr_squeeze"] = True
                    squeeze_list.append(f"{sym}({fr*100:+.3f}%)")
                else:
                    self.candidate_meta[sym]["fr_squeeze"] = False
            if squeeze_list:
                logger.info("⚡ 💰 轧空机会(FR极负): %s", ", ".join(squeeze_list[:6]))
        except Exception as e:
            logger.debug("⚡ 资金费率获取失败: %s", e)

    async def _emergency_position_news_check(self) -> None:
        """每5分钟检查：持有多单且负面新闻 → 市价强平（一票否决）"""
        if not self.open_positions:
            return
        for symbol, pos in list(self.open_positions.items()):
            meta      = self.candidate_meta.get(symbol, {})
            sentiment = meta.get("news_sentiment", "neutral")
            conflict  = (pos.direction == "LONG"  and sentiment == "negative") or \
                        (pos.direction == "SHORT" and sentiment == "positive")
            if not conflict:
                continue
            buf   = self.kline_buffer.get(symbol, [])
            price = buf[-1]["c"] if buf else pos.current_price
            if price <= 0:
                continue
            close_pnl = pos.quantity_remaining * (
                (price - pos.entry_price) if pos.direction == "LONG"
                else (pos.entry_price - price)
            )
            emoji = "🚨" if sentiment == "negative" else "⚠️"
            logger.warning(
                "⚡ [%s] %s 紧急平仓！%s持仓遭遇%s新闻，市价逃生 @ %.6f",
                symbol, emoji, pos.direction, sentiment, price,
            )
            self._record_scalp_trade(pos, price, f"紧急平仓_{sentiment}新闻", close_pnl)
            if not pos.paper and self.trader and self.cfg.get("SCALP_AUTO_TRADE", False):
                await self.trader.cancel_all_orders(symbol)
                exit_side = "SELL" if pos.direction == "LONG" else "BUY"
                await self.trader.place_market_order(symbol, exit_side, pos.quantity_remaining)
            del self.open_positions[symbol]
            set_scalp_position(symbol, None)

    # ─── BTC 方向守卫 ──────────────────────────────────────────────────────────

    def _btc_guard(self, direction: str) -> bool:
        """BTC 5分钟方向过滤：急涨时不做空，急跌时不做多"""
        guard_pct = self.cfg.get("BTC_GUARD_PCT", 2.0)
        btc_buf   = self.kline_buffer.get("BTCUSDT", [])
        if len(btc_buf) < 5:
            return True
        btc_5m = (btc_buf[-1]["c"] - btc_buf[-5]["o"]) / btc_buf[-5]["o"] * 100
        if direction == "LONG"  and btc_5m < -guard_pct:
            return False
        if direction == "SHORT" and btc_5m >  guard_pct:
            return False
        return True

    # ─── 工具函数 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _calc_slope(closes: list[float], n: int = 6) -> float:
        """线性回归斜率（归一化为均价的%/根），用于TP/SL管理中的趋势强度判断"""
        if len(closes) < n:
            return 0.0
        pts    = closes[-n:]
        x_mean = (n - 1) / 2.0
        y_mean = sum(pts) / n
        if y_mean == 0:
            return 0.0
        num = sum((i - x_mean) * (pts[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        return (num / den) / y_mean * 100 if den > 0 else 0.0

    def _detect_trend(self, buf: list) -> str:
        """MA排列趋势判断（仅用于TP/SL管理，不用于入场信号）"""
        if len(buf) < 20:
            return "FLAT"
        closes = [k["c"] for k in buf]
        ma5  = sum(closes[-5:])  / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        slope  = self._calc_slope(closes)
        thresh = 0.15
        if ma5 > ma10 > ma20:
            return "WATERFALL_UP"   if slope >  thresh else "UP"
        if ma5 < ma10 < ma20:
            return "WATERFALL_DOWN" if slope < -thresh else "DOWN"
        return "FLAT"

    def _get_taker_ratio(self, symbol: str) -> float | None:
        """获取当前K线Taker买入比；量不足时回退到上一根K线（噪声过滤）"""
        live      = self._live_candle.get(symbol, {})
        total_vol = live.get("total_vol", 0.0)
        avg_vol   = self._avg_vol.get(symbol, 0.0)

        if total_vol > 0 and (avg_vol == 0 or total_vol >= avg_vol * 0.20):
            return live["taker_buy"] / total_vol

        # 量不足（K线刚开启），回退到最近闭合K线
        buf = self.kline_buffer.get(symbol, [])
        if buf:
            last = buf[-1]
            q = last["q"]
            return last["Q"] / q if q > 0 else None
        return None

    def _get_oi_change_pct(self, symbol: str) -> float | None:
        """计算最近3分钟OI变化%（负值=OI减少=爆仓踩踏）"""
        cache = self._oi_cache.get(symbol, [])
        if len(cache) < 2:
            return None
        oldest_oi = cache[0][1]
        newest_oi = cache[-1][1]
        if oldest_oi <= 0:
            return None
        return (newest_oi - oldest_oi) / oldest_oi * 100

    def _get_squeeze_oi_threshold(self, symbol: str) -> float:
        """按币种分层返回轧空触发所需的最小OI降幅%"""
        cfg = self.cfg
        if symbol in _MAJOR_SYMBOLS:
            return cfg.get("SQUEEZE_OI_DROP_MAJOR", 0.5)
        vol = self.candidate_meta.get(symbol, {}).get("volume_24h", 0.0)
        if vol >= 500_000_000:  # 24h成交量≥5亿→中型
            return cfg.get("SQUEEZE_OI_DROP_MID", 1.0)
        return cfg.get("SQUEEZE_OI_DROP_MEME", 1.5)

    # ─── 观察模式（止损后60分钟冷却）──────────────────────────────────────────

    async def _check_observe_mode(self, symbol: str) -> None:
        """K线收盘时更新观察计数；MA方向连续3根确认后解除冷却"""
        obs = self.observe_symbols.get(symbol)
        if not obs:
            return
        if time.monotonic() - obs["since"] > 3600:
            logger.info("⚡ [%s] 观察模式超时60分钟，自动解除", symbol)
            del self.observe_symbols[symbol]
            return
        buf = self.kline_buffer.get(symbol, [])
        if len(buf) < 20:
            return
        closes = [k["c"] for k in buf]
        ma5  = sum(closes[-5:])  / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        if   ma5 > ma10 > ma20: eff = "UP"
        elif ma5 < ma10 < ma20: eff = "DOWN"
        else:                    eff = "FLAT"

        if eff != "FLAT":
            if obs.get("last_trend") is None or eff == obs["last_trend"]:
                obs["count"]      = obs.get("count", 0) + 1
                obs["last_trend"] = eff
            else:
                obs["count"]      = 1
                obs["last_trend"] = eff
        else:
            obs["count"] = 0

        if obs["count"] >= 3:
            logger.info("⚡ [%s] 📊 观察结束：%s趋势连续%d根确认，允许重新入场",
                        symbol, eff, obs["count"])
            del self.observe_symbols[symbol]

    # ─── V3 信号检测（Tick级驱动）────────────────────────────────────────────

    async def _detect_signal_v3(self, symbol: str, price: float) -> None:
        """
        每条WS消息触发，三阶段判定：
          Phase 1: 轧空猎杀 (Squeeze Hunter) — 豁免24h偏置，不豁免Surf负面新闻
          Phase 2: 方向偏置门控（仅Trend Breakout受限）
          Phase 3: 动能突破 (Trend Breakout) — 每根K线只触发一次
        """
        cfg  = self.cfg
        meta = self.candidate_meta.get(symbol, {})
        buf  = self.kline_buffer.get(symbol, [])
        if len(buf) < 20:
            return

        news        = meta.get("news_sentiment", "neutral")
        taker_ratio = self._get_taker_ratio(symbol)
        if taker_ratio is None:
            return

        # ── Phase 1: 轧空猎杀（Surf负面新闻一票否决，24h偏置豁免）──────────
        if news != "negative":
            oi_change_pct = self._get_oi_change_pct(symbol)
            oi_threshold  = self._get_squeeze_oi_threshold(symbol)

            if oi_change_pct is not None and oi_change_pct <= -oi_threshold:
                wick_pct   = cfg.get("SQUEEZE_WICK_PCT", 1.0)
                sq_taker   = cfg.get("SQUEEZE_TAKER_MIN", 0.65)
                recent_3   = buf[-3:] if len(buf) >= 3 else buf
                recent_low  = min(k["l"] for k in recent_3)
                recent_high = max(k["h"] for k in recent_3)

                # 轧空猎杀做多：OI暴跌后价格从低点反弹 > wick_pct% 且 Taker爆买
                if (cfg.get("SCALP_ENABLE_LONG", True) and
                        price > recent_low * (1 + wick_pct / 100) and
                        taker_ratio >= sq_taker):
                    self._fstat["squeeze"] += 1
                    logger.info(
                        "⚡ [%s] 🔴 轧空猎杀: OI变化%.2f%%(阈值%.1f%%) | 反弹%.2f%% | Taker=%.0f%%",
                        symbol, oi_change_pct, oi_threshold,
                        (price - recent_low) / recent_low * 100, taker_ratio * 100,
                    )
                    if self._btc_guard("LONG"):
                        await self._execute_entry(symbol, "LONG", abs(oi_change_pct), "轧空猎杀多")
                        return
                    else:
                        self._fstat["btc_guard"] += 1

                # 轧多猎杀做空：OI暴跌后价格从高点回落 > wick_pct% 且 Taker爆卖
                if (cfg.get("SCALP_ENABLE_SHORT", True) and
                        price < recent_high * (1 - wick_pct / 100) and
                        (1 - taker_ratio) >= sq_taker):
                    self._fstat["squeeze"] += 1
                    logger.info(
                        "⚡ [%s] 🟢 轧多猎杀: OI变化%.2f%%(阈值%.1f%%) | 回落%.2f%% | Taker卖=%.0f%%",
                        symbol, oi_change_pct, oi_threshold,
                        (recent_high - price) / recent_high * 100, (1 - taker_ratio) * 100,
                    )
                    if self._btc_guard("SHORT"):
                        await self._execute_entry(symbol, "SHORT", abs(oi_change_pct), "轧多猎杀空")
                        return
                    else:
                        self._fstat["btc_guard"] += 1
            elif oi_change_pct is None:
                self._fstat["oi_miss"] += 1

        # ── Phase 2: 方向偏置门控（Trend Breakout专用）───────────────────────
        bias        = meta.get("direction_bias", "ANY")
        allow_long  = not (news == "negative" or bias == "SHORT_ONLY")
        allow_short = not (news == "positive" or bias == "LONG_ONLY")

        # ── Phase 3: 动能突破（每根K线只触发一次）────────────────────────────
        if self._breakout_fired.get(symbol, False):
            self._maybe_print_fstat()
            return

        closes   = [k["c"] for k in buf]
        ma5      = sum(closes[-5:])  / 5
        ma10     = sum(closes[-10:]) / 10
        ma20     = sum(closes[-20:]) / 20
        prev     = buf[-1]  # 最近闭合K线
        bo_taker = cfg.get("BREAKOUT_TAKER_MIN", 0.55)

        if allow_long and ma5 > ma10 > ma20 and price > prev["h"] and taker_ratio >= bo_taker:
            if self._btc_guard("LONG"):
                self._fstat["breakout"] += 1
                self._breakout_fired[symbol] = True
                bo_pct = (price - prev["h"]) / prev["h"] * 100
                logger.info("⚡ [%s] 🟡 动能突破多: 突破前高%.6f → %.6f (+%.3f%%) | Taker=%.0f%%",
                            symbol, prev["h"], price, bo_pct, taker_ratio * 100)
                await self._execute_entry(symbol, "LONG", bo_pct, "动能突破多")
                return
            else:
                self._fstat["btc_guard"] += 1

        if allow_short and ma5 < ma10 < ma20 and price < prev["l"] and (1 - taker_ratio) >= bo_taker:
            if self._btc_guard("SHORT"):
                self._fstat["breakout"] += 1
                self._breakout_fired[symbol] = True
                bo_pct = (prev["l"] - price) / prev["l"] * 100
                logger.info("⚡ [%s] 🟠 动能突破空: 跌破前低%.6f → %.6f (-%.3f%%) | Taker卖=%.0f%%",
                            symbol, prev["l"], price, bo_pct, (1 - taker_ratio) * 100)
                await self._execute_entry(symbol, "SHORT", bo_pct, "动能突破空")
                return
            else:
                self._fstat["btc_guard"] += 1

        self._maybe_print_fstat()

    # ─── 开仓 ──────────────────────────────────────────────────────────────────

    async def _execute_entry(self, symbol: str, direction: str,
                             trigger_pct: float, signal_label: str = "") -> None:
        cfg = self.cfg

        # 每日亏损熔断检查
        today = datetime.now(timezone.utc).date()
        if today > self._daily_loss_date:
            self.daily_loss_usdt  = 0.0
            self._daily_loss_date = today
            logger.info("⚡ 每日亏损计数已重置（新的一天）")
        max_daily = cfg.get("SCALP_MAX_DAILY_LOSS_USDT", 50.0)
        if self.daily_loss_usdt >= max_daily:
            logger.warning("⚡ [%s] 🔒 每日亏损熔断(已亏%.2fU ≥ %.0fU)，今日停止开仓",
                           symbol, self.daily_loss_usdt, max_daily)
            return

        # 更新信号冷却时间戳
        self._signal_cooldown[symbol] = time.monotonic()

        # 获取入场价（REST实时价）
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
        position_usdt = cfg.get("SCALP_POSITION_USDT", 50.0)
        buf_entry     = self.kline_buffer.get(symbol, [])

        # ── 结构止损 + 最大保证金硬帽 ──────────────────────────────────────
        use_dynamic      = cfg.get("SCALP_USE_DYNAMIC_SL", True)
        sl_margin_pct    = cfg.get("SCALP_STOP_LOSS_PCT", 15.0)
        max_sl_price_pct = sl_margin_pct / leverage  # 保证金%换算成价格%

        if use_dynamic and len(buf_entry) >= 5:
            if direction == "LONG":
                struct_low = min(k.get("l", k["c"]) for k in buf_entry[-5:])
                sl_price   = struct_low * 0.998
            else:
                struct_high = max(k.get("h", k["c"]) for k in buf_entry[-5:])
                sl_price    = struct_high * 1.002
            sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100
            if sl_dist_pct > max_sl_price_pct:
                sl_price = (entry_price * (1 - max_sl_price_pct / 100)
                            if direction == "LONG"
                            else entry_price * (1 + max_sl_price_pct / 100))
        else:
            sl_price = (entry_price * (1 - max_sl_price_pct / 100)
                        if direction == "LONG"
                        else entry_price * (1 + max_sl_price_pct / 100))

        sl_distance_pct = abs(entry_price - sl_price) / entry_price * 100

        # ── RR倍数TP ─────────────────────────────────────────────────────
        tp1_dist = sl_distance_pct * cfg.get("SCALP_TP1_RR", 1.5)
        tp2_dist = sl_distance_pct * cfg.get("SCALP_TP2_RR", 3.5)

        if direction == "LONG":
            tp1_price = entry_price * (1 + tp1_dist / 100)
            tp2_price = entry_price * (1 + tp2_dist / 100)
        else:
            tp1_price = entry_price * (1 - tp1_dist / 100)
            tp2_price = entry_price * (1 - tp2_dist / 100)

        # ── Surf AI 深度审查（仅极端行情调用）─────────────────────────────
        surf_ok, surf_score, surf_reason = await self._surf_entry_check(symbol, direction, entry_price)
        if not surf_ok:
            logger.info("⚡ [%s] ❌ Surf AI拦截(score=%d): %s", symbol, surf_score, surf_reason)
            return

        # ── 固定风险仓位计算 ──────────────────────────────────────────────
        risk_usdt    = cfg.get("SCALP_RISK_PER_TRADE_USDT", 5.0)
        quantity_risk = (risk_usdt / (entry_price * sl_distance_pct / 100)
                         if sl_distance_pct > 0
                         else position_usdt * leverage / entry_price)
        quantity_max = position_usdt * leverage / entry_price
        quantity     = min(quantity_risk, quantity_max)

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
        logger.info(
            "⚡ [%s] ✅ [%s] SL=%.2f%% TP1=%.2f%%(×%.1fR) TP2=%.2f%%(×%.1fR) | qty=%.4f",
            symbol, signal_label, sl_distance_pct, tp1_dist,
            tp1_dist / max(sl_distance_pct, 0.001), tp2_dist,
            tp2_dist / max(sl_distance_pct, 0.001), quantity,
        )
        self._fstat["passed"] += 1

        if not cfg.get("SCALP_AUTO_TRADE", False):
            add_scalp_signal({**base_signal, "auto_traded": False, "paper": False})
            logger.info("⚡ [%s] 信号发出 (自动交易关闭)", symbol)
            return

        side   = "BUY"  if direction == "LONG" else "SELL"
        exit_s = "SELL" if direction == "LONG" else "BUY"

        # ── 模拟开仓 ──────────────────────────────────────────────────────
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
            add_scalp_signal({**base_signal, "auto_traded": True, "paper": True,
                              "quantity": round(quantity, 6), "leverage": leverage})
            logger.info("⚡ [%s] 📋 模拟开仓 %s ×%.6f @ %.6f | SL %.6f | TP1 %.6f | TP2 %.6f",
                        symbol, direction, quantity, entry_price, sl_price, tp1_price, tp2_price)
            return

        # ── 真实开仓（IOC 限价单，防飞单追高）─────────────────────────────
        await self.trader.set_leverage(symbol, leverage)
        ioc_price  = entry_price * 1.003 if direction == "LONG" else entry_price * 0.997
        trade_resp = await self.trader.place_limit_ioc_order(symbol, side, quantity, ioc_price)
        if not trade_resp:
            logger.error("⚡ [%s] IOC限价单请求失败", symbol)
            return
        filled_qty = float(trade_resp.get("executedQty", 0))
        if filled_qty <= 0:
            logger.info("⚡ [%s] IOC未成交（行情飞跃超过0.3%%滑点），信号作废", symbol)
            return

        actual      = float(trade_resp.get("avgPrice") or entry_price)
        entry_price = actual if actual > 0 else entry_price
        quantity    = filled_qty  # 使用实际成交量

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
        add_scalp_signal({**base_signal, "auto_traded": True, "paper": False,
                          "quantity": round(quantity, 6), "leverage": leverage,
                          "order_id": trade_resp.get("orderId")})
        logger.info("⚡ [%s] 开仓成功 %s ×%.6f @ %.6f | SL %.6f | TP1 %.6f | TP2 %.6f",
                    symbol, direction, quantity, entry_price, sl_price, tp1_price, tp2_price)

    # ─── TP / SL 实时检查 ──────────────────────────────────────────────────────

    def _record_scalp_trade(self, pos, exit_price: float, close_reason: str, extra_pnl: float = 0.0) -> None:
        total_pnl = pos.realized_pnl + extra_pnl
        if total_pnl < 0:
            self.daily_loss_usdt = getattr(self, "daily_loss_usdt", 0.0) + abs(total_pnl)
        pnl_pct = (total_pnl / (pos.entry_price * pos.quantity / self.cfg.get("SCALP_LEVERAGE", 10)) * 100
                   if pos.entry_price > 0 and pos.quantity > 0 else 0.0)
        add_scalp_trade({
            "symbol":       pos.symbol,
            "direction":    pos.direction,
            "signal":       pos.signal_label,
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
        tp1_ratio = cfg.get("SCALP_TP1_RATIO",    0.5)
        tp2_ratio = cfg.get("SCALP_TP2_RATIO",    0.3)
        trail_pct = cfg.get("SCALP_TP3_TRAIL_PCT", 1.5)
        exit_s    = "SELL" if pos.direction == "LONG" else "BUY"
        auto      = cfg.get("SCALP_AUTO_TRADE", False)
        is_paper  = pos.paper
        tag       = "📋 " if is_paper else ""

        def _tp_hit(tp_price: float) -> bool:
            return (pos.direction == "LONG"  and price >= tp_price) or \
                   (pos.direction == "SHORT" and price <= tp_price)

        # ── 趋势反转提前止损（瀑布反向时减少损失）───────────────────────
        if not pos.tp1_hit:
            buf_live = self.kline_buffer.get(symbol, [])
            if len(buf_live) >= 20:
                live_trend = self._detect_trend(buf_live)
                waterfall_against = (
                    (pos.direction == "LONG"  and live_trend == "WATERFALL_DOWN") or
                    (pos.direction == "SHORT" and live_trend == "WATERFALL_UP")
                )
                if waterfall_against:
                    move_pct = abs(price - pos.entry_price) / pos.entry_price * 100
                    sl_dist  = abs(pos.sl_price - pos.entry_price) / pos.entry_price * 100
                    if move_pct < sl_dist * 0.65:
                        close_pnl = pos.quantity_remaining * (
                            (price - pos.entry_price) if pos.direction == "LONG"
                            else (pos.entry_price - price)
                        )
                        logger.info("⚡ [%s] %s⚡ 瀑布反转(%s)提前止损 @ %.6f",
                                    symbol, tag, live_trend, price)
                        self._record_scalp_trade(pos, price, "趋势反转", close_pnl)
                        del self.open_positions[symbol]
                        set_scalp_position(symbol, None)
                        self.observe_symbols[symbol] = {
                            "since": time.monotonic(), "last_trend": None, "count": 0,
                        }
                        return

        # ── TP1：锁利润50%，SL移至入场与TP1中点 ─────────────────────────
        if not pos.tp1_hit and _tp_hit(pos.tp1_price):
            pos.tp1_hit = True
            buf_tp   = self.kline_buffer.get(symbol, [])
            tp_trend = self._detect_trend(buf_tp) if len(buf_tp) >= 20 else "FLAT"
            # 飞升全仓追踪：趋势仍是瀑布方向时跳过减仓，全仓保本追踪大行情
            if (pos.direction == "LONG"  and tp_trend == "WATERFALL_UP") or \
               (pos.direction == "SHORT" and tp_trend == "WATERFALL_DOWN"):
                pos.sl_price        = (pos.entry_price + pos.tp1_price) / 2
                pos.tp2_hit         = True
                pos.trail_ref_price = price
                logger.info("⚡ [%s] %s🚀 飞升全仓追踪 @ %.6f (%s)，SL→入场-TP1中点",
                            symbol, tag, price, tp_trend)
                set_scalp_position(symbol, pos.to_dict())
                return
            qty     = pos.quantity * tp1_ratio
            tp1_pnl = qty * abs(price - pos.entry_price)
            new_sl  = (pos.entry_price + pos.tp1_price) / 2  # V3全部信号均为动量型
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
                sl_resp = await self.trader.place_stop_loss_order(symbol, exit_s, new_sl)
                if sl_resp and sl_resp.get("orderId"):
                    pos.sl_order_id = sl_resp["orderId"]
                    pos.sl_price    = new_sl
            pct_margin = tp1_pnl / (pos.entry_price * pos.quantity / cfg.get("SCALP_LEVERAGE", 10)) * 100
            logger.info("⚡ [%s] %s🟡 TP1 @ %.6f | 锁%.0f%%仓 | 保证金+%.1f%% | SL→中点保护",
                        symbol, tag, price, tp1_ratio * 100, pct_margin)
            set_scalp_position(symbol, pos.to_dict())

        # ── TP2：再锁30%，剩20%开始EMA5追踪 ─────────────────────────────
        elif pos.tp1_hit and not pos.tp2_hit and _tp_hit(pos.tp2_price):
            pos.tp2_hit = True
            qty     = pos.quantity * tp2_ratio
            tp2_pnl = qty * abs(price - pos.entry_price)
            if is_paper:
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp2_pnl
            elif auto:
                await self.trader.place_market_order(symbol, exit_s, qty)
                pos.quantity_remaining -= qty
                pos.realized_pnl += tp2_pnl
                if pos.quantity_remaining > 0:
                    activ = (price * (1 - trail_pct / 200) if pos.direction == "LONG"
                             else price * (1 + trail_pct / 200))
                    await self.trader.place_trailing_stop_order(
                        symbol, exit_s, activ, trail_pct, pos.quantity_remaining,
                    )
            pos.trail_ref_price = price
            margin_base = pos.entry_price * pos.quantity / cfg.get("SCALP_LEVERAGE", 10)
            pct_margin  = (pos.realized_pnl + tp2_pnl) / margin_base * 100 if margin_base > 0 else 0.0
            logger.info("⚡ [%s] %s🟢 TP2 @ %.6f | 再锁30%%仓 | 累计保证金+%.1f%% | 剩20%%EMA5追踪",
                        symbol, tag, price, pct_margin)
            set_scalp_position(symbol, pos.to_dict())

        # ── TP3：EMA5 + 固定%双轨追踪止损（只向有利方向棘轮）────────────
        if pos.tp2_hit and pos.quantity_remaining > 0 and pos.trail_ref_price > 0:
            if pos.direction == "LONG" and price > pos.trail_ref_price:
                pos.trail_ref_price = price
            elif pos.direction == "SHORT" and price < pos.trail_ref_price:
                pos.trail_ref_price = price

            buf_trail = self.kline_buffer.get(symbol, [])
            if len(buf_trail) >= 5:
                ema5     = sum(k["c"] for k in buf_trail[-5:]) / 5
                ema5_sl  = ema5 * 0.997 if pos.direction == "LONG" else ema5 * 1.003
                fixed_sl = (pos.trail_ref_price * (1 - trail_pct / 100)
                            if pos.direction == "LONG"
                            else pos.trail_ref_price * (1 + trail_pct / 100))
                if pos.direction == "LONG":
                    new_sl = max(ema5_sl, fixed_sl)
                    pos.sl_price = max(new_sl, pos.sl_price)  # 只棘轮上移
                else:
                    new_sl = min(ema5_sl, fixed_sl)
                    pos.sl_price = min(new_sl, pos.sl_price)  # 只棘轮下移
            else:
                if pos.direction == "LONG":
                    pos.sl_price = max(pos.trail_ref_price * (1 - trail_pct / 100), pos.sl_price)
                else:
                    pos.sl_price = min(pos.trail_ref_price * (1 + trail_pct / 100), pos.sl_price)

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
                logger.info("⚡ [%s] %s🏁 TP3追踪止损 @ %.6f", symbol, tag, price)
            elif pos.tp1_hit:
                reason = "SL_保本"
                logger.info("⚡ [%s] %s🔵 SL_保本 @ %.6f", symbol, tag, price)
            else:
                reason = "SL"
                loss_margin_pct = (abs(close_pnl + pos.realized_pnl) /
                                   (pos.entry_price * pos.quantity / cfg.get("SCALP_LEVERAGE", 10)) * 100)
                logger.info("⚡ [%s] %s🔴 SL @ %.6f | 保证金亏损 %.1f%%", symbol, tag, price, loss_margin_pct)
                self.observe_symbols[symbol] = {
                    "since": time.monotonic(), "last_trend": None, "count": 0,
                }
            self._record_scalp_trade(pos, price, reason, close_pnl)
            del self.open_positions[symbol]
            set_scalp_position(symbol, None)

    # ─── 过滤统计（每5分钟）───────────────────────────────────────────────────

    def _maybe_print_fstat(self) -> None:
        now = time.monotonic()
        if now - self._fstat_ts < 300:
            return
        self._fstat_ts = now
        s   = self._fstat
        cfg = self.cfg
        n   = s["checked"] or 1

        lines = [
            "⚡ V3.0 超短线过滤统计 (最近5分钟) ─────────────────────────────",
            f"  总检测次数: {n}  ({len(self.candidate_symbols)}个候选币)",
            f"  ❌ 非候选币跳过: {s['no_candidate']:>5}次",
            f"  ❌ 信号冷却中:   {s['cooldown']:>5}次",
            f"  ❌ OI数据未就绪: {s['oi_miss']:>4}次  (启动30秒后可用)",
            f"  ❌ BTC大盘过滤:  {s['btc_guard']:>4}次",
            f"  🔴 轧空/轧多信号: {s['squeeze']}次",
            f"  🟡 动能突破信号:  {s['breakout']}次",
            f"  ✅ 触发开仓:      {s['passed']}次" + (" ★" if s["passed"] > 0 else " (等待行情)"),
            f"  参数: SQ_OI大币={cfg.get('SQUEEZE_OI_DROP_MAJOR',0.5)}%"
            f" 中型={cfg.get('SQUEEZE_OI_DROP_MID',1.0)}%"
            f" Meme={cfg.get('SQUEEZE_OI_DROP_MEME',1.5)}%"
            f" | Taker轧空≥{cfg.get('SQUEEZE_TAKER_MIN',0.65):.0%}"
            f" 突破≥{cfg.get('BREAKOUT_TAKER_MIN',0.55):.0%}",
            "  ────────────────────────────────────────────────────────────",
        ]
        logger.info("\n".join(lines))
        _signals_mod.scalp_filter_stats = {
            "checked":      s["checked"],
            "no_candidate": s["no_candidate"],
            "oi_miss":      s["oi_miss"],
            "btc_guard":    s["btc_guard"],
            "cooldown":     s["cooldown"],
            "squeeze":      s["squeeze"],
            "breakout":     s["breakout"],
            "passed":       s["passed"],
            "cfg_sq_oi_major": cfg.get("SQUEEZE_OI_DROP_MAJOR", 0.5),
            "cfg_sq_oi_mid":   cfg.get("SQUEEZE_OI_DROP_MID",   1.0),
            "cfg_sq_oi_meme":  cfg.get("SQUEEZE_OI_DROP_MEME",  1.5),
            "cfg_sq_taker":    cfg.get("SQUEEZE_TAKER_MIN",      0.65),
            "cfg_bo_taker":    cfg.get("BREAKOUT_TAKER_MIN",     0.55),
            "updated_at":      time.time(),
        }
        for k in self._fstat:
            self._fstat[k] = 0

    # ─── 心跳日志（每5分钟）───────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        await asyncio.sleep(30)
        while self.running:
            cfg        = self.cfg
            buffered   = sum(1 for b in self.kline_buffer.values() if len(b) >= 20)
            positions  = len(self.open_positions)
            paper_cnt  = sum(1 for p in self.open_positions.values() if p.paper)
            real_cnt   = positions - paper_cnt
            oi_covered = sum(1 for s in self.candidate_symbols
                             if len(self._oi_cache.get(s, [])) >= 2)
            enabled    = cfg.get("SCALP_ENABLED", False)
            auto       = cfg.get("SCALP_AUTO_TRADE", False)
            paper_mode = cfg.get("SCALP_PAPER_TRADE", False)
            mode_str   = "自动下单" if auto else ("模拟开仓" if paper_mode else "仅信号")
            logger.info(
                "⚡ V3心跳 | 策略%s | 候选%d个 | OI覆盖%d个 | 已就绪%d个 | "
                "仓位%d个(真实%d/模拟%d) | 观察中%d个 | 模式:%s",
                "开启" if enabled else "关闭",
                len(self.candidate_symbols), oi_covered, buffered,
                positions, real_cnt, paper_cnt,
                len(self.observe_symbols), mode_str,
            )
            await asyncio.sleep(300)

    # ─── REST 仓位同步（防WS漏消息）──────────────────────────────────────────

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
