# 🚀 币安异常轧空监控机器人配置
import os
import json

# ==================== API 密钥配置 ====================
# 强烈建议使用环境变量来保护你的 API 密钥
# 在终端中设置:
# export BINANCE_API_KEY="YOUR_API_KEY"
# export BINANCE_API_SECRET="YOUR_API_SECRET"
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_BINANCE_API_SECRET")
# 新增: Surf.ai 统一数据平台 API Key
SURF_API_KEY = os.getenv("SURF_API_KEY", "YOUR_SURF_API_KEY")

# ==================== Telegram 警报配置 ====================
# 请填写你从 @BotFather 获取的 Bot Token 和你的 Chat ID
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

# ==================== 运行参数 ====================
MAX_CONCURRENT_REQUESTS = 15    # 并发请求限制 (保护机制，避免触发币安 API 2400次/分钟的限流)

# ==================== 数据存储 ====================
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONFIG_FILE = os.path.join(DATA_DIR, "settings.json")

class ConfigManager:
    def __init__(self):
        self.default_settings = {
            "FUNDING_RATE_THRESHOLD": 0.001,
            "OI_SURGE_RATIO": 1.5,
            "TA_CONFIRMATION_ENABLED": True,
            "RSI_TIMEFRAME": "15m",
            "RSI_PERIOD": 14,
            "RSI_MAX_ENTRY": 75,
            "INTERVAL_MINUTES": 5,
            "AUTO_TRADE_ENABLED": False,
            "POSITION_SIZE_USDT": 20.0,
            "STOP_LOSS_PERCENT": 5.0,
            "TAKE_PROFIT_PERCENT": 10.0,
            "USE_TRAILING_STOP": True,      # 新增: 是否启用追踪止损
            "TSL_ACTIVATION_PERCENT": 5.0,  # 新增: 追踪止损激活阈值 (盈利5%后启动)
            "TSL_CALLBACK_PERCENT": 1.5,    # 新增: 追踪止损回撤比例 (从最高点回撤1.5%后止盈)
            "MIN_OI_USDT": 5000000.0,     # OI下限(500万) -> 对应小市值
            "MAX_OI_USDT": 50000000.0,    # OI上限(5000万)
            "FLOW_OI_RATIO": 0.05,        # 资金流入占比阈值(5%)
            "WHALE_LS_MIN": 1.05,         # 大户多空比最小要求(看多)
            "RETAIL_LS_MAX": 0.95,        # 散户多空比最大要求(看空)
            "USE_DYNAMIC_SL": True,       # 开启K线结构低点动态止损
            "ENABLE_NEWS_FILTER": True,   # 新增: 是否启用新闻利好过滤
            "ENABLE_LIQ_FILTER": True,    # 新增: 是否启用清算热图过滤
            "LIQ_SHORT_RATIO_MIN": 1.5,   # 新增: 空头清算量/多头清算量最小比例
            "ENABLE_AI_AGENT": True,      # 新增: 是否启用AI Agent决策
            "AI_AGENT_SCORE_MIN": 80      # 新增: AI Agent最低开仓评分
        }
        self.settings = self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    merged = self.default_settings.copy()
                    merged.update(data)
                    return merged
            except Exception:
                pass
        return self.default_settings.copy()

    def save(self, new_settings):
        for k, v in new_settings.items():
            if k in self.default_settings:
                # 自动类型转换，确保前端传来的字符串被正确解析
                typ = type(self.default_settings[k])
                self.settings[k] = (str(v).lower() in ['true', '1', 'on']) if typ == bool else typ(v)
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=4)

config_manager = ConfigManager()