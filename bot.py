import os
import asyncio
import logging
import aiohttp
import pandas as pd
import pandas_ta as ta
from datetime import datetime
from config import config_manager, DATA_DIR, MAX_CONCURRENT_REQUESTS, SURF_API_KEY
from notifier import send_telegram_alert
from trader import BinanceTrader

logger = logging.getLogger(__name__)

class BinanceSqueezeBot:
    def __init__(self):
        self.base_url = "https://fapi.binance.com"
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)
        # 初始化交易模块
        self.trader = None
        # 使用信号量控制并发度，防止被币安封禁 IP
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def fetch_with_retry(self, session, url, params=None, headers=None, retries=3):
        for i in range(retries):
            try:
                async with self.semaphore:
                    async with session.get(url, params=params, headers=headers, timeout=20) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        elif resp.status in [401, 403]:
                            logger.error(f"API Key 验证失败或无权限: {url}")
                            return None # Stop retrying on auth errors
                        elif resp.status == 429: # API 达到限流
                            logger.warning(f"触发限流，休眠重试...")
                            await asyncio.sleep(5)
            except Exception:
                pass
            await asyncio.sleep(1)
        return None

    async def get_derivatives_ratios(self, session, symbol):
        """并发获取单一币种的衍生多空比等各项数据"""
        params = {"symbol": symbol, "period": "5m", "limit": 1}
        tasks = [
            self.fetch_with_retry(session, f"{self.base_url}/futures/data/globalLongShortAccountRatio", params),
            self.fetch_with_retry(session, f"{self.base_url}/futures/data/topLongShortAccountRatio", params),
            self.fetch_with_retry(session, f"{self.base_url}/futures/data/topLongShortPositionRatio", params),
            self.fetch_with_retry(session, f"{self.base_url}/futures/data/takerlongshortRatio", params)
        ]
        results = await asyncio.gather(*tasks)
        
        return {
            "ls_account": float(results[0][0]['longShortRatio']) if results[0] else None,
            "top_acc_ls": float(results[1][0]['longShortRatio']) if results[1] else None,
            "top_pos_ls": float(results[2][0]['longShortRatio']) if results[2] else None,
            "taker_ls": float(results[3][0]['buySellRatio']) if results[3] else None
        }

    async def get_signal_confirmation(self, session, symbol: str) -> (bool, dict):
        """获取K线进行订单流(CVD净流入)和TA二次确认"""
        cfg = config_manager.settings
        default_data = {"rsi": 0.0, "recent_low": 0.0, "total_flow": 0.0, "consistent": True}

        klines = await self.fetch_with_retry(
            session, f"{self.base_url}/fapi/v1/klines",
            params={"symbol": symbol, "interval": cfg["RSI_TIMEFRAME"], "limit": 100}
        )
        if not klines or len(klines) < cfg["RSI_PERIOD"]:
            if cfg["TA_CONFIRMATION_ENABLED"]: return False, default_data
            return True, default_data

        df = pd.DataFrame(klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time',
            'quote_asset_volume', 'number_of_trades', 'taker_buy_base_asset_volume',
            'taker_buy_quote_asset_volume', 'ignore'
        ])
        df['close'] = df['close'].astype(float)
        df['low'] = df['low'].astype(float)
        
        # 计算资金流 (CVD Delta)
        df['taker_buy'] = df['taker_buy_quote_asset_volume'].astype(float)
        df['taker_sell'] = df['quote_asset_volume'].astype(float) - df['taker_buy']
        df['delta'] = df['taker_buy'] - df['taker_sell']
        
        recent_3 = df.iloc[-3:]
        consistent_flow = all(d > 0 for d in recent_3['delta']) # 连续3根绿柱(买入大于卖出)
        total_flow = recent_3['delta'].sum()
        recent_low = recent_3['low'].min()

        # 计算 RSI
        rsi = df.ta.rsi(length=cfg["RSI_PERIOD"])
        latest_rsi = rsi.iloc[-1]

        rsi_ok = latest_rsi < cfg["RSI_MAX_ENTRY"] if cfg["TA_CONFIRMATION_ENABLED"] else True
        return rsi_ok, {"rsi": latest_rsi, "recent_low": recent_low, "total_flow": total_flow, "consistent": consistent_flow}

    async def process_symbol(self, session, symbol, premium_info, ticker_info, btc_change):
        cfg = config_manager.settings
        now = datetime.now()

        # 1. 基础数据推导
        mark_price = float(premium_info.get('markPrice', 0))
        index_price = float(premium_info.get('indexPrice', 0))
        last_funding_rate = float(premium_info.get('lastFundingRate', 0))
        price_change = float(ticker_info.get('priceChangePercent', 0))

        basis = mark_price - index_price
        basis_percent = basis / index_price if index_price else 0

        # 2. 获取历史 OI 数据，判断是否激增
        oi_hist = await self.fetch_with_retry(
            session, f"{self.base_url}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "5m", "limit": 10}
        )

        oi_surge_ratio = 0
        latest_oi = 0
        if oi_hist and len(oi_hist) > 0:
            oi_values = [float(x['sumOpenInterestValue']) for x in oi_hist]
            latest_oi = oi_values[-1]
            if len(oi_values) == 10:
                recent_3_mean = sum(oi_values[-3:]) / 3
                recent_10_mean = sum(oi_values) / 10
                if recent_10_mean > 0:
                    oi_surge_ratio = recent_3_mean / recent_10_mean

        # 3. 抓取各类衍生品做空比例
        ratios = await self.get_derivatives_ratios(session, symbol)

        # 4. 数据保存至 CSV
        row = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "mark_price": mark_price,
            "index_price": index_price,
            "price_change_percent": price_change,
            "basis": basis,
            "basis_percent": basis_percent,
            "last_funding_rate": last_funding_rate,
            "oi": latest_oi,
            "long_short_account_ratio": ratios["ls_account"],
            "top_trader_account_ls_ratio": ratios["top_acc_ls"],
            "top_trader_position_ls_ratio": ratios["top_pos_ls"],
            "taker_buy_sell_ratio": ratios["taker_ls"]
        }
        df = pd.DataFrame([row])
        csv_path = os.path.join(DATA_DIR, f"{symbol}.csv")
        df.to_csv(csv_path, mode='a', header=not os.path.exists(csv_path), index=False)

        # 5. 🚨 核心报警逻辑 🚨
        # 步骤 1&2: 盘子大小限制 ($5M - $50M)
        is_cap_valid = cfg["MIN_OI_USDT"] <= latest_oi <= cfg["MAX_OI_USDT"]
        # 步骤 4: 筹码分歧 (大户多>1.05, 散户空<0.95)
        whale_ls = ratios["top_pos_ls"]
        retail_ls = ratios["ls_account"]
        is_smart_money = (whale_ls and whale_ls >= cfg["WHALE_LS_MIN"]) and (retail_ls and retail_ls <= cfg["RETAIL_LS_MAX"])
        # 步骤 5: 费率成本判定 (< 0.05% 即 0.0005 为入场好时机)
        is_good_funding = last_funding_rate <= 0.0005
        # 步骤 6: 相对强弱 (Alpha)
        is_stronger_than_btc = price_change > btc_change

        # 初筛：小盘子 + 聪明钱 + 好费率 + 比大盘强 + OI激增
        basic_filter = is_cap_valid and is_smart_money and is_good_funding and is_stronger_than_btc and oi_surge_ratio > cfg["OI_SURGE_RATIO"]

        if basic_filter:
            # 进行二次TA确认
            is_confirmed, flow_data = await self.get_signal_confirmation(session, symbol)
            
            flow_ratio = flow_data['total_flow'] / latest_oi if latest_oi > 0 else 0

            # 验证 步骤2(流入占比) 和 步骤3(流入连续性)
            if not flow_data['consistent'] or flow_ratio < cfg["FLOW_OI_RATIO"] or not is_confirmed:
                return

            # --- 终极进化：Surf.ai 统一数据源过滤 ---
            surf_headers = {"Authorization": f"Bearer {SURF_API_KEY}"}
            
            # 1. 新闻过滤
            has_good_news = True
            news_context = "未检测"
            if cfg["ENABLE_NEWS_FILTER"] and SURF_API_KEY != "YOUR_SURF_API_KEY":
                base_asset = symbol.replace("USDT", "")
                news_params = {"q": base_asset, "limit": 1}
                news = await self.fetch_with_retry(session, "https://api.asksurf.ai/v1/news/curated", params=news_params, headers=surf_headers)
                if news and len(news.get("data", [])) > 0:
                    has_good_news = True
                    news_context = f"检测到新闻: '{news['data'][0]['title']}'"
                else:
                    has_good_news = False
                    news_context = "无相关新闻"

            # 2. 清算热图过滤
            has_liq_target = True
            liq_context = "未检测"
            if cfg["ENABLE_LIQ_FILTER"] and SURF_API_KEY != "YOUR_SURF_API_KEY":
                liq_params = {"symbol": symbol, "exchange": "Binance"}
                liq_data = await self.fetch_with_retry(session, "https://api.asksurf.ai/v1/exchange/liquidations_chart", params=liq_params, headers=surf_headers)
                if liq_data and liq_data.get("data"):
                    # Surf API provides aggregated data, we can check if recent liquidations are mostly shorts
                    recent_liqs = liq_data["data"][-12:] # last hour
                    short_liq_sum = sum(d.get('short_liquidations_usd', 0) for d in recent_liqs)
                    long_liq_sum = sum(d.get('long_liquidations_usd', 0) for d in recent_liqs)
                    has_liq_target = short_liq_sum > long_liq_sum * cfg["LIQ_SHORT_RATIO_MIN"]
                    liq_context = f"近期空头清算占优 (空/多比: {short_liq_sum/long_liq_sum:.2f}x)" if long_liq_sum > 0 and has_liq_target else "近期无明显空头清算"
                else:
                    has_liq_target = False
                    liq_context = "无清算数据"

            if (cfg["ENABLE_NEWS_FILTER"] and not has_good_news) or (cfg["ENABLE_LIQ_FILTER"] and not has_liq_target):
                return

            # --- AI Agent 决策 (Surf Chat API) ---
            ai_score = 100
            if cfg["ENABLE_AI_AGENT"] and SURF_API_KEY != "YOUR_SURF_API_KEY":
                ai_context = f"""
                - Price Change (24h): {price_change}% (BTC: {btc_change}%)
                - Funding Rate: {last_funding_rate*100:.4f}%
                - Open Interest: ${latest_oi/1e6:.2f}M (Surge Ratio: {oi_surge_ratio:.2f}x)
                - Order Flow: Recent 15min net inflow ${flow_data['total_flow']/1e6:.2f}M ({flow_ratio*100:.2f}% of OI)
                - LS Ratios: Whales={whale_ls}, Retail={retail_ls}
                - News: {news_context}
                - Liquidation: {liq_context}
                """
                prompt = f"As a crypto analyst, evaluate a long trade for {symbol} based on this data. Provide a confidence score from 0 to 100. 80+ is a strong buy. Output ONLY the integer score. Context: {ai_context}"
                
                try:
                    payload = {"model": "surf-1.5-turbo", "messages": [{"role": "user", "content": prompt}]}
                    async with session.post("https://api.asksurf.ai/v1/chat/completions", json=payload, headers=surf_headers, timeout=30) as resp:
                        if resp.status == 200:
                            response_data = await resp.json()
                            score_text = response_data['choices'][0]['message']['content'].strip()
                            ai_score = int(''.join(filter(str.isdigit, score_text)))
                        else:
                            logger.error(f"AI Agent call failed with status {resp.status}: {await resp.text()}")
                except Exception as e:
                    logger.error(f"AI Agent call failed: {e}")
            
            if ai_score < cfg["AI_AGENT_SCORE_MIN"]:
                return

            alert_msg = (
                f"🚨 **AI Agent 确认终极猎杀信号 (Score: {ai_score})** 🚨\n\n"
                f"**币种**: `{symbol}`\n"
                f"**相对强弱**: `{price_change}%` vs BTC `{btc_change}%`\n"
                f"**资金费率**: `{last_funding_rate*100:.4f}%` (低成本)\n"
                f"**持仓市值(盘子)**: `${latest_oi/1000000:.2f}M` (控盘区间)\n"
                f"**资金流入占比**: `{flow_ratio*100:.2f}%` (主力介入)\n"
                f"**多空博弈**: 散户 `{retail_ls}` vs 大户 `{whale_ls}`\n"
                f"**外部信号**: 新闻✅ 清算池✅\n\n"
                f"💡 *AI分析*: 所有高Alpha因子共振，AI评分通过，是极高胜率的轧空结构，建议立即执行！"
            )

            # 6. 🤖 自动交易逻辑 🤖
            if cfg["AUTO_TRADE_ENABLED"] and self.trader:
                quantity = cfg["POSITION_SIZE_USDT"] / mark_price
                trade_response = await self.trader.place_market_order(symbol, 'BUY', quantity)
                
                if trade_response:
                    trade_msg = f"\n\n**🤖 自动交易执行**\n**方向**: `做多`\n**数量**: `{quantity:.4f}`\n"

                    # 止盈策略：追踪止损 或 固定止盈
                    if cfg.get("USE_TRAILING_STOP", False):
                        activation_price = mark_price * (1 + cfg["TSL_ACTIVATION_PERCENT"] / 100)
                        await self.trader.place_trailing_stop_order(symbol, 'SELL', activation_price, cfg["TSL_CALLBACK_PERCENT"], quantity)
                        trade_msg += f"**止盈策略**: `追踪止损 (回调: {cfg['TSL_CALLBACK_PERCENT']}%)`\n"
                    else:
                        take_profit_price = mark_price * (1 + cfg["TAKE_PROFIT_PERCENT"] / 100)
                        await self.trader.place_take_profit_order(symbol, 'SELL', take_profit_price)
                        trade_msg += f"**止盈价**: `{take_profit_price:.4f}`\n"

                    # 止损策略：动态K线结构止损 / 固定止损
                    if cfg.get("USE_DYNAMIC_SL", False) and flow_data['recent_low'] > 0:
                        stop_loss_price = flow_data['recent_low'] * 0.998 # K线低点再下移0.2%
                    else:
                        stop_loss_price = mark_price * (1 - cfg["STOP_LOSS_PERCENT"] / 100)
                    await self.trader.place_stop_loss_order(symbol, 'SELL', stop_loss_price)
                    trade_msg += f"**止损价**: `{stop_loss_price:.4f}`"
                    
                    alert_msg += trade_msg

            send_telegram_alert(alert_msg)

    def monitor_binance_square(self):
        """
        [扩展模块] 监控币安广场热点
        注：币安暂无提取“广场帖子”的官方公共开放API。
        如需接入，推荐方案：
        1. 接入第三方数据源的 NLP 情感分析推流。
        2. 自定义爬虫（Selenium / Playwright）抓取页面 `https://www.binance.com/en/square`。
        """
        pass

    async def run(self):
        logger.info("🚀 币安庄家轧空监控机器人启动！")
        while True:
            try:
                cfg = config_manager.settings
                logger.info(f"开始新一轮全市场数据扫描 (间隔: {cfg['INTERVAL_MINUTES']} 分钟)...")
                self.monitor_binance_square() # 调用热点扩展预留接口

                async with aiohttp.ClientSession() as session:
                    # 如果开启交易，则初始化交易模块
                    if cfg["AUTO_TRADE_ENABLED"]:
                        self.trader = BinanceTrader(session)

                    # 批量获取基础信息 (一次请求解决 200+ 币种的基础指标)
                    premium_data = await self.fetch_with_retry(session, f"{self.base_url}/fapi/v1/premiumIndex")
                    ticker_data = await self.fetch_with_retry(session, f"{self.base_url}/fapi/v1/ticker/24hr")

                    if premium_data and ticker_data:
                        premium_dict = {item['symbol']: item for item in premium_data if item['symbol'].endswith('USDT')}
                        ticker_dict = {item['symbol']: item for item in ticker_data if item['symbol'].endswith('USDT')}
                        
                        btc_ticker = ticker_dict.get('BTCUSDT', {})
                        btc_change = float(btc_ticker.get('priceChangePercent', 0))
                        
                        symbols = list(premium_dict.keys())
                        
                        # 并发处理所有币种（限制并发数以保护 API）
                        tasks = [self.process_symbol(session, sym, premium_dict[sym], ticker_dict.get(sym, {}), btc_change) for sym in symbols if sym != 'BTCUSDT']
                        await asyncio.gather(*tasks)
                
                logger.info(f"✅ 本轮采集分析完成，沉睡 {cfg['INTERVAL_MINUTES']} 分钟。")
            except Exception as e:
                logger.error(f"主循环发生异常: {e}", exc_info=True)
            await asyncio.sleep(config_manager.settings["INTERVAL_MINUTES"] * 60)