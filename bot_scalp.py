"""
超短线动量猎杀机器人 V3.0 — Squeeze Hunter
只做两种高确定性信号：
  1. 轧空/轧多猎杀 (Squeeze Hunter): OI暴跌 + Taker爆买/卖 → 捕捉爆仓后的反向行情
  2. 动能突破 (Trend Breakout):  MA5>MA10>MA20 + 突破前高 + Taker确认 → 右侧顺势
架构：Binance WS (Tick驱动) + OI REST轮询(10s) + 可选Surf新闻/AI风控
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp

from config import config_manager, configured_surf_keys
from market_hub import hub
import signals as _signals_mod
from scanner.candidates import get_sorted_candidates
from scanner.provider_metrics import record_provider_call, record_provider_skip
from scanner.sources.surf_api import (
    chat_completion as surf_chat_completion,
    fetch_news_feed as surf_fetch_news_feed,
    news_matches_symbol as surf_news_matches_symbol,
    project_terms as surf_project_terms,
    search_news as surf_search_news,
)
from signals import add_scalp_signal, set_scalp_position, add_scalp_trade
from trader import BinanceTrader
from watchlist import get_watch_item, is_symbol_blocked

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
    market_state:       str        = "UNKNOWN"
    tp1_ratio:          float      = 0.15
    tp2_ratio:          float      = 0.25
    trail_pct:          float      = 5.0
    structure_trail_bars: int      = 5
    time_stop_minutes:  float      = 30.0
    tp2_timeout_minutes: float     = 120.0
    paper:              bool       = False
    entry_time:         str        = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    entry_ts:           float      = field(default_factory=time.monotonic)
    sl_order_id:        int | None = None
    risk_usdt:          float      = 0.0
    realized_gross_pnl: float      = 0.0
    realized_pnl:       float      = 0.0
    fee_usdt:           float      = 0.0
    slippage_usdt:      float      = 0.0
    closed_quantity:    float      = 0.0
    current_price:      float      = 0.0
    max_favorable_pct:  float      = 0.0
    max_adverse_pct:    float      = 0.0
    max_favorable_time: str        = ""
    max_adverse_time:   str        = ""
    entry_context:      dict       = field(default_factory=dict)

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
            "signal_label":       self.signal_label,
            "market_state":       self.market_state,
            "tp1_ratio":          self.tp1_ratio,
            "tp2_ratio":          self.tp2_ratio,
            "risk_usdt":          round(self.risk_usdt, 4),
            "entry_context":      self.entry_context,
            "paper":              self.paper,
            "entry_time":         self.entry_time,
            "realized_pnl":       round(self.realized_pnl, 4),
            "realized_gross_pnl": round(self.realized_gross_pnl, 4),
            "fee_usdt":           round(self.fee_usdt, 4),
            "slippage_usdt":      round(self.slippage_usdt, 4),
            "mfe_pct":            round(self.max_favorable_pct, 3),
            "mae_pct":            round(self.max_adverse_pct, 3),
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
        self.daily_realized_r:  float                    = 0.0
        self.daily_peak_r:      float                    = 0.0
        self._daily_loss_date:  object                   = datetime.now(timezone.utc).date()
        self.symbol_ban_until:  dict[str, float]         = {}
        self._symbol_loss_log:  dict[str, list]          = {}
        self.running:           bool                     = False
        self.session:           aiohttp.ClientSession | None = None
        self.trader:            BinanceTrader | None         = None
        self._ws = None
        self._ws_lock = asyncio.Lock()
        self._subscribed_streams: set[str] = set()
        # OI 缓存：{sym: [(monotonic_ts, oi_value), ...]}，保留最近3分钟
        self._oi_cache:         dict[str, list]          = {}
        # 实时当前未闭合K线（随每个WS Tick更新）
        self._live_candle:      dict[str, dict]          = {}
        # 突破信号状态锁（每根K线收盘时重置为False）
        self._breakout_fired:   dict[str, bool]          = {}
        # 信号冷却：同一币短时间内不重复触发
        self._signal_cooldown:  dict[str, float]         = {}
        # 平均K线成交量（用于Taker比率噪声过滤）
        self._avg_vol:          dict[str, float]         = {}
        # 平仓后继续观察，用于判断卖飞/方向是否选对（不额外 REST 调用）
        self._post_exit_watch:   dict[str, list]          = {}
        # Surf 成本控制：后台新闻扫描按配置限频，默认关闭。
        self._last_surf_news_scan_at: float                = 0.0
        # OI暖机截止时间（首轮OI就绪后静默60秒，防信号井喷）
        self._oi_warmup_until:  float                    = 0.0
        # 过滤统计（每5分钟输出）
        self._fstat:            dict[str, int]           = {
            "checked": 0, "no_candidate": 0, "oi_miss": 0,
            "btc_guard": 0, "cooldown": 0,
            "symbol_banned": 0, "vol_miss": 0,
            "state_block": 0, "atr_block": 0, "manual_block": 0,
            "yaobi_block": 0, "premove_block": 0,
            "squeeze": 0, "breakout": 0, "passed": 0,
        }
        self._fstat_ts:         float                    = 0.0

    @property
    def cfg(self) -> dict:
        return config_manager.settings

    @staticmethod
    def _as_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_binance_perp_symbol(symbol: str) -> str:
        raw = "".join(ch for ch in str(symbol or "").upper().strip() if ch.isalnum())
        if not raw:
            return ""
        return raw if raw.endswith("USDT") else f"{raw}USDT"

    def _yaobi_context_from_candidate(self, c: dict) -> dict:
        return {
            "yaobi_context": True,
            "yaobi_symbol": c.get("symbol", ""),
            "yaobi_name": c.get("name", ""),
            "yaobi_score": int(c.get("score", 0) or 0),
            "yaobi_anomaly_score": int(c.get("anomaly_score", 0) or 0),
            "yaobi_category": c.get("category", ""),
            "yaobi_sources": list(c.get("sources", []) or []),
            "yaobi_signals": list(c.get("signals", []) or [])[:8],
            "yaobi_decision_action": c.get("decision_action", ""),
            "yaobi_decision_confidence": int(c.get("decision_confidence", 0) or 0),
            "yaobi_decision_reasons": list(c.get("decision_reasons", []) or [])[:5],
            "yaobi_decision_risks": list(c.get("decision_risks", []) or [])[:5],
            "yaobi_decision_note": c.get("decision_note", ""),
            "yaobi_sentiment_label": c.get("sentiment_label", ""),
            "yaobi_sentiment_score": int(c.get("sentiment_score", 0) or 0),
            "yaobi_sentiment_heat": int(c.get("sentiment_heat", 0) or 0),
            "yaobi_oi_trend_grade": c.get("oi_trend_grade", ""),
            "yaobi_oi_change_24h_pct": self._as_float(c.get("oi_change_24h_pct")),
            "yaobi_oi_change_3d_pct": self._as_float(c.get("oi_change_3d_pct")),
            "yaobi_oi_change_7d_pct": self._as_float(c.get("oi_change_7d_pct")),
            "yaobi_oi_acceleration": self._as_float(c.get("oi_acceleration")),
            "yaobi_ema_deviation_pct": self._as_float(c.get("ema_deviation_pct")),
            "yaobi_volume_ratio": self._as_float(c.get("volume_ratio"), 1.0),
            "yaobi_whale_long_ratio": self._as_float(c.get("whale_long_ratio"), 0.5),
            "yaobi_short_crowd_pct": self._as_float(c.get("short_crowd_pct"), 50.0),
            "yaobi_funding_rate_pct": self._as_float(c.get("funding_rate_pct")),
            "yaobi_retail_short_pct": self._as_float(c.get("retail_short_pct"), 50.0),
            "yaobi_okx_buy_ratio": self._as_float(c.get("okx_buy_ratio")),
            "yaobi_okx_large_trade_pct": self._as_float(c.get("okx_large_trade_pct")),
            "yaobi_okx_risk_level": int(c.get("okx_risk_level", 0) or 0),
            "yaobi_okx_top10_hold_pct": self._as_float(c.get("okx_top10_hold_pct")),
            "yaobi_okx_token_tags": list(c.get("okx_token_tags", []) or [])[:8],
            "yaobi_surf_ai_risk_level": c.get("surf_ai_risk_level", ""),
            "yaobi_market_filter_note": c.get("market_filter_note", ""),
            "yaobi_holder_signal": c.get("holder_signal", ""),
            "yaobi_chain": c.get("chain", ""),
            "yaobi_chain_id": c.get("chain_id", ""),
            "yaobi_address": c.get("address", ""),
            "yaobi_price_usd": self._as_float(c.get("price_usd")),
            "yaobi_price_change_1h": self._as_float(c.get("price_change_1h")),
            "yaobi_price_change_4h": self._as_float(c.get("price_change_4h")),
            "yaobi_price_change_24h": self._as_float(c.get("price_change_24h")),
            "yaobi_updated_at": c.get("updated_at", ""),
            "yaobi_found_at": c.get("found_at", ""),
        }

    def _load_yaobi_futures_context(self) -> dict[str, dict]:
        if not self.cfg.get("SCALP_USE_YAOBI_CONTEXT", True):
            return {}
        top_n = int(self.cfg.get("SCALP_YAOBI_CONTEXT_TOP_N", 30) or 0)
        if top_n <= 0:
            return {}

        try:
            items = get_sorted_candidates(min_score=0)
        except Exception as e:
            logger.debug("⚡ 妖币共享候选读取失败: %s", e)
            return {}

        min_score = int(self.cfg.get("SCALP_YAOBI_MIN_SCORE", 30) or 0)
        min_anomaly = int(self.cfg.get("SCALP_YAOBI_MIN_ANOMALY_SCORE", 35) or 0)
        monitored = set(self.monitored_symbols)
        selected: list[tuple[int, int, str, dict]] = []

        for c in items:
            if not c.get("has_futures"):
                continue
            symbol = self._to_binance_perp_symbol(c.get("symbol", ""))
            if not symbol or (monitored and symbol not in monitored):
                continue
            score = int(c.get("score", 0) or 0)
            anomaly = int(c.get("anomaly_score", 0) or 0)
            action = str(c.get("decision_action", "") or "")
            if score < min_score and anomaly < min_anomaly and action not in ("允许交易", "等待确认", "禁止交易"):
                continue
            selected.append((score, anomaly, symbol, c))

        selected.sort(key=lambda x: (x[0], x[1]), reverse=True)
        contexts: dict[str, dict] = {}
        for _, _, symbol, c in selected:
            if symbol in contexts:
                continue
            contexts[symbol] = self._yaobi_context_from_candidate(c)
            if len(contexts) >= top_n:
                break
        return contexts

    def _merge_yaobi_context(
        self,
        candidates: dict[str, dict],
        ticker_index: dict[str, dict] | None = None,
        allow_add: bool = True,
    ) -> dict:
        contexts = self._load_yaobi_futures_context()
        if not contexts:
            return {"available": 0, "merged": 0, "added": 0, "blocked": 0}

        ticker_index = ticker_index or {}
        merged = added = blocked = 0
        for symbol, ctx in contexts.items():
            meta = candidates.get(symbol)
            ticker = ticker_index.get(symbol, {})
            if meta is None:
                if not allow_add:
                    continue
                change = self._as_float(ticker.get("priceChangePercent"), ctx.get("yaobi_price_change_24h", 0.0))
                volume = self._as_float(ticker.get("quoteVolume"), 0.0)
                meta = {
                    "change_24h": change,
                    "volume_24h": volume,
                    "rank": len(candidates) + 1,
                    "direction_bias": "ANY",
                    "news_sentiment": "neutral",
                    "vol_surge": 1.0,
                    "last_price": self._as_float(ticker.get("lastPrice"), ctx.get("yaobi_price_usd", 0.0)),
                    "candidate_sources": ["yaobi_shared"],
                }
                candidates[symbol] = meta
                added += 1
            else:
                sources = set(meta.get("candidate_sources", ["binance_24h"]))
                sources.add("yaobi_shared")
                meta["candidate_sources"] = sorted(sources)
                merged += 1

            if ticker and not meta.get("last_price"):
                meta["last_price"] = self._as_float(ticker.get("lastPrice"))
            meta.update(ctx)
            if ctx.get("yaobi_decision_action") == "禁止交易":
                blocked += 1
        return {"available": len(contexts), "merged": merged, "added": added, "blocked": blocked}

    def _prepare_candidate_path_meta(self, candidates: dict[str, dict]) -> None:
        path_keys = (
            "scalp_candidate_seen_time",
            "scalp_candidate_seen_ts",
            "scalp_candidate_seen_price",
            "scalp_candidate_last_price",
            "scalp_candidate_max_up_pct",
            "scalp_candidate_max_down_pct",
            "scalp_candidate_elapsed_min",
        )
        for symbol, meta in candidates.items():
            prev = self.candidate_meta.get(symbol, {})
            for key in path_keys:
                if key in prev and key not in meta:
                    meta[key] = prev[key]
            if not meta.get("scalp_candidate_seen_price"):
                price = self._as_float(meta.get("last_price") or meta.get("yaobi_price_usd"))
                if price > 0:
                    meta["scalp_candidate_seen_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    meta["scalp_candidate_seen_ts"] = time.monotonic()
                    meta["scalp_candidate_seen_price"] = price
                    meta["scalp_candidate_last_price"] = price
                    meta["scalp_candidate_max_up_pct"] = 0.0
                    meta["scalp_candidate_max_down_pct"] = 0.0
                    meta["scalp_candidate_elapsed_min"] = 0.0

    def _update_candidate_path(self, symbol: str, price: float) -> None:
        if price <= 0:
            return
        meta = self.candidate_meta.get(symbol)
        if not meta:
            return
        if not meta.get("scalp_candidate_seen_price"):
            meta["scalp_candidate_seen_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            meta["scalp_candidate_seen_ts"] = time.monotonic()
            meta["scalp_candidate_seen_price"] = price
            meta["scalp_candidate_max_up_pct"] = 0.0
            meta["scalp_candidate_max_down_pct"] = 0.0

        seen_price = self._as_float(meta.get("scalp_candidate_seen_price"))
        if seen_price <= 0:
            return
        up_pct = (price - seen_price) / seen_price * 100
        down_pct = (seen_price - price) / seen_price * 100
        meta["scalp_candidate_last_price"] = round(price, 8)
        meta["scalp_candidate_max_up_pct"] = round(max(self._as_float(meta.get("scalp_candidate_max_up_pct")), up_pct), 4)
        meta["scalp_candidate_max_down_pct"] = round(max(self._as_float(meta.get("scalp_candidate_max_down_pct")), down_pct), 4)
        seen_ts = self._as_float(meta.get("scalp_candidate_seen_ts"))
        if seen_ts > 0:
            meta["scalp_candidate_elapsed_min"] = round((time.monotonic() - seen_ts) / 60, 2)

    def _yaobi_entry_guard(self, symbol: str, direction: str) -> tuple[bool, str]:
        if not self.cfg.get("SCALP_USE_YAOBI_CONTEXT", True):
            return True, ""
        meta = self.candidate_meta.get(symbol, {})
        if not meta.get("yaobi_context"):
            return True, ""

        action = str(meta.get("yaobi_decision_action") or "")
        if self.cfg.get("SCALP_YAOBI_BLOCK_DECISION_BAN", True) and action == "禁止交易":
            return False, f"妖币扫描决策=禁止交易: {meta.get('yaobi_decision_note') or meta.get('yaobi_decision_risks')}"
        if self.cfg.get("SCALP_YAOBI_BLOCK_WAIT_CONFIRM", True) and action == "等待确认":
            return False, f"妖币扫描决策=等待确认，仅观察不自动交易: {meta.get('yaobi_decision_note') or ''}"

        if self.cfg.get("SCALP_YAOBI_BLOCK_HIGH_RISK", True):
            if str(meta.get("yaobi_surf_ai_risk_level") or "").upper() == "HIGH":
                return False, f"Surf AI高风险: {meta.get('yaobi_decision_note') or ''}"
            if int(meta.get("yaobi_okx_risk_level", 0) or 0) >= 4:
                return False, f"OKX风险等级{meta.get('yaobi_okx_risk_level')}"

        if self.cfg.get("SCALP_YAOBI_FUNDING_OI_GUARD", True):
            grade = str(meta.get("yaobi_oi_trend_grade") or "").upper()
            oi24 = self._as_float(meta.get("yaobi_oi_change_24h_pct"))
            funding = self._as_float(
                meta.get("yaobi_funding_rate_pct"),
                self._as_float(meta.get("funding_rate")),
            )
            crowded_oi = (
                grade in {"S", "A"} or
                oi24 >= self.cfg.get("SCALP_YAOBI_OI_GUARD_MIN_24H_PCT", 50.0)
            )
            funding_extreme = self.cfg.get("SCALP_YAOBI_FUNDING_EXTREME_PCT", 0.05)
            if crowded_oi and direction == "SHORT" and funding <= -funding_extreme:
                return False, f"OI强增长/评级{grade or '-'} + 资金费率{funding:.4f}%偏空拥挤，禁止追空"
            if crowded_oi and direction == "LONG" and funding >= funding_extreme:
                return False, f"OI强增长/评级{grade or '-'} + 资金费率{funding:.4f}%偏多拥挤，禁止追多"

        if self.cfg.get("SCALP_YAOBI_DIRECTION_GUARD", False):
            sentiment = str(meta.get("yaobi_sentiment_label") or "").lower()
            if direction == "LONG" and sentiment == "bearish" and action != "允许交易":
                return False, "妖币情绪偏空，阻止动能多"
            if direction == "SHORT" and sentiment == "bullish" and action != "允许交易":
                return False, "妖币情绪偏多，阻止动能空"

        return True, ""

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
            candidates = {
                s: {
                    "change_24h": 0.0,
                    "volume_24h": 0.0,
                    "rank": i + 1,
                    "direction_bias": "ANY",
                    "news_sentiment": self.candidate_meta.get(s, {}).get("news_sentiment", "neutral"),
                    "candidate_sources": ["manual_watchlist"],
                }
                for i, s in enumerate(syms)
            }
            yb_stats = self._merge_yaobi_context(candidates, allow_add=False)
            self._prepare_candidate_path_meta(candidates)
            self.candidate_symbols = list(candidates.keys())
            self.candidate_meta    = candidates
            await self._sync_ws_subscriptions()
            if yb_stats["merged"]:
                logger.info("⚡ 妖币共享: 自选池已补充 %d 个候选上下文", yb_stats["merged"])
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
        ticker_index = {t.get("symbol"): t for t in usdt if t.get("symbol")}

        limit    = cfg.get("SCALP_CANDIDATE_LIMIT", 80)
        min_vol  = 5_000_000.0
        candidates = {}
        excluded   = []

        for i, t in enumerate(usdt[:limit]):
            sym    = t["symbol"]
            if not sym.isascii() or not sym.endswith("USDT"):
                continue
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
                "last_price":     float(t.get("lastPrice", 0) or 0),
                "candidate_sources": ["binance_24h"],
            }

        yb_stats = self._merge_yaobi_context(candidates, ticker_index)
        self._prepare_candidate_path_meta(candidates)

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
        await self._sync_ws_subscriptions()

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
        if yb_stats["available"]:
            logger.info(
                "⚡ 妖币共享: 可用%d个 | 合并%d个 | 新增%d个Binance合约候选 | 禁入标记%d个",
                yb_stats["available"], yb_stats["merged"], yb_stats["added"], yb_stats["blocked"],
            )

        await self._fetch_funding_rates(list(candidates.keys()))
        await self._surf_news_scan(list(candidates.keys()))

    # ─── WebSocket ─────────────────────────────────────────────────────────────

    def _desired_ws_streams(self) -> set[str]:
        symbols = set(self.candidate_symbols)
        symbols.update(self.open_positions.keys())
        symbols.update(self._post_exit_watch.keys())
        symbols.add("BTCUSDT")
        return {f"{s.lower()}@kline_1m" for s in symbols if s}

    async def _sync_ws_subscriptions(self) -> None:
        if not self._ws or self._ws.closed:
            return
        desired = self._desired_ws_streams()
        to_sub = sorted(desired - self._subscribed_streams)
        to_unsub = sorted(self._subscribed_streams - desired)
        try:
            async with self._ws_lock:
                for i, chunk in enumerate([to_unsub[j:j + 200] for j in range(0, len(to_unsub), 200)]):
                    await self._ws.send_str(json.dumps({"method": "UNSUBSCRIBE", "params": chunk, "id": 10_000 + i}))
                    await asyncio.sleep(0.1)
                for i, chunk in enumerate([to_sub[j:j + 200] for j in range(0, len(to_sub), 200)]):
                    await self._ws.send_str(json.dumps({"method": "SUBSCRIBE", "params": chunk, "id": 20_000 + i}))
                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.debug("⚡ WS订阅同步失败: %s", e)
            return
        self._subscribed_streams = desired
        if to_sub or to_unsub:
            logger.info("⚡ WS订阅同步 | 新增%d | 退订%d | 当前%d",
                        len(to_sub), len(to_unsub), len(self._subscribed_streams))

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
            self._ws = ws
            self._subscribed_streams = set()
            params = sorted(self._desired_ws_streams())
            for i, chunk in enumerate([params[j:j + 200] for j in range(0, len(params), 200)]):
                await ws.send_str(json.dumps({"method": "SUBSCRIBE", "params": chunk, "id": i + 1}))
                await asyncio.sleep(0.3)
            self._subscribed_streams = set(params)
            logger.info("⚡ WS 已连接，订阅 %d 个候选/持仓币种", len(self._subscribed_streams))
            async for msg in ws:
                if not self.running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._on_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            if self.running:
                logger.warning("⚡ WS 连接关闭，即将重连...")
            self._ws = None
            self._subscribed_streams = set()

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
            if len(buf) > 360:
                buf.pop(0)
            # 更新平均K线成交量（最近10根，用于Taker噪声过滤）
            if len(buf) >= 2:
                self._avg_vol[symbol] = sum(k2["q"] for k2 in buf[-10:]) / min(10, len(buf))
            # 重置突破锁（每根新K线允许重新触发一次Breakout信号）
            self._breakout_fired[symbol] = False

        # 平仓后的影子复盘：继续观察原方向是否还有空间
        self._update_post_exit_watch(symbol, price)
        # 候选池路径复盘：从进入超短线候选池开始记录短线最大上/下波动
        self._update_candidate_path(symbol, price)

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

        ban_until = self.symbol_ban_until.get(symbol, 0.0)
        if ban_until and time.time() < ban_until:
            self._fstat["symbol_banned"] += 1
            return
        if ban_until:
            self.symbol_ban_until.pop(symbol, None)

        if is_symbol_blocked(symbol):
            self._fstat["manual_block"] += 1
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
                # 首轮OI就绪后启动60秒暖机静默，防止OI缓存从空→满时信号井喷
                if self._oi_warmup_until == 0.0:
                    ready = sum(1 for s in self.candidate_symbols if self._oi_cache.get(s))
                    if ready >= len(self.candidate_symbols) * 0.8:
                        self._oi_warmup_until = time.monotonic() + 60
                        logger.info("⚡ OI暖机完成(%d/%d), 静默60秒防信号井喷",
                                    ready, len(self.candidate_symbols))

    # ─── Surf 新闻扫描 + 急救平仓 ─────────────────────────────────────────────

    async def _surf_news_scan(self, symbols: list[str]) -> None:
        """按配置低频扫描少量高风险币新闻，更新 candidate_meta[sym]['news_sentiment']。"""
        if not self.cfg.get("SCALP_SURF_NEWS_ENABLED", False):
            record_provider_skip("surf", "news/feed", "scalp_surf_news_disabled", items=len(symbols))
            return
        if not configured_surf_keys():
            record_provider_skip("surf", "news/feed", "missing_surf_api_key", items=len(symbols))
            return

        now = time.monotonic()
        interval = float(self.cfg.get("SCALP_SURF_NEWS_INTERVAL_MINUTES", 60) or 60) * 60
        if self._last_surf_news_scan_at and now - self._last_surf_news_scan_at < interval:
            record_provider_skip("surf", "news/feed", "scalp_surf_news_interval")
            return

        target_symbols = self._surf_news_target_symbols(symbols)
        if not target_symbols:
            record_provider_skip("surf", "news/feed", "scalp_surf_no_targets", items=len(symbols))
            return
        self._last_surf_news_scan_at = now

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

        def _chunks(rows: list[str], size: int) -> list[list[str]]:
            return [rows[i:i + size] for i in range(0, len(rows), size)]

        lookup_terms: list[str] = []
        fallback_searches = 0
        for sym in target_symbols:
            lookup_terms.extend(surf_project_terms(sym, self.candidate_meta.get(sym, {}).get("name", ""))[:2])
        lookup_terms = list(dict.fromkeys(lookup_terms))

        items: list[dict] = []
        for chunk in _chunks(lookup_terms, 25):
            items.extend(await surf_fetch_news_feed(self.session, projects=chunk, limit=50))
            await asyncio.sleep(0.05)
        if not items:
            return

        for sym in target_symbols:
            meta = self.candidate_meta.get(sym)
            if not meta:
                continue
            matched = [
                item for item in items
                if surf_news_matches_symbol(item, sym, meta.get("name", ""))
            ][:5]
            if (not matched
                    and fallback_searches < 10
                    and abs(float(meta.get("change_24h", 0.0) or 0.0)) >= 30):
                query = " ".join(surf_project_terms(sym, meta.get("name", ""))[:2])
                matched = await surf_search_news(self.session, query)
                fallback_searches += 1
            if not matched:
                continue
            text = " ".join((item.get("text") or "").lower() for item in matched)
            if _has_genuine_neg(text):
                sentiment = "negative"
            elif any(k in text for k in POS):
                sentiment = "positive"
            else:
                sentiment = "neutral"
            old = meta.get("news_sentiment", "neutral")
            if old != sentiment:
                logger.info("⚡ [%s] 📰 Surf新闻情绪: %s → %s (%d条)",
                            sym, old, sentiment, len(matched))
            meta["news_sentiment"] = sentiment
            meta["surf_news_titles"] = [item.get("title", "") for item in matched[:3]]

    def _surf_news_target_symbols(self, symbols: list[str]) -> list[str]:
        top_n = int(self.cfg.get("SCALP_SURF_NEWS_TOP_N", 8) or 8)
        open_set = set(self.open_positions.keys())

        def _priority(sym: str) -> tuple:
            meta = self.candidate_meta.get(sym, {})
            change = abs(float(meta.get("change_24h", 0.0) or 0.0))
            bias_score = 1 if meta.get("direction_bias") != "ANY" else 0
            fr_score = 1 if meta.get("fr_squeeze") else 0
            rank = int(meta.get("rank", 9999) or 9999)
            return (1 if sym in open_set else 0, fr_score, bias_score, change, -rank)

        eligible = [
            sym for sym in symbols
            if sym in open_set
            or abs(float(self.candidate_meta.get(sym, {}).get("change_24h", 0.0) or 0.0)) >= 30
            or self.candidate_meta.get(sym, {}).get("direction_bias") != "ANY"
            or self.candidate_meta.get(sym, {}).get("fr_squeeze")
        ]
        eligible.sort(key=_priority, reverse=True)
        return eligible[:max(1, top_n)]

    async def _surf_entry_check(self, symbol: str, direction: str, price: float) -> tuple[bool, int, str]:
        """仅极端行情（24h涨跌 > ±30%）调用 Surf AI 深度审查，普通行情直接放行"""
        if not self.cfg.get("SCALP_SURF_ENTRY_AI_ENABLED", False):
            record_provider_skip("surf", "chat/completions", "scalp_entry_ai_disabled")
            return True, 100, "Surf AI关闭"
        if not configured_surf_keys():
            record_provider_skip("surf", "chat/completions", "missing_surf_api_key")
            return True, 100, "未配置"

        meta       = self.candidate_meta.get(symbol, {})
        change_24h = meta.get("change_24h", 0.0)
        vol_24h    = meta.get("volume_24h", 0.0)

        min_abs_change = float(self.cfg.get("SCALP_SURF_ENTRY_AI_MIN_ABS_CHANGE", 80.0) or 80.0)
        if abs(change_24h) < min_abs_change:
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
            ok_resp, text, status = await surf_chat_completion(
                self.session,
                prompt,
                timeout_sec=18,
                reasoning_effort="low",
            )
            if not ok_resp:
                return True, 100, f"HTTP {status}"
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
            record_provider_call("surf", "chat/completions", False, status="json_decode", error=str(e))
            logger.warning("⚡ [%s] Surf解析失败(%s)，放行", symbol, e)
            return True, 100, "解析失败"
        except Exception as e:
            record_provider_call("surf", "chat/completions", False, status="exception", error=f"{type(e).__name__}: {e}")
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
            emoji = "🚨" if sentiment == "negative" else "⚠️"
            logger.warning(
                "⚡ [%s] %s 紧急平仓！%s持仓遭遇%s新闻，市价逃生 @ %.6f",
                symbol, emoji, pos.direction, sentiment, price,
            )
            if not pos.paper and self.trader and self.cfg.get("SCALP_AUTO_TRADE", False):
                exit_side = "SELL" if pos.direction == "LONG" else "BUY"
                resp = await self.trader.place_reduce_only_market_order(symbol, exit_side, pos.quantity_remaining)
                if not resp:
                    logger.error("⚡ [%s] 紧急平仓下单失败，保留本地仓位和保护单", symbol)
                    continue
                await self.trader.cancel_all_orders(symbol)
            self._record_scalp_trade(pos, price, f"紧急平仓_{sentiment}新闻")
            del self.open_positions[symbol]
            set_scalp_position(symbol, None)

    def _reset_daily_risk_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today <= self._daily_loss_date:
            return
        self.daily_loss_usdt = 0.0
        self.daily_realized_r = 0.0
        self.daily_peak_r = 0.0
        self._daily_loss_date = today
        self.symbol_ban_until.clear()
        self._symbol_loss_log.clear()
        logger.info("⚡ 每日风控计数已重置（UTC新的一天）")

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

    def _get_taker_ratio(self, symbol: str, min_vol_ratio: float | None = None) -> float | None:
        """获取当前K线Taker买入比；入场信号不再回退到上一根K线，避免逻辑错位。"""
        live      = self._live_candle.get(symbol, {})
        total_vol = live.get("total_vol", 0.0)
        buf       = self.kline_buffer.get(symbol, [])
        if len(buf) >= 5:
            avg_vol = sum(k["q"] for k in buf[-5:]) / 5
        else:
            avg_vol = self._avg_vol.get(symbol, 0.0)

        ratio_required = self.cfg.get("BREAKOUT_MIN_VOL_RATIO", 0.20) if min_vol_ratio is None else min_vol_ratio
        if total_vol > 0 and (avg_vol == 0 or total_vol >= avg_vol * ratio_required):
            return live["taker_buy"] / total_vol

        return None

    @staticmethod
    def _calc_atr_pct(buf: list, n: int = 14) -> float:
        if len(buf) < n + 1:
            return 0.0
        ranges = []
        rows = buf[-(n + 1):]
        for i in range(1, len(rows)):
            prev_close = rows[i - 1]["c"]
            high = rows[i]["h"]
            low = rows[i]["l"]
            ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        last_close = rows[-1]["c"]
        return (sum(ranges) / len(ranges)) / last_close * 100 if last_close > 0 and ranges else 0.0

    def _breakout_required_pct(self, buf: list, atr_pct: float | None = None) -> float:
        min_pct = self.cfg.get("BREAKOUT_MIN_PCT", 0.10)
        atr_pct = self._calc_atr_pct(buf) if atr_pct is None else atr_pct
        return max(min_pct, atr_pct * self.cfg.get("BREAKOUT_ATR_MULT", 0.5))

    def _breakout_atr_allowed(self, atr_pct: float) -> bool:
        atr_min = self.cfg.get("BREAKOUT_ATR_MIN_PCT", 0.0)
        atr_max = self.cfg.get("BREAKOUT_ATR_MAX_PCT", 0.0)
        if atr_min and atr_pct < atr_min:
            return False
        if atr_max and atr_pct > atr_max:
            return False
        return True

    def _recent_directional_move_pct(
        self,
        symbol: str,
        direction: str,
        lookback: int = 30,
        last_price: float | None = None,
    ) -> float:
        buf = self.kline_buffer.get(symbol, [])
        if len(buf) < lookback:
            return 0.0
        base = self._as_float(buf[-lookback]["c"])
        price = self._as_float(last_price, self._as_float(buf[-1]["c"]))
        if base <= 0 or price <= 0:
            return 0.0
        if direction == "LONG":
            return (price - base) / base * 100
        return (base - price) / base * 100

    def _breakout_premove_allowed(self, symbol: str, direction: str, price: float) -> tuple[bool, str]:
        max_pct = self.cfg.get("BREAKOUT_MAX_PREMOVE_30M_PCT", 0.0)
        if not max_pct:
            return True, ""
        move30 = self._recent_directional_move_pct(symbol, direction, 30, price)
        if move30 > max_pct:
            return False, f"30m同向已走{move30:.2f}% > {max_pct:.2f}%，避免突破追晚"
        return True, ""

    def _check_market_state(self, symbol: str, direction: str) -> str:
        buf = self.kline_buffer.get(symbol, [])
        if len(buf) < 60:
            return "UNKNOWN"

        closes = [k["c"] for k in buf]
        ema20 = sum(closes[-20:]) / 20
        ema60 = sum(closes[-60:]) / 60
        if ema60 <= 0:
            return "UNKNOWN"

        atr_pct = self._calc_atr_pct(buf) or 0.1
        ret60 = (closes[-1] - closes[-60]) / closes[-60] * 100 if closes[-60] else 0.0
        if len(closes) >= 120 and closes[-120] > 0:
            ret120 = (closes[-1] - closes[-120]) / closes[-120] * 100
        else:
            ret120 = ret60
        dist_ema60 = (closes[-1] - ema60) / ema60 * 100
        slope20 = self._calc_slope(closes, 20)
        change_24h = self.candidate_meta.get(symbol, {}).get("change_24h", 0.0)

        trend_up = ema20 > ema60 and slope20 > 0.02
        trend_down = ema20 < ema60 and slope20 < -0.02
        chop_band = max(atr_pct * 0.4, 0.20)
        if abs((ema20 - ema60) / ema60 * 100) < chop_band and abs(ret60) < max(atr_pct * 4, 1.2):
            return "RANGE_CHOP"

        if direction == "LONG":
            late = (ret120 > 20 or change_24h > 80) and dist_ema60 > max(atr_pct * 4, 5)
            if late:
                return "TREND_LATE"
            if trend_up and 0 < ret60 < 15 and dist_ema60 < max(atr_pct * 3, 6):
                return "TREND_EARLY"
            if trend_up:
                return "TREND_MID"
        else:
            late = (ret120 < -20 or change_24h < -35) and dist_ema60 < -max(atr_pct * 4, 5)
            if late:
                return "TREND_LATE"
            if trend_down and -15 < ret60 < 0 and dist_ema60 > -max(atr_pct * 3, 6):
                return "TREND_EARLY"
            if trend_down:
                return "TREND_MID"
        return "UNKNOWN"

    def _exit_profile_for_state(self, state: str) -> dict:
        cfg = self.cfg
        if state == "TREND_EARLY":
            return {"tp1_ratio": 0.10, "tp2_ratio": 0.20}
        if state == "TREND_LATE":
            return {
                "tp1_ratio": 0.40,
                "tp2_ratio": 0.60,
                "time_stop_minutes": min(cfg.get("SCALP_TIME_STOP_MINUTES", 30), 20),
                "tp2_timeout_minutes": min(cfg.get("SCALP_TP2_TIMEOUT_MINUTES", 120), 60),
            }
        return {
            "tp1_ratio": cfg.get("SCALP_TP1_RATIO", 0.15),
            "tp2_ratio": cfg.get("SCALP_TP2_RATIO", 0.25),
        }

    def _cost_rate(self) -> float:
        return self.cfg.get("FEE_RATE_PER_SIDE", 0.0004) + self.cfg.get("SLIPPAGE_RATE_PER_SIDE", 0.0005)

    def _breakeven_price(self, pos: ScalpPosition) -> float:
        roundtrip_cost = self._cost_rate() * 2
        lock_pct = self.cfg.get("SCALP_NET_BREAKEVEN_LOCK_PCT", 0.0) / 100
        if pos.direction == "LONG":
            return pos.entry_price * (1 + roundtrip_cost + lock_pct)
        return pos.entry_price * (1 - roundtrip_cost - lock_pct)

    def _apply_tp3_trailing_stop(self, pos: ScalpPosition, price: float) -> None:
        if not pos.tp2_hit or pos.quantity_remaining <= 0 or pos.trail_ref_price <= 0:
            return

        trail_pct = pos.trail_pct
        if pos.direction == "LONG" and price > pos.trail_ref_price:
            pos.trail_ref_price = price
        elif pos.direction == "SHORT" and price < pos.trail_ref_price:
            pos.trail_ref_price = price

        candidates: list[float] = []
        buf_trail = self.kline_buffer.get(pos.symbol, [])
        if len(buf_trail) >= 20:
            ema20 = sum(k["c"] for k in buf_trail[-20:]) / 20
            candidates.append(
                ema20 * (1 - trail_pct / 100)
                if pos.direction == "LONG"
                else ema20 * (1 + trail_pct / 100)
            )
            structure_bars = int(pos.structure_trail_bars)
            recent = buf_trail[-structure_bars:] if len(buf_trail) >= structure_bars else buf_trail
            candidates.append(
                min(k["l"] for k in recent)
                if pos.direction == "LONG"
                else max(k["h"] for k in recent)
            )

        candidates.append(
            pos.trail_ref_price * (1 - trail_pct / 100)
            if pos.direction == "LONG"
            else pos.trail_ref_price * (1 + trail_pct / 100)
        )
        candidates = [c for c in candidates if c > 0]
        if not candidates:
            return

        aggressive_runner = self.cfg.get("SCALP_TP3_AGGRESSIVE_RUNNER", True)
        if pos.direction == "LONG":
            ordered = sorted(candidates)
            new_sl = ordered[len(ordered) // 2] if aggressive_runner else ordered[-1]
            pos.sl_price = max(new_sl, pos.sl_price)
        else:
            ordered = sorted(candidates)
            new_sl = ordered[len(ordered) // 2] if aggressive_runner else ordered[0]
            pos.sl_price = min(new_sl, pos.sl_price)

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
        """按币种分层返回轧空触发所需的最小OI降幅%;FR极负时保守降低门槛×0.5"""
        cfg = self.cfg
        if symbol in _MAJOR_SYMBOLS:
            base = cfg.get("SQUEEZE_OI_DROP_MAJOR", 0.5)
        else:
            vol = self.candidate_meta.get(symbol, {}).get("volume_24h", 0.0)
            if vol >= 500_000_000:
                base = cfg.get("SQUEEZE_OI_DROP_MID", 1.0)
            else:
                base = cfg.get("SQUEEZE_OI_DROP_MEME", 1.5)
        # FR < -0.1% 意味着空头积累严重，OI需降幅可放宽一半
        if self.candidate_meta.get(symbol, {}).get("fr_squeeze", False):
            return base * 0.5
        return base

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

        # OI暖机期静默，防首轮OI就绪引爆10+信号
        if time.monotonic() < self._oi_warmup_until:
            return

        news        = meta.get("news_sentiment", "neutral")
        taker_ratio = self._get_taker_ratio(symbol)
        if taker_ratio is None:
            self._fstat["vol_miss"] += 1
            return

        # ── Phase 1: 轧空猎杀（Surf负面新闻一票否决，24h偏置豁免）──────────
        if news != "negative":
            oi_change_pct = self._get_oi_change_pct(symbol)
            oi_threshold  = self._get_squeeze_oi_threshold(symbol)

            if oi_change_pct is not None and oi_change_pct <= -oi_threshold:
                wick_pct   = cfg.get("SQUEEZE_WICK_PCT", 1.0)
                # FR极负时同步放宽wick门槛，提高轧空信号灵敏度
                if meta.get("fr_squeeze", False):
                    wick_pct *= 0.5
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
                        state = self._check_market_state(symbol, "LONG")
                        await self._execute_entry(symbol, "LONG", abs(oi_change_pct), "轧空猎杀多", state)
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
                        state = self._check_market_state(symbol, "SHORT")
                        await self._execute_entry(symbol, "SHORT", abs(oi_change_pct), "轧多猎杀空", state)
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

        atr_pct = self._calc_atr_pct(buf)
        if not self._breakout_atr_allowed(atr_pct):
            self._fstat["atr_block"] += 1
            self._maybe_print_fstat()
            return

        closes     = [k["c"] for k in buf]
        ma5        = sum(closes[-5:])  / 5
        ma10       = sum(closes[-10:]) / 10
        ma20       = sum(closes[-20:]) / 20
        prev       = buf[-1]  # 最近闭合K线
        bo_taker   = cfg.get("BREAKOUT_TAKER_MIN", 0.60)
        bo_min_pct = self._breakout_required_pct(buf, atr_pct=atr_pct)

        if allow_long and ma5 > ma10 > ma20 and price > prev["h"] * (1 + bo_min_pct / 100) and taker_ratio >= bo_taker:
            if self._btc_guard("LONG"):
                state = self._check_market_state(symbol, "LONG")
                if state == "RANGE_CHOP":
                    self._fstat["state_block"] += 1
                    logger.info("⚡ [%s] 🟡 动能突破多被状态机过滤: RANGE_CHOP", symbol)
                    return
                premove_ok, premove_reason = self._breakout_premove_allowed(symbol, "LONG", price)
                if not premove_ok:
                    self._fstat["premove_block"] += 1
                    self._breakout_fired[symbol] = True
                    logger.info("⚡ [%s] 🟡 动能突破多被追涨过滤: %s", symbol, premove_reason)
                    return
                self._fstat["breakout"] += 1
                self._breakout_fired[symbol] = True
                bo_pct = (price - prev["h"]) / prev["h"] * 100
                logger.info("⚡ [%s] 🟡 动能突破多: 突破前高%.6f → %.6f (+%.3f%%) | Taker=%.0f%% | 状态=%s",
                            symbol, prev["h"], price, bo_pct, taker_ratio * 100, state)
                await self._execute_entry(symbol, "LONG", bo_pct, "动能突破多", state)
                return
            else:
                self._fstat["btc_guard"] += 1

        if allow_short and ma5 < ma10 < ma20 and price < prev["l"] * (1 - bo_min_pct / 100) and (1 - taker_ratio) >= bo_taker:
            if self._btc_guard("SHORT"):
                state = self._check_market_state(symbol, "SHORT")
                if state == "RANGE_CHOP":
                    self._fstat["state_block"] += 1
                    logger.info("⚡ [%s] 🟠 动能突破空被状态机过滤: RANGE_CHOP", symbol)
                    return
                premove_ok, premove_reason = self._breakout_premove_allowed(symbol, "SHORT", price)
                if not premove_ok:
                    self._fstat["premove_block"] += 1
                    self._breakout_fired[symbol] = True
                    logger.info("⚡ [%s] 🟠 动能突破空被追空过滤: %s", symbol, premove_reason)
                    return
                self._fstat["breakout"] += 1
                self._breakout_fired[symbol] = True
                bo_pct = (prev["l"] - price) / prev["l"] * 100
                logger.info("⚡ [%s] 🟠 动能突破空: 跌破前低%.6f → %.6f (-%.3f%%) | Taker卖=%.0f%% | 状态=%s",
                            symbol, prev["l"], price, bo_pct, (1 - taker_ratio) * 100, state)
                await self._execute_entry(symbol, "SHORT", bo_pct, "动能突破空", state)
                return
            else:
                self._fstat["btc_guard"] += 1

        self._maybe_print_fstat()

    # ─── 开仓 ──────────────────────────────────────────────────────────────────

    async def _execute_entry(self, symbol: str, direction: str,
                             trigger_pct: float, signal_label: str = "",
                             market_state: str = "UNKNOWN") -> None:
        cfg = self.cfg

        # 每日亏损熔断检查
        self._reset_daily_risk_if_needed()
        ban_until = self.symbol_ban_until.get(symbol, 0.0)
        if ban_until and time.time() < ban_until:
            logger.info("⚡ [%s] 🔒 单币熔断中，今日不再开仓", symbol)
            return
        max_daily = cfg.get("SCALP_MAX_DAILY_LOSS_USDT", 200.0)
        max_daily_r = cfg.get("SCALP_MAX_DAILY_LOSS_R", 10.0)
        daily_drawdown_r = self.daily_peak_r - self.daily_realized_r
        if self.daily_loss_usdt >= max_daily:
            logger.warning("⚡ [%s] 🔒 每日亏损熔断(已亏%.2fU ≥ %.0fU)，今日停止开仓",
                           symbol, self.daily_loss_usdt, max_daily)
            return
        if self.daily_realized_r <= -max_daily_r or daily_drawdown_r >= max_daily_r:
            logger.warning("⚡ [%s] 🔒 每日R熔断(净%.2fR / 回撤%.2fR / 阈值%.1fR)，今日停止开仓",
                           symbol, self.daily_realized_r, daily_drawdown_r, max_daily_r)
            return

        yaobi_ok, yaobi_reason = self._yaobi_entry_guard(symbol, direction)
        if not yaobi_ok:
            self._fstat["yaobi_block"] += 1
            self._signal_cooldown[symbol] = time.monotonic()
            logger.info("⚡ [%s] 🧭 妖币共享数据拦截 %s: %s", symbol, direction, yaobi_reason)
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
        position_usdt = cfg.get("SCALP_POSITION_USDT", 100.0)
        buf_entry     = self.kline_buffer.get(symbol, [])

        # ── 结构止损 + 最大保证金硬帽 ──────────────────────────────────────
        use_dynamic      = cfg.get("SCALP_USE_DYNAMIC_SL", True)
        sl_margin_pct    = cfg.get("SCALP_STOP_LOSS_PCT", 50.0)
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
        tp1_dist = sl_distance_pct * cfg.get("SCALP_TP1_RR", 2.0)
        tp2_dist = sl_distance_pct * cfg.get("SCALP_TP2_RR", 4.0)

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
        intended_risk_usdt = cfg.get("SCALP_RISK_PER_TRADE_USDT", 20.0)
        quantity_risk = (intended_risk_usdt / (entry_price * sl_distance_pct / 100)
                         if sl_distance_pct > 0
                         else position_usdt * leverage / entry_price)
        quantity_max = position_usdt * leverage / entry_price
        quantity     = min(quantity_risk, quantity_max)
        actual_risk_usdt = (
            entry_price * quantity * sl_distance_pct / 100
            if sl_distance_pct > 0 else intended_risk_usdt
        )
        exit_profile = self._exit_profile_for_state(market_state)
        tp1_ratio = exit_profile.get("tp1_ratio", cfg.get("SCALP_TP1_RATIO", 0.15))
        tp2_ratio = exit_profile.get("tp2_ratio", cfg.get("SCALP_TP2_RATIO", 0.25))
        time_stop_minutes = exit_profile.get("time_stop_minutes", cfg.get("SCALP_TIME_STOP_MINUTES", 30))
        tp2_timeout_minutes = exit_profile.get("tp2_timeout_minutes", cfg.get("SCALP_TP2_TIMEOUT_MINUTES", 120))

        base_signal = {
            "type":         "scalp",
            "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":       symbol,
            "direction":    direction,
            "signal_label": signal_label,
            "market_state": market_state,
            "trigger_pct":  round(trigger_pct, 2),
            "entry_price":  round(entry_price, 8),
            "sl_price":     round(sl_price, 8),
            "tp1_price":    round(tp1_price, 8),
            "tp2_price":    round(tp2_price, 8),
        }
        entry_context = self._entry_context_snapshot(
            symbol, direction, trigger_pct, signal_label, market_state,
        )
        base_signal["entry_context"] = entry_context
        logger.info(
            "⚡ [%s] ✅ [%s] SL=%.2f%% TP1=%.2f%%(×%.1fR) TP2=%.2f%%(×%.1fR) | qty=%.4f | 风险%.2fU/目标%.2fU",
            symbol, signal_label, sl_distance_pct, tp1_dist,
            tp1_dist / max(sl_distance_pct, 0.001), tp2_dist,
            tp2_dist / max(sl_distance_pct, 0.001), quantity,
            actual_risk_usdt, intended_risk_usdt,
        )
        base_signal.update({
            "quantity": round(quantity, 6),
            "risk_usdt_intended": round(intended_risk_usdt, 4),
            "risk_usdt_actual": round(actual_risk_usdt, 4),
        })
        self._fstat["passed"] += 1

        paper_mode = cfg.get("SCALP_PAPER_TRADE", False)
        if not cfg.get("SCALP_AUTO_TRADE", False) and not paper_mode:
            add_scalp_signal({**base_signal, "auto_traded": False, "paper": False})
            logger.info("⚡ [%s] 信号发出 (自动交易关闭)", symbol)
            return

        side   = "BUY"  if direction == "LONG" else "SELL"
        exit_s = "SELL" if direction == "LONG" else "BUY"

        # ── 模拟开仓 ──────────────────────────────────────────────────────
        if paper_mode:
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
                market_state       = market_state,
                tp1_ratio          = tp1_ratio,
                tp2_ratio          = tp2_ratio,
                trail_pct          = cfg.get("SCALP_TP3_TRAIL_PCT", 8.0),
                structure_trail_bars = int(cfg.get("SCALP_STRUCTURE_TRAIL_BARS", 5)),
                time_stop_minutes  = time_stop_minutes,
                tp2_timeout_minutes = tp2_timeout_minutes,
                risk_usdt          = actual_risk_usdt,
                paper              = True,
                entry_context      = entry_context,
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
        sl_distance_pct = abs(entry_price - sl_price) / entry_price * 100 if entry_price > 0 else sl_distance_pct
        actual_risk_usdt = (
            entry_price * quantity * sl_distance_pct / 100
            if sl_distance_pct > 0 else intended_risk_usdt
        )
        tp1_dist = sl_distance_pct * cfg.get("SCALP_TP1_RR", 2.0)
        tp2_dist = sl_distance_pct * cfg.get("SCALP_TP2_RR", 4.0)
        if direction == "LONG":
            tp1_price = entry_price * (1 + tp1_dist / 100)
            tp2_price = entry_price * (1 + tp2_dist / 100)
        else:
            tp1_price = entry_price * (1 - tp1_dist / 100)
            tp2_price = entry_price * (1 - tp2_dist / 100)
        base_signal.update({
            "entry_price": round(entry_price, 8),
            "tp1_price": round(tp1_price, 8),
            "tp2_price": round(tp2_price, 8),
            "quantity": round(quantity, 6),
            "risk_usdt_actual": round(actual_risk_usdt, 4),
        })

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
            market_state       = market_state,
            tp1_ratio          = tp1_ratio,
            tp2_ratio          = tp2_ratio,
            trail_pct          = cfg.get("SCALP_TP3_TRAIL_PCT", 8.0),
            structure_trail_bars = int(cfg.get("SCALP_STRUCTURE_TRAIL_BARS", 5)),
            time_stop_minutes  = time_stop_minutes,
            tp2_timeout_minutes = tp2_timeout_minutes,
            risk_usdt          = actual_risk_usdt,
            paper              = False,
            entry_context      = entry_context,
        )
        self.open_positions[symbol] = pos
        set_scalp_position(symbol, pos.to_dict())
        add_scalp_signal({**base_signal, "auto_traded": True, "paper": False,
                          "quantity": round(quantity, 6), "leverage": leverage,
                          "order_id": trade_resp.get("orderId")})
        logger.info("⚡ [%s] 开仓成功 %s ×%.6f @ %.6f | SL %.6f | TP1 %.6f | TP2 %.6f",
                    symbol, direction, quantity, entry_price, sl_price, tp1_price, tp2_price)

    # ─── TP / SL 实时检查 ──────────────────────────────────────────────────────

    def _apply_close_segment(self, pos: ScalpPosition, exit_price: float, qty: float) -> dict:
        qty = max(0.0, min(qty, pos.quantity_remaining))
        gross = qty * ((exit_price - pos.entry_price) if pos.direction == "LONG"
                       else (pos.entry_price - exit_price))
        entry_notional = pos.entry_price * qty
        exit_notional = exit_price * qty
        fee = (entry_notional + exit_notional) * self.cfg.get("FEE_RATE_PER_SIDE", 0.0004)
        slippage = (entry_notional + exit_notional) * self.cfg.get("SLIPPAGE_RATE_PER_SIDE", 0.0005)
        net = gross - fee - slippage

        pos.realized_gross_pnl += gross
        pos.realized_pnl += net
        pos.fee_usdt += fee
        pos.slippage_usdt += slippage
        pos.closed_quantity += qty
        pos.quantity_remaining = max(0.0, pos.quantity_remaining - qty)
        return {"qty": qty, "gross": gross, "fee": fee, "slippage": slippage, "net": net}

    def _update_position_excursion(self, pos: ScalpPosition, price: float) -> None:
        if price <= 0 or pos.entry_price <= 0:
            return
        move_pct = ((price - pos.entry_price) / pos.entry_price * 100
                    if pos.direction == "LONG"
                    else (pos.entry_price - price) / pos.entry_price * 100)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if move_pct > pos.max_favorable_pct:
            pos.max_favorable_pct = move_pct
            pos.max_favorable_time = now
        adverse = max(0.0, -move_pct)
        if adverse > pos.max_adverse_pct:
            pos.max_adverse_pct = adverse
            pos.max_adverse_time = now

    def _entry_context_snapshot(
        self,
        symbol: str,
        direction: str,
        trigger_pct: float,
        signal_label: str,
        market_state: str,
    ) -> dict:
        meta = dict(self.candidate_meta.get(symbol, {}))
        buf = self.kline_buffer.get(symbol, [])
        last = buf[-1] if buf else {}
        closes = [k["c"] for k in buf]
        ret20 = ((closes[-1] - closes[-20]) / closes[-20] * 100
                 if len(closes) >= 20 and closes[-20] else 0.0)
        ret60 = ((closes[-1] - closes[-60]) / closes[-60] * 100
                 if len(closes) >= 60 and closes[-60] else 0.0)
        taker = self._get_taker_ratio(symbol, min_vol_ratio=0.0)
        watch = get_watch_item(symbol) or {}
        seen_price = self._as_float(meta.get("scalp_candidate_seen_price"))
        last_price = self._as_float(meta.get("scalp_candidate_last_price"))
        max_up = self._as_float(meta.get("scalp_candidate_max_up_pct"))
        max_down = self._as_float(meta.get("scalp_candidate_max_down_pct"))
        pre_entry_favorable = max_up if direction == "LONG" else max_down
        pre_entry_adverse = max_down if direction == "LONG" else max_up
        current_price = self._as_float(
            self._live_candle.get(symbol, {}).get("close"),
            last_price or self._as_float(last.get("c")),
        )
        directional_30m = self._recent_directional_move_pct(symbol, direction, 30, current_price)
        return {
            "symbol": symbol,
            "direction": direction,
            "signal_label": signal_label,
            "market_state": market_state,
            "trigger_pct": round(trigger_pct, 4),
            "candidate_rank": meta.get("rank"),
            "direction_bias": meta.get("direction_bias"),
            "change_24h": meta.get("change_24h", 0.0),
            "volume_24h": meta.get("volume_24h", 0.0),
            "candidate_sources": meta.get("candidate_sources", []),
            "vol_surge": meta.get("vol_surge", 0.0),
            "funding_rate": meta.get("funding_rate", 0.0),
            "fr_squeeze": meta.get("fr_squeeze", False),
            "news_sentiment": meta.get("news_sentiment", "neutral"),
            "scalp_candidate_seen_time": meta.get("scalp_candidate_seen_time", ""),
            "scalp_candidate_seen_price": round(seen_price, 8) if seen_price else 0.0,
            "scalp_candidate_last_price": round(last_price, 8) if last_price else 0.0,
            "scalp_candidate_elapsed_min": meta.get("scalp_candidate_elapsed_min", 0.0),
            "scalp_candidate_max_up_pct": round(max_up, 4),
            "scalp_candidate_max_down_pct": round(max_down, 4),
            "pre_entry_favorable_from_candidate_pct": round(pre_entry_favorable, 4),
            "pre_entry_adverse_from_candidate_pct": round(pre_entry_adverse, 4),
            "pre_entry_directional_30m_pct": round(directional_30m, 4),
            "breakout_max_premove_30m_pct": self.cfg.get("BREAKOUT_MAX_PREMOVE_30M_PCT", 0.0),
            "yaobi_context": meta.get("yaobi_context", False),
            "yaobi_score": meta.get("yaobi_score", 0),
            "yaobi_anomaly_score": meta.get("yaobi_anomaly_score", 0),
            "yaobi_category": meta.get("yaobi_category", ""),
            "yaobi_decision_action": meta.get("yaobi_decision_action", ""),
            "yaobi_decision_confidence": meta.get("yaobi_decision_confidence", 0),
            "yaobi_decision_reasons": meta.get("yaobi_decision_reasons", []),
            "yaobi_decision_risks": meta.get("yaobi_decision_risks", []),
            "yaobi_sentiment_label": meta.get("yaobi_sentiment_label", ""),
            "yaobi_oi_trend_grade": meta.get("yaobi_oi_trend_grade", ""),
            "yaobi_oi_change_24h_pct": meta.get("yaobi_oi_change_24h_pct", 0.0),
            "yaobi_oi_change_3d_pct": meta.get("yaobi_oi_change_3d_pct", 0.0),
            "yaobi_oi_change_7d_pct": meta.get("yaobi_oi_change_7d_pct", 0.0),
            "yaobi_funding_rate_pct": meta.get("yaobi_funding_rate_pct", 0.0),
            "yaobi_volume_ratio": meta.get("yaobi_volume_ratio", 0.0),
            "yaobi_whale_long_ratio": meta.get("yaobi_whale_long_ratio", 0.0),
            "yaobi_short_crowd_pct": meta.get("yaobi_short_crowd_pct", 0.0),
            "yaobi_okx_buy_ratio": meta.get("yaobi_okx_buy_ratio", 0.0),
            "yaobi_okx_large_trade_pct": meta.get("yaobi_okx_large_trade_pct", 0.0),
            "yaobi_okx_risk_level": meta.get("yaobi_okx_risk_level", 0),
            "yaobi_address": meta.get("yaobi_address", ""),
            "yaobi_chain": meta.get("yaobi_chain", ""),
            "yaobi_market_filter_note": meta.get("yaobi_market_filter_note", ""),
            "watchlist_status": watch.get("status", ""),
            "watchlist_reason": watch.get("reason", ""),
            "oi_change_3m_pct": round(self._get_oi_change_pct(symbol) or 0.0, 4),
            "current_taker_ratio": round(taker, 4) if taker is not None else None,
            "atr_pct": round(self._calc_atr_pct(buf), 4) if len(buf) >= 15 else 0.0,
            "ret20m_pct": round(ret20, 4),
            "ret60m_pct": round(ret60, 4),
            "last_kline_close": last.get("c"),
            "kline_buffer_len": len(buf),
        }

    def _start_post_exit_watch(self, trade: dict, pos: ScalpPosition, exit_price: float) -> None:
        if exit_price <= 0:
            return
        trade["post_exit_status"] = "watching"
        trade["post_exit_horizons_min"] = [15, 30, 60, 120]
        watch = {
            "trade": trade,
            "direction": pos.direction,
            "exit_price": exit_price,
            "start_ts": time.monotonic(),
            "max_favorable_pct": 0.0,
            "max_adverse_pct": 0.0,
            "completed": set(),
            "horizons": [15, 30, 60, 120],
        }
        self._post_exit_watch.setdefault(pos.symbol, []).append(watch)

    def _update_post_exit_watch(self, symbol: str, price: float) -> None:
        watches = self._post_exit_watch.get(symbol)
        if not watches or price <= 0:
            return
        remaining = []
        now = time.monotonic()
        for watch in watches:
            exit_price = watch["exit_price"]
            move_pct = ((price - exit_price) / exit_price * 100
                        if watch["direction"] == "LONG"
                        else (exit_price - price) / exit_price * 100)
            if move_pct > watch["max_favorable_pct"]:
                watch["max_favorable_pct"] = move_pct
            adverse = max(0.0, -move_pct)
            if adverse > watch["max_adverse_pct"]:
                watch["max_adverse_pct"] = adverse

            elapsed_min = (now - watch["start_ts"]) / 60
            trade = watch["trade"]
            trade["post_exit_mfe_pct"] = round(watch["max_favorable_pct"], 4)
            trade["post_exit_mae_pct"] = round(watch["max_adverse_pct"], 4)
            trade["post_exit_last_price"] = round(price, 8)
            trade["post_exit_elapsed_min"] = round(elapsed_min, 2)
            for horizon in watch["horizons"]:
                if elapsed_min >= horizon and horizon not in watch["completed"]:
                    trade[f"post_exit_{horizon}m_favorable_pct"] = round(watch["max_favorable_pct"], 4)
                    trade[f"post_exit_{horizon}m_adverse_pct"] = round(watch["max_adverse_pct"], 4)
                    trade[f"post_exit_{horizon}m_price"] = round(price, 8)
                    watch["completed"].add(horizon)
            if len(watch["completed"]) < len(watch["horizons"]):
                remaining.append(watch)
            else:
                trade["post_exit_status"] = "complete"
        if remaining:
            self._post_exit_watch[symbol] = remaining
        else:
            self._post_exit_watch.pop(symbol, None)

    @staticmethod
    def _next_utc_midnight_ts() -> float:
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).date()
        return datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc).timestamp()

    def _register_symbol_loss(self, pos: ScalpPosition, close_reason: str, net_pnl: float) -> None:
        if net_pnl >= 0 or pos.risk_usdt <= 0:
            return
        now = time.monotonic()
        log = self._symbol_loss_log.setdefault(pos.symbol, [])
        log.append((now, abs(net_pnl) / pos.risk_usdt, close_reason))
        cutoff = now - 7200
        self._symbol_loss_log[pos.symbol] = [(ts, r, reason) for ts, r, reason in log if ts >= cutoff]
        recent = self._symbol_loss_log[pos.symbol]
        sl_count = sum(1 for _, _, reason in recent if reason == "SL")
        loss_r = sum(r for _, r, _ in recent)
        if sl_count >= 2 or loss_r >= 2.0:
            self.symbol_ban_until[pos.symbol] = self._next_utc_midnight_ts()
            logger.warning("⚡ [%s] 🔒 单币熔断: 2小时内SL=%d / 累计亏损%.2fR，禁入至UTC明日00:00",
                           pos.symbol, sl_count, loss_r)

    def _record_scalp_trade(self, pos: ScalpPosition, exit_price: float, close_reason: str) -> None:
        if pos.quantity_remaining > 0:
            self._apply_close_segment(pos, exit_price, pos.quantity_remaining)

        total_pnl = pos.realized_pnl
        risk_base = pos.risk_usdt or self.cfg.get("SCALP_RISK_PER_TRADE_USDT", 20.0)
        trade_r = total_pnl / risk_base if risk_base > 0 else 0.0
        self.daily_realized_r += trade_r
        self.daily_peak_r = max(self.daily_peak_r, self.daily_realized_r)
        if total_pnl < 0:
            self.daily_loss_usdt = getattr(self, "daily_loss_usdt", 0.0) + abs(total_pnl)
        self._register_symbol_loss(pos, close_reason, total_pnl)

        pnl_pct = (total_pnl / (pos.entry_price * pos.quantity / self.cfg.get("SCALP_LEVERAGE", 10)) * 100
                   if pos.entry_price > 0 and pos.quantity > 0 else 0.0)
        hold_minutes = (time.monotonic() - pos.entry_ts) / 60
        trade = {
            "symbol":       pos.symbol,
            "direction":    pos.direction,
            "signal":       pos.signal_label,
            "signal_label": pos.signal_label,
            "market_state": pos.market_state,
            "entry_price":  round(pos.entry_price, 8),
            "exit_price":   round(exit_price, 8),
            "entry_time":   pos.entry_time,
            "exit_time":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pnl_usdt":     round(total_pnl, 4),
            "gross_pnl_usdt": round(pos.realized_gross_pnl, 4),
            "fee_usdt":     round(pos.fee_usdt, 4),
            "slippage_usdt": round(pos.slippage_usdt, 4),
            "net_r":        round(trade_r, 4),
            "pnl_pct":      round(pnl_pct, 2),
            "hold_minutes":  round(hold_minutes, 2),
            "mfe_pct":      round(pos.max_favorable_pct, 4),
            "mae_pct":      round(pos.max_adverse_pct, 4),
            "mfe_time":     pos.max_favorable_time,
            "mae_time":     pos.max_adverse_time,
            "tp1_hit":      pos.tp1_hit,
            "tp2_hit":      pos.tp2_hit,
            "entry_context": pos.entry_context,
            "close_reason": close_reason,
            "paper":        pos.paper,
            "leverage":     self.cfg.get("SCALP_LEVERAGE", 10),
            "quantity":     round(pos.quantity, 6),
        }
        self._start_post_exit_watch(trade, pos, exit_price)
        add_scalp_trade(trade)

    async def _check_tp_sl(self, symbol: str, price: float) -> None:
        pos = self.open_positions.get(symbol)
        if not pos:
            return

        cfg       = self.cfg
        tp1_ratio = pos.tp1_ratio
        tp2_ratio = pos.tp2_ratio
        trail_pct = pos.trail_pct
        exit_s    = "SELL" if pos.direction == "LONG" else "BUY"
        auto      = cfg.get("SCALP_AUTO_TRADE", False)
        is_paper  = pos.paper
        tag       = "📋 " if is_paper else ""
        self._update_position_excursion(pos, price)

        def _tp_hit(tp_price: float) -> bool:
            return (pos.direction == "LONG"  and price >= tp_price) or \
                   (pos.direction == "SHORT" and price <= tp_price)

        async def _close_remaining(reason: str) -> bool:
            if not is_paper and auto and self.trader:
                resp = await self.trader.place_reduce_only_market_order(symbol, exit_s, pos.quantity_remaining)
                if not resp:
                    logger.error("⚡ [%s] %s平仓失败，保留本地仓位等待下一次检查", symbol, reason)
                    return False
                await self.trader.cancel_all_orders(symbol)
            self._record_scalp_trade(pos, price, reason)
            del self.open_positions[symbol]
            set_scalp_position(symbol, None)
            return True

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
                    stop_fraction = cfg.get("SCALP_REVERSAL_STOP_SL_FRACTION", 0.40)
                    if move_pct < sl_dist * stop_fraction:
                        logger.info("⚡ [%s] %s⚡ 瀑布反转(%s)提前止损 @ %.6f",
                                    symbol, tag, live_trend, price)
                        closed = await _close_remaining("趋势反转")
                        if closed:
                            self.observe_symbols[symbol] = {
                                "since": time.monotonic(), "last_trend": None, "count": 0,
                            }
                        return

        elapsed_min = (time.monotonic() - pos.entry_ts) / 60
        if (not pos.tp1_hit and
                elapsed_min >= pos.time_stop_minutes and
                not _tp_hit(pos.tp1_price)):
            logger.info("⚡ [%s] %s⏱ 时间止损 %.0fmin 未触达TP1 @ %.6f",
                        symbol, tag, elapsed_min, price)
            await _close_remaining("时间止损")
            return

        if (pos.tp1_hit and not pos.tp2_hit and
                elapsed_min >= pos.tp2_timeout_minutes and
                not _tp_hit(pos.tp2_price)):
            logger.info("⚡ [%s] %s⏱ TP1后超时 %.0fmin 未触达TP2 @ %.6f",
                        symbol, tag, elapsed_min, price)
            await _close_remaining("TP2超时")
            return

        # ── TP1：小比例拿利，SL移至成本调整后的保本位 ────────────────────
        if not pos.tp1_hit and _tp_hit(pos.tp1_price):
            buf_tp   = self.kline_buffer.get(symbol, [])
            tp_trend = self._detect_trend(buf_tp) if len(buf_tp) >= 20 else "FLAT"
            # 飞升全仓追踪：趋势仍是瀑布方向时跳过减仓，全仓保本追踪大行情
            if (pos.direction == "LONG"  and tp_trend == "WATERFALL_UP") or \
               (pos.direction == "SHORT" and tp_trend == "WATERFALL_DOWN"):
                new_sl              = self._breakeven_price(pos)
                if not is_paper and auto and self.trader:
                    old_sl_order_id = pos.sl_order_id
                    sl_resp = await self.trader.place_stop_loss_order(symbol, exit_s, new_sl)
                    if sl_resp and sl_resp.get("orderId"):
                        pos.sl_order_id = sl_resp["orderId"]
                        if old_sl_order_id:
                            await self.trader.cancel_order(symbol, old_sl_order_id)
                    else:
                        logger.error("⚡ [%s] 保本止损挂单失败，保留原止损单", symbol)
                        return
                pos.sl_price        = new_sl
                pos.tp1_hit         = True
                pos.tp2_hit         = True
                pos.trail_ref_price = price
                logger.info("⚡ [%s] %s🚀 飞升全仓追踪 @ %.6f (%s)，SL→成本保本",
                            symbol, tag, price, tp_trend)
                set_scalp_position(symbol, pos.to_dict())
                return
            qty     = pos.quantity * tp1_ratio
            new_sl  = self._breakeven_price(pos)
            if is_paper:
                segment = self._apply_close_segment(pos, price, qty)
                pos.tp1_hit = True
                pos.sl_price = new_sl
            elif auto:
                resp = await self.trader.place_reduce_only_market_order(symbol, exit_s, qty)
                if not resp:
                    logger.error("⚡ [%s] TP1减仓失败，保留本地仓位等待下一次检查", symbol)
                    return
                segment = self._apply_close_segment(pos, price, qty)
                pos.tp1_hit = True
                old_sl_order_id = pos.sl_order_id
                sl_resp = await self.trader.place_stop_loss_order(symbol, exit_s, new_sl)
                if sl_resp and sl_resp.get("orderId"):
                    pos.sl_order_id = sl_resp["orderId"]
                    if old_sl_order_id:
                        await self.trader.cancel_order(symbol, old_sl_order_id)
                    pos.sl_price    = new_sl
                else:
                    logger.error("⚡ [%s] TP1后保本止损挂单失败，保留原止损单", symbol)
            else:
                segment = {"net": 0.0}
                pos.tp1_hit = True
            pct_margin = segment["net"] / (pos.entry_price * pos.quantity / cfg.get("SCALP_LEVERAGE", 10)) * 100
            logger.info("⚡ [%s] %s🟡 TP1 @ %.6f | 锁%.0f%%仓 | 净保证金+%.1f%% | SL→成本保本",
                        symbol, tag, price, tp1_ratio * 100, pct_margin)
            set_scalp_position(symbol, pos.to_dict())

        # ── TP2：再锁一部分，剩余大头进入EMA20/结构追踪 ──────────────────
        elif pos.tp1_hit and not pos.tp2_hit and _tp_hit(pos.tp2_price):
            qty     = pos.quantity * tp2_ratio
            if is_paper:
                self._apply_close_segment(pos, price, qty)
                pos.tp2_hit = True
            elif auto:
                resp = await self.trader.place_reduce_only_market_order(symbol, exit_s, qty)
                if not resp:
                    logger.error("⚡ [%s] TP2减仓失败，保留本地仓位等待下一次检查", symbol)
                    return
                self._apply_close_segment(pos, price, qty)
                if pos.quantity_remaining > 0:
                    activ = (price * (1 - trail_pct / 200) if pos.direction == "LONG"
                             else price * (1 + trail_pct / 200))
                    await self.trader.place_trailing_stop_order(
                        symbol, exit_s, activ, trail_pct, pos.quantity_remaining,
                    )
                pos.tp2_hit = True
            else:
                pos.tp2_hit = True
            pos.trail_ref_price = price
            margin_base = pos.entry_price * pos.quantity / cfg.get("SCALP_LEVERAGE", 10)
            pct_margin = pos.realized_pnl / margin_base * 100 if margin_base > 0 else 0.0
            remaining_ratio = max(0.0, 1.0 - tp1_ratio - tp2_ratio)
            logger.info("⚡ [%s] %s🟢 TP2 @ %.6f | 再锁%.0f%%仓 | 累计净保证金+%.1f%% | 剩%.0f%%EMA20/结构追踪",
                        symbol, tag, price, tp2_ratio * 100, pct_margin, remaining_ratio * 100)
            if pos.quantity_remaining <= 0:
                self._record_scalp_trade(pos, price, "TP2")
                del self.open_positions[symbol]
                set_scalp_position(symbol, None)
                return
            set_scalp_position(symbol, pos.to_dict())

        # ── TP3：EMA20 + 市场结构 + 固定%三轨追踪止损（只向有利方向棘轮）──
        if pos.tp2_hit and pos.quantity_remaining > 0 and pos.trail_ref_price > 0:
            self._apply_tp3_trailing_stop(pos, price)

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
            await _close_remaining(reason)

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
            f"  ❌ 单币熔断中:   {s['symbol_banned']:>5}次",
            f"  ❌ 人工禁入:     {s['manual_block']:>5}次",
            f"  ❌ 妖币共享拦截: {s['yaobi_block']:>5}次",
            f"  ❌ 追涨/追空过滤: {s['premove_block']:>3}次",
            f"  ❌ 当前K量不足:  {s['vol_miss']:>5}次",
            f"  ❌ 状态机过滤:   {s['state_block']:>5}次",
            f"  ❌ ATR区间过滤:  {s['atr_block']:>5}次",
            f"  ❌ OI数据未就绪: {s['oi_miss']:>4}次  (启动30秒后可用)",
            f"  ❌ BTC大盘过滤:  {s['btc_guard']:>4}次",
            f"  🔴 轧空/轧多信号: {s['squeeze']}次",
            f"  🟡 动能突破信号:  {s['breakout']}次",
            f"  ✅ 触发开仓:      {s['passed']}次" + (" ★" if s["passed"] > 0 else " (等待行情)"),
            f"  参数: SQ_OI大币={cfg.get('SQUEEZE_OI_DROP_MAJOR',0.5)}%"
            f" 中型={cfg.get('SQUEEZE_OI_DROP_MID',1.0)}%"
            f" Meme={cfg.get('SQUEEZE_OI_DROP_MEME',1.5)}%"
            f" | Taker轧空≥{cfg.get('SQUEEZE_TAKER_MIN',0.65):.0%}"
            f" 突破≥{cfg.get('BREAKOUT_TAKER_MIN',0.62):.0%}",
            "  ────────────────────────────────────────────────────────────",
        ]
        logger.info("\n".join(lines))
        _signals_mod.scalp_filter_stats = {
            "checked":      s["checked"],
            "no_candidate": s["no_candidate"],
            "oi_miss":      s["oi_miss"],
            "btc_guard":    s["btc_guard"],
            "cooldown":     s["cooldown"],
            "symbol_banned": s["symbol_banned"],
            "manual_block":  s["manual_block"],
            "yaobi_block":   s["yaobi_block"],
            "premove_block": s["premove_block"],
            "vol_miss":     s["vol_miss"],
            "state_block":   s["state_block"],
            "atr_block":     s["atr_block"],
            "squeeze":      s["squeeze"],
            "breakout":     s["breakout"],
            "passed":       s["passed"],
            "cfg_sq_oi_major": cfg.get("SQUEEZE_OI_DROP_MAJOR", 0.5),
            "cfg_sq_oi_mid":   cfg.get("SQUEEZE_OI_DROP_MID",   1.0),
            "cfg_sq_oi_meme":  cfg.get("SQUEEZE_OI_DROP_MEME",  1.5),
            "cfg_sq_taker":    cfg.get("SQUEEZE_TAKER_MIN",      0.65),
            "cfg_bo_taker":    cfg.get("BREAKOUT_TAKER_MIN",     0.62),
            "cfg_bo_min_pct":   cfg.get("BREAKOUT_MIN_PCT",       0.10),
            "cfg_bo_atr_mult":  cfg.get("BREAKOUT_ATR_MULT",      0.7),
            "cfg_bo_atr_min":   cfg.get("BREAKOUT_ATR_MIN_PCT",   0.50),
            "cfg_bo_atr_max":   cfg.get("BREAKOUT_ATR_MAX_PCT",   1.20),
            "cfg_bo_max_premove_30m": cfg.get("BREAKOUT_MAX_PREMOVE_30M_PCT", 3.0),
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
            mode_str   = "模拟开仓" if paper_mode else ("自动下单" if auto else "仅信号")
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
