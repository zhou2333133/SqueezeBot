import asyncio
import logging
import os
from datetime import datetime

import aiohttp
import pandas as pd

from config import config_manager, DATA_DIR, MAX_CONCURRENT_REQUESTS, SURF_API_KEY
from okx_client import OKXOnChainClient
from signals import add_signal
from trader import BinanceTrader

logger = logging.getLogger(__name__)

_KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_asset_volume", "number_of_trades",
    "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore",
]


class BinanceSqueezeBot:
    def __init__(self):
        self.base_url = "https://fapi.binance.com"
        os.makedirs(DATA_DIR, exist_ok=True)
        self.trader: BinanceTrader | None = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    # ─── 网络 ──────────────────────────────────────────────────────────────

    async def fetch_with_retry(self, session, url, params=None, headers=None, retries=3):
        for i in range(retries):
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                async with self.semaphore:
                    async with session.get(
                        url, params=params, headers=headers, timeout=timeout
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        elif resp.status in (401, 403):
                            logger.error("API Key 验证失败: %s", url)
                            return None
                        elif resp.status == 429:
                            wait = 5 * (i + 1)
                            logger.warning("触发限流，%ds 后重试... (url=%s)", wait, url)
                            await asyncio.sleep(wait)
                        else:
                            logger.debug("HTTP %s，第 %d/%d 次重试: %s", resp.status, i + 1, retries, url)
            except asyncio.TimeoutError:
                logger.warning("请求超时，第 %d/%d 次重试: %s", i + 1, retries, url)
            except aiohttp.ClientError as e:
                logger.warning("网络错误 (%s)，第 %d/%d 次重试: %s", e, i + 1, retries, url)
            except Exception as e:
                logger.error("fetch 未知异常: %s", e)
            if i < retries - 1:
                await asyncio.sleep(1)
        return None

    # ─── 数据获取 ──────────────────────────────────────────────────────────

    async def get_derivatives_ratios(self, session, symbol: str) -> dict:
        params = {"symbol": symbol, "period": "5m", "limit": 1}
        tasks = [
            self.fetch_with_retry(session, f"{self.base_url}/futures/data/globalLongShortAccountRatio", params),
            self.fetch_with_retry(session, f"{self.base_url}/futures/data/topLongShortAccountRatio", params),
            self.fetch_with_retry(session, f"{self.base_url}/futures/data/topLongShortPositionRatio", params),
            self.fetch_with_retry(session, f"{self.base_url}/futures/data/takerlongshortRatio", params),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        def _safe(res, key):
            if isinstance(res, Exception) or not res:
                return None
            try:
                return float(res[0][key])
            except (KeyError, IndexError, TypeError, ValueError):
                return None

        return {
            "ls_account": _safe(results[0], "longShortRatio"),
            "top_acc_ls": _safe(results[1], "longShortRatio"),
            "top_pos_ls": _safe(results[2], "longShortRatio"),
            "taker_ls":   _safe(results[3], "buySellRatio"),
        }

    async def get_signal_confirmation(self, session, symbol: str, direction: str = "LONG"):
        cfg = config_manager.settings
        default_data = {
            "rsi": 50.0, "recent_low": 0.0, "recent_high": 0.0,
            "total_flow": 0.0, "consistent": False,
        }

        klines = await self.fetch_with_retry(
            session, f"{self.base_url}/fapi/v1/klines",
            params={"symbol": symbol, "interval": cfg["RSI_TIMEFRAME"], "limit": 100},
        )
        if not klines or len(klines) < cfg["RSI_PERIOD"]:
            return (False, default_data) if cfg["TA_CONFIRMATION_ENABLED"] else (True, default_data)

        df = pd.DataFrame(klines, columns=_KLINE_COLS)
        for col in ("open", "high", "low", "close",
                    "quote_asset_volume", "taker_buy_quote_asset_volume"):
            df[col] = df[col].astype(float)

        df["delta"] = df["taker_buy_quote_asset_volume"] - (
            df["quote_asset_volume"] - df["taker_buy_quote_asset_volume"]
        )
        recent_3 = df.iloc[-3:]
        total_flow  = float(recent_3["delta"].sum())
        recent_low  = float(recent_3["low"].min())
        recent_high = float(recent_3["high"].max())

        if direction == "LONG":
            consistent_flow = all(d > 0 for d in recent_3["delta"])
        else:
            consistent_flow = all(d < 0 for d in recent_3["delta"])

        _period = cfg["RSI_PERIOD"]
        _delta = df["close"].diff()
        _gain = _delta.clip(lower=0).ewm(alpha=1 / _period, adjust=False).mean()
        _loss = (-_delta.clip(upper=0)).ewm(alpha=1 / _period, adjust=False).mean()
        rsi_series = 100 - 100 / (1 + _gain / _loss.replace(0, 1e-10))
        latest_rsi = rsi_series.iloc[-1] if not rsi_series.empty else None
        if latest_rsi is None or pd.isna(latest_rsi):
            latest_rsi = 50.0
        latest_rsi = float(latest_rsi)

        if cfg["TA_CONFIRMATION_ENABLED"]:
            rsi_ok = (latest_rsi < cfg["RSI_MAX_ENTRY"]) if direction == "LONG" \
                     else (latest_rsi > 100 - cfg["RSI_MAX_ENTRY"])
        else:
            rsi_ok = True

        return rsi_ok, {
            "rsi": latest_rsi,
            "recent_low": recent_low,
            "recent_high": recent_high,
            "total_flow": total_flow,
            "consistent": consistent_flow,
        }

    async def mtf_timing_filter(self, session, symbol: str, direction: str = "LONG"):
        try:
            klines_4h = await self.fetch_with_retry(
                session, f"{self.base_url}/fapi/v1/klines",
                params={"symbol": symbol, "interval": "4h", "limit": 50},
            )
            if not klines_4h or len(klines_4h) < 21:
                return False, "4H K线数据不足"

            df_4h = pd.DataFrame(klines_4h, columns=_KLINE_COLS)
            df_4h["close"] = df_4h["close"].astype(float)
            df_4h["ema20"] = ta.ema(df_4h["close"], length=20)
            current_price = float(df_4h["close"].iloc[-1])
            ema20_4h = float(df_4h["ema20"].iloc[-1])

            if direction == "LONG" and current_price < ema20_4h:
                return False, f"4H趋势看跌 (价格 {current_price:.4f} < EMA20 {ema20_4h:.4f})"
            if direction == "SHORT" and current_price > ema20_4h:
                return False, f"4H趋势看涨 (价格 {current_price:.4f} > EMA20 {ema20_4h:.4f})"

            klines_15m = await self.fetch_with_retry(
                session, f"{self.base_url}/fapi/v1/klines",
                params={"symbol": symbol, "interval": "15m", "limit": 100},
            )
            if not klines_15m or len(klines_15m) < 51:
                return False, "15M K线数据不足"

            df_15m = pd.DataFrame(klines_15m, columns=_KLINE_COLS)
            df_15m["close"] = df_15m["close"].astype(float)
            df_15m["ema50"] = ta.ema(df_15m["close"], length=50)
            price_15m  = float(df_15m["close"].iloc[-1])
            ema50_15m  = float(df_15m["ema50"].iloc[-1])

            if price_15m > ema50_15m * 1.02:
                return False, f"15M价格离需求区太远 ({price_15m:.4f} > EMA50×1.02 {ema50_15m*1.02:.4f})"
            if price_15m < ema50_15m * 0.98:
                return False, f"15M价格深跌破需求区 ({price_15m:.4f} < EMA50×0.98 {ema50_15m*0.98:.4f})"

            return True, f"✅ MTF共振: 4H趋势向上, 15M回调至需求区 (EMA50: {ema50_15m:.4f})"

        except Exception as e:
            logger.error("MTF分析异常 [%s]: %s", symbol, e, exc_info=True)
            return False, "MTF分析异常"

    # ─── 核心处理 ──────────────────────────────────────────────────────────

    async def process_symbol(
        self, session, symbol: str,
        premium_info: dict, ticker_info: dict,
        btc_change: float, okx: OKXOnChainClient,
    ):
        cfg = config_manager.settings

        # ── 1. 基础数据 ──────────────────────────────────────────────────
        mark_price        = float(premium_info.get("markPrice", 0))
        index_price       = float(premium_info.get("indexPrice", 0))
        last_funding_rate = float(premium_info.get("lastFundingRate", 0))
        price_change      = float(ticker_info.get("priceChangePercent", 0))

        if mark_price <= 0:
            return

        basis         = mark_price - index_price
        basis_percent = basis / index_price if index_price else 0

        # ── 2. OI 历史激增 ────────────────────────────────────────────────
        oi_hist = await self.fetch_with_retry(
            session, f"{self.base_url}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "5m", "limit": 10},
        )
        oi_surge_ratio = 0.0
        latest_oi = 0.0
        if oi_hist:
            oi_values = [float(x["sumOpenInterestValue"]) for x in oi_hist]
            latest_oi = oi_values[-1]
            if len(oi_values) >= 10:
                recent_3_mean  = sum(oi_values[-3:]) / 3
                recent_10_mean = sum(oi_values) / 10
                if recent_10_mean > 0:
                    oi_surge_ratio = recent_3_mean / recent_10_mean

        # ── 3. 多空比 ─────────────────────────────────────────────────────
        ratios = await self.get_derivatives_ratios(session, symbol)

        # ── 4. 落地 CSV ───────────────────────────────────────────────────
        row = {
            "timestamp":                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":                      symbol,
            "mark_price":                  mark_price,
            "index_price":                 index_price,
            "price_change_percent":        price_change,
            "basis":                       basis,
            "basis_percent":               basis_percent,
            "last_funding_rate":           last_funding_rate,
            "oi":                          latest_oi,
            "long_short_account_ratio":    ratios["ls_account"],
            "top_trader_account_ls_ratio": ratios["top_acc_ls"],
            "top_trader_position_ls_ratio":ratios["top_pos_ls"],
            "taker_buy_sell_ratio":        ratios["taker_ls"],
        }
        csv_path = os.path.join(DATA_DIR, f"{symbol}.csv")
        write_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
        pd.DataFrame([row]).to_csv(csv_path, mode="a", header=write_header, index=False)

        # ── 5. 信号筛选 ───────────────────────────────────────────────────
        whale_ls  = ratios["top_pos_ls"]
        retail_ls = ratios["ls_account"]
        is_cap_valid = cfg["MIN_OI_USDT"] <= latest_oi <= cfg["MAX_OI_USDT"]

        is_smart_money_long  = (whale_ls  and whale_ls  >= cfg["WHALE_LS_MIN"]) and \
                               (retail_ls and retail_ls <= cfg["RETAIL_LS_MAX"])
        is_smart_money_short = (whale_ls  and whale_ls  <= cfg["RETAIL_LS_MAX"]) and \
                               (retail_ls and retail_ls >= cfg["WHALE_LS_MIN"])

        basic_filter_long  = (is_cap_valid and is_smart_money_long
                               and last_funding_rate <= 0.0005
                               and price_change > btc_change
                               and oi_surge_ratio > cfg["OI_SURGE_RATIO"])
        basic_filter_short = (is_cap_valid and is_smart_money_short
                               and last_funding_rate >= -0.0005
                               and price_change < btc_change
                               and oi_surge_ratio > cfg["OI_SURGE_RATIO"])

        signal_direction = None
        if cfg.get("ENABLE_LONG_STRATEGY", True)  and basic_filter_long:
            signal_direction = "LONG"
        elif cfg.get("ENABLE_SHORT_STRATEGY", False) and basic_filter_short:
            signal_direction = "SHORT"

        if not signal_direction:
            return

        # ── TA 二次确认 ──────────────────────────────────────────────────
        is_confirmed, flow_data = await self.get_signal_confirmation(
            session, symbol, direction=signal_direction
        )
        flow_ratio = abs(flow_data["total_flow"]) / latest_oi if latest_oi > 0 else 0
        if not flow_data["consistent"] or flow_ratio < cfg["FLOW_OI_RATIO"] or not is_confirmed:
            return

        # ── Surf.ai 过滤（新闻 + 清算热图）────────────────────────────────
        surf_headers = {"Authorization": f"Bearer {SURF_API_KEY}"}
        has_good_news = True
        news_context  = "未启用"
        has_liq_target = True
        liq_context    = "未启用"

        if SURF_API_KEY and SURF_API_KEY != "YOUR_SURF_API_KEY":
            if cfg["ENABLE_NEWS_FILTER"]:
                base_asset = symbol.replace("USDT", "")
                news = await self.fetch_with_retry(
                    session, "https://api.asksurf.ai/v1/news/curated",
                    params={"q": base_asset, "limit": 1}, headers=surf_headers,
                )
                if news and news.get("data"):
                    has_good_news = True
                    news_context = f"检测到新闻: '{news['data'][0]['title'][:40]}...'"
                else:
                    has_good_news = False
                    news_context = "无相关新闻"

            if cfg["ENABLE_LIQ_FILTER"]:
                liq_data = await self.fetch_with_retry(
                    session, "https://api.asksurf.ai/v1/exchange/liquidations_chart",
                    params={"symbol": symbol, "exchange": "Binance"}, headers=surf_headers,
                )
                if liq_data and liq_data.get("data"):
                    recent     = liq_data["data"][-12:]
                    short_liq  = sum(d.get("short_liquidations_usd", 0) for d in recent)
                    long_liq   = sum(d.get("long_liquidations_usd",  0) for d in recent)
                    if signal_direction == "LONG":
                        has_liq_target = short_liq > long_liq * cfg["LIQ_SHORT_RATIO_MIN"] if long_liq > 0 else short_liq > 0
                        liq_context = f"空头清算占优 (空/多: {short_liq/long_liq:.2f}x)" if long_liq > 0 and has_liq_target else "无明显空头清算"
                    else:
                        has_liq_target = long_liq > short_liq * cfg["LIQ_SHORT_RATIO_MIN"] if short_liq > 0 else long_liq > 0
                        liq_context = f"多头清算占优 (多/空: {long_liq/short_liq:.2f}x)" if short_liq > 0 and has_liq_target else "无明显多头清算"
                else:
                    has_liq_target = False
                    liq_context = "无清算数据"

        if (cfg["ENABLE_NEWS_FILTER"] and not has_good_news) or \
           (cfg["ENABLE_LIQ_FILTER"]  and not has_liq_target):
            return

        # ── MTF 择时过滤 ──────────────────────────────────────────────────
        timing_reason = "MTF过滤已关闭"
        if cfg.get("ENABLE_MTF_FILTER", True):
            is_timing_good, timing_reason = await self.mtf_timing_filter(
                session, symbol, direction=signal_direction
            )
            if not is_timing_good:
                logger.info("[%s] MTF择时未通过: %s", symbol, timing_reason)
                return

        # ── OKX 链上确认（第 6 层过滤）────────────────────────────────────
        okx_ok, okx_context = True, "OKX过滤已关闭"
        if cfg.get("ENABLE_OKX_FILTER", True):
            okx_ok, okx_context = await okx.check_onchain_confirmation(symbol, signal_direction)
            if not okx_ok:
                logger.info("[%s] OKX链上过滤未通过: %s", symbol, okx_context)
                return

        # ── AI Agent 评分 ────────────────────────────────────────────────
        # 未配置 Surf.ai 时跳过评分（默认通过）；配置了但调用失败则拒绝（fail closed）
        ai_score = 100
        if cfg.get("ENABLE_AI_AGENT", True) and SURF_API_KEY and SURF_API_KEY != "YOUR_SURF_API_KEY":
            ai_score = 0  # fail closed — 必须从 API 获取实际分数
            ai_context = (
                f"Price Change 24h: {price_change:.2f}% (BTC: {btc_change:.2f}%) | "
                f"Funding: {last_funding_rate*100:.4f}% | "
                f"OI: ${latest_oi/1e6:.2f}M (surge {oi_surge_ratio:.2f}x) | "
                f"Flow: ${flow_data['total_flow']/1e6:.2f}M ({flow_ratio*100:.2f}% of OI) | "
                f"Whales L/S={whale_ls}, Retail L/S={retail_ls} | "
                f"News: {news_context} | Liq: {liq_context} | "
                f"MTF: {timing_reason} | OKX: {okx_context}"
            )
            prompt = (
                f"Crypto analyst: evaluate a {signal_direction} trade for {symbol}. "
                f"Score 0-100 (80+ = strong signal). Output ONLY the integer. Data: {ai_context}"
            )
            try:
                payload = {"model": "surf-1.5-turbo", "messages": [{"role": "user", "content": prompt}]}
                async with session.post(
                    "https://api.asksurf.ai/v1/chat/completions",
                    json=payload, headers=surf_headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        score_text = data["choices"][0]["message"]["content"].strip()
                        digits = "".join(filter(str.isdigit, score_text))
                        ai_score = min(100, int(digits)) if digits else 0
                    else:
                        logger.error("AI Agent HTTP %s: %s", resp.status, await resp.text())
            except Exception as e:
                logger.error("AI Agent 调用失败: %s", e)

        if ai_score < cfg["AI_AGENT_SCORE_MIN"]:
            logger.info("[%s] AI评分 %d < 阈值 %d，跳过", symbol, ai_score, cfg["AI_AGENT_SCORE_MIN"])
            return

        # ── 自动交易 ──────────────────────────────────────────────────────
        trade_details: dict = {}
        if cfg["AUTO_TRADE_ENABLED"] and self.trader:
            leverage     = cfg.get("LEVERAGE", 5)
            await self.trader.set_leverage(symbol, leverage)
            nominal_size = cfg["POSITION_SIZE_USDT"] * leverage
            quantity     = nominal_size / mark_price
            entry_side   = "BUY"  if signal_direction == "LONG" else "SELL"
            exit_side    = "SELL" if signal_direction == "LONG" else "BUY"

            trade_resp = await self.trader.place_market_order(symbol, entry_side, quantity)
            if trade_resp:
                trade_details = {
                    "leverage":     leverage,
                    "margin_usdt":  cfg["POSITION_SIZE_USDT"],
                    "quantity":     round(quantity, 6),
                    "order_id":     trade_resp.get("orderId"),
                }

                if cfg.get("USE_TRAILING_STOP", False):
                    mult       = 1 + cfg["TSL_ACTIVATION_PERCENT"] / 100 if signal_direction == "LONG" \
                                 else 1 - cfg["TSL_ACTIVATION_PERCENT"] / 100
                    activation = mark_price * mult
                    await self.trader.place_trailing_stop_order(
                        symbol, exit_side, activation, cfg["TSL_CALLBACK_PERCENT"], quantity
                    )
                    trade_details["tp_strategy"] = f"追踪止损 (回调 {cfg['TSL_CALLBACK_PERCENT']}%)"
                else:
                    tp_mult  = 1 + cfg["TAKE_PROFIT_PERCENT"] / 100 if signal_direction == "LONG" \
                               else 1 - cfg["TAKE_PROFIT_PERCENT"] / 100
                    tp_price = mark_price * tp_mult
                    await self.trader.place_take_profit_order(symbol, exit_side, tp_price)
                    trade_details["tp_price"] = round(tp_price, 4)

                if cfg.get("USE_DYNAMIC_SL", False):
                    if signal_direction == "LONG" and flow_data["recent_low"] > 0:
                        sl_price = flow_data["recent_low"] * 0.998
                    elif signal_direction == "SHORT" and flow_data["recent_high"] > 0:
                        sl_price = flow_data["recent_high"] * 1.002
                    else:
                        sl_mult  = 1 - cfg["STOP_LOSS_PERCENT"] / 100 if signal_direction == "LONG" \
                                   else 1 + cfg["STOP_LOSS_PERCENT"] / 100
                        sl_price = mark_price * sl_mult
                else:
                    sl_mult  = 1 - cfg["STOP_LOSS_PERCENT"] / 100 if signal_direction == "LONG" \
                               else 1 + cfg["STOP_LOSS_PERCENT"] / 100
                    sl_price = mark_price * sl_mult

                await self.trader.place_stop_loss_order(symbol, exit_side, sl_price)
                trade_details["sl_price"] = round(sl_price, 4)

        # ── 信号入库（推送到 Web 控制台）─────────────────────────────────
        signal = {
            "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":          symbol,
            "direction":       signal_direction,
            "mark_price":      mark_price,
            "ai_score":        ai_score,
            "funding_rate":    round(last_funding_rate * 100, 5),
            "oi_usdt":         round(latest_oi / 1e6, 2),
            "oi_surge_ratio":  round(oi_surge_ratio, 2),
            "flow_ratio_pct":  round(flow_ratio * 100, 2),
            "whale_ls":        whale_ls,
            "retail_ls":       retail_ls,
            "rsi":             round(flow_data["rsi"], 1),
            "price_change":    round(price_change, 2),
            "btc_change":      round(btc_change, 2),
            "filters": {
                "ta":   is_confirmed,
                "news": has_good_news,
                "liq":  has_liq_target,
                "mtf":  timing_reason.startswith("✅") or timing_reason == "MTF过滤已关闭",
                "okx":  okx_ok,
            },
            "details": {
                "news":    news_context,
                "liq":     liq_context,
                "mtf":     timing_reason,
                "okx":     okx_context,
            },
            "trade": trade_details,
        }
        add_signal(signal)
        logger.info("🚨 [%s] 信号已发出 (方向: %s, AI评分: %d)", symbol, signal_direction, ai_score)

    # ─── 主循环 ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info("🚀 币安庄家轧空监控机器人启动！")
        async with aiohttp.ClientSession(trust_env=True) as session:
            okx = OKXOnChainClient(session)
            while True:
                try:
                    cfg = config_manager.settings
                    logger.info("🔍 开始全市场扫描 (间隔 %d 分钟)...", cfg["INTERVAL_MINUTES"])

                    self.trader = BinanceTrader(session) if cfg["AUTO_TRADE_ENABLED"] else None

                    premium_data, ticker_data = await asyncio.gather(
                        self.fetch_with_retry(session, f"{self.base_url}/fapi/v1/premiumIndex"),
                        self.fetch_with_retry(session, f"{self.base_url}/fapi/v1/ticker/24hr"),
                    )

                    if premium_data and ticker_data:
                        premium_dict = {x["symbol"]: x for x in premium_data if x["symbol"].endswith("USDT")}
                        ticker_dict  = {x["symbol"]: x for x in ticker_data  if x["symbol"].endswith("USDT")}
                        btc_change   = float(ticker_dict.get("BTCUSDT", {}).get("priceChangePercent", 0))
                        symbols      = [s for s in premium_dict if s != "BTCUSDT"]

                        tasks = [
                            self.process_symbol(
                                session, sym,
                                premium_dict[sym], ticker_dict.get(sym, {}),
                                btc_change, okx,
                            )
                            for sym in symbols
                        ]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        errors  = [r for r in results if isinstance(r, Exception)]
                        if errors:
                            logger.warning("本轮 %d 个币种处理出现异常（已跳过）", len(errors))
                    else:
                        logger.error("无法获取 premiumIndex 或 ticker 数据，本轮跳过")

                    logger.info("✅ 本轮扫描完成，休眠 %d 分钟", cfg["INTERVAL_MINUTES"])

                except Exception as e:
                    logger.error("主循环异常: %s", e, exc_info=True)

                await asyncio.sleep(config_manager.settings["INTERVAL_MINUTES"] * 60)
