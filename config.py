import json
import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── API 密钥 ───────────────────────────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_BINANCE_API_SECRET")
SURF_API_KEY       = os.getenv("SURF_API_KEY",       "YOUR_SURF_API_KEY")
OKX_API_KEY        = os.getenv("OKX_API_KEY",        "YOUR_OKX_API_KEY")
OKX_SECRET_KEY     = os.getenv("OKX_SECRET_KEY",     "YOUR_OKX_SECRET_KEY")
OKX_PASSPHRASE     = os.getenv("OKX_PASSPHRASE",     "")

MAX_CONCURRENT_REQUESTS = 15

DATA_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LOGS_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
CONFIG_FILE = os.path.join(DATA_DIR, "settings.json")


class ConfigManager:
    _BOUNDS: dict[str, tuple] = {
        # 中线
        "LEVERAGE":               (1,      125),
        "POSITION_SIZE_USDT":     (1,      1_000_000),
        "STOP_LOSS_PERCENT":      (0.1,    50.0),
        "TAKE_PROFIT_PERCENT":    (0.1,    500.0),
        "AI_AGENT_SCORE_MIN":     (0,      100),
        "INTERVAL_MINUTES":       (1,      1440),
        "RSI_MAX_ENTRY":          (1,      99),
        "RSI_PERIOD":             (2,      200),
        "OI_SURGE_RATIO":         (0.1,    20.0),
        "FLOW_OI_RATIO":          (0.001,  1.0),
        "TSL_ACTIVATION_PERCENT": (0.1,    100.0),
        "TSL_CALLBACK_PERCENT":   (0.1,    5.0),
        "LIQ_SHORT_RATIO_MIN":    (0.1,    20.0),
        "WHALE_LS_MIN":           (0.01,   10.0),
        "RETAIL_LS_MAX":          (0.01,   10.0),
        # 妖币扫描器
        "YAOBI_SCAN_INTERVAL":    (1,      1440),
        "YAOBI_MIN_SCORE":        (0,      100),
        # 超短线 V3.0
        "SCALP_MAX_POSITIONS":         (1,      20),
        "SCALP_POSITION_USDT":         (1,      1_000_000),
        "SCALP_LEVERAGE":              (1,      125),
        "SCALP_STOP_LOSS_PCT":         (0.1,    50.0),
        "SCALP_TP1_RATIO":             (0.1,    0.9),
        "SCALP_TP2_RATIO":             (0.1,    0.9),
        "SCALP_TP3_TRAIL_PCT":         (0.1,    5.0),
        "SCALP_CANDIDATE_LIMIT":       (20,     500),
        "SCALP_RISK_PER_TRADE_USDT":   (1,      1_000_000),
        "SCALP_MAX_DAILY_LOSS_USDT":   (1,      1_000_000),
        "SCALP_TP1_RR":                (0.5,    10.0),
        "SCALP_TP2_RR":                (1.0,    20.0),
        # V3.0 轧空猎杀 & 动能突破
        "SQUEEZE_OI_DROP_MAJOR":       (0.1,    5.0),
        "SQUEEZE_OI_DROP_MID":         (0.3,    10.0),
        "SQUEEZE_OI_DROP_MEME":        (0.3,    15.0),
        "SQUEEZE_WICK_PCT":            (0.3,    5.0),
        "SQUEEZE_TAKER_MIN":           (0.5,    0.9),
        "BREAKOUT_TAKER_MIN":          (0.4,    0.9),
        "SIGNAL_COOLDOWN_SECONDS":     (1,      60),
        "OI_POLL_INTERVAL":            (5,      60),
        # 一直做空
        "FADE_TRIGGER_PCT":       (1.0,    50.0),
        "FADE_MAX_POSITIONS":     (1,      20),
        "FADE_POSITION_USDT":     (1,      1_000_000),
        "FADE_LEVERAGE":          (1,      125),
        "FADE_STOP_LOSS_PCT":     (0.1,    50.0),
        "FADE_TP1_PCT":           (0.1,    100.0),
        "FADE_TP1_RATIO":         (0.1,    0.9),
        "FADE_TP2_PCT":           (0.1,    200.0),
        "FADE_TP2_RATIO":         (0.1,    0.9),
        "FADE_COOLDOWN_MINUTES":  (1,      480),
        # Funding Rate Reversal
        "FR_OI_SURGE_PCT":        (1.0,    200.0),
        "FR_FUNDING_THRESHOLD":   (-0.1,   -0.0001),
        # 全局市场过滤
        "BTC_GUARD_PCT":          (0.5,    10.0),
    }

    def __init__(self):
        self.default_settings: dict = {
            # ── 中线策略 ──────────────────────────────────────────────────────
            "FUNDING_RATE_THRESHOLD":  0.001,
            "OI_SURGE_RATIO":          1.5,
            "TA_CONFIRMATION_ENABLED": True,
            "RSI_TIMEFRAME":           "15m",
            "RSI_PERIOD":              14,
            "RSI_MAX_ENTRY":           75,
            "INTERVAL_MINUTES":        5,
            "AUTO_TRADE_ENABLED":      False,
            "SWING_PAPER_TRADE":       False,
            "ENABLE_LONG_STRATEGY":    True,
            "ENABLE_SHORT_STRATEGY":   False,
            "LEVERAGE":                5,
            "POSITION_SIZE_USDT":      20.0,
            "STOP_LOSS_PERCENT":       5.0,
            "TAKE_PROFIT_PERCENT":     10.0,
            "USE_TRAILING_STOP":       True,
            "TSL_ACTIVATION_PERCENT":  5.0,
            "TSL_CALLBACK_PERCENT":    1.5,
            "MIN_OI_USDT":             5_000_000.0,
            "MAX_OI_USDT":             50_000_000.0,
            "FLOW_OI_RATIO":           0.05,
            "WHALE_LS_MIN":            1.05,
            "RETAIL_LS_MAX":           0.95,
            "USE_DYNAMIC_SL":          True,
            "ENABLE_NEWS_FILTER":      True,
            "ENABLE_LIQ_FILTER":       True,
            "LIQ_SHORT_RATIO_MIN":     1.5,
            "ENABLE_AI_AGENT":         True,
            "AI_AGENT_SCORE_MIN":      80,
            "ENABLE_MTF_FILTER":       True,
            "ENABLE_OKX_FILTER":       True,
            # ── 妖币扫描器 ────────────────────────────────────────────────────
            "YAOBI_ENABLED":           False,
            "YAOBI_SCAN_INTERVAL":     15,
            "YAOBI_MIN_SCORE":         30,
            "YAOBI_CHAINS":            "eth,bsc,solana,base,arbitrum",
            "OBSIDIAN_VAULT_PATH":     r"C:\BOT\yaobi",
            "COINGLASS_API_KEY":       "",
            # ── 超短线策略 V3.0 (Squeeze Hunter) ─────────────────────────────
            "SCALP_ENABLED":             False,
            "SCALP_AUTO_TRADE":          False,
            "SCALP_ENABLE_LONG":         True,
            "SCALP_ENABLE_SHORT":        True,
            "SCALP_MAX_POSITIONS":       3,
            "SCALP_POSITION_USDT":       50.0,       # 最大保证金上限/笔
            "SCALP_LEVERAGE":            10,
            "SCALP_STOP_LOSS_PCT":       15.0,       # 最大SL保证金%（硬帽）
            "SCALP_TP1_RATIO":           0.5,        # TP1平仓50%
            "SCALP_TP2_RATIO":           0.3,        # TP2再平30%，剩20%追踪
            "SCALP_TP3_TRAIL_PCT":       1.5,        # EMA5追踪止损回撤%
            "SCALP_WATCHLIST":           "",
            "SCALP_CANDIDATE_LIMIT":     80,
            "SCALP_PAPER_TRADE":         False,
            # ── 动态止损 & 风控 ───────────────────────────────────────────────
            "SCALP_USE_DYNAMIC_SL":      True,       # 结构止损（local high/low）
            "SCALP_RISK_PER_TRADE_USDT": 5.0,        # 每笔最大亏损USDT
            "SCALP_MAX_DAILY_LOSS_USDT": 50.0,       # 每日亏损熔断阈值
            "SCALP_TP1_RR":              1.5,        # TP1 = SL距离 × 1.5
            "SCALP_TP2_RR":              3.5,        # TP2 = SL距离 × 3.5
            # ── V3.0 轧空猎杀参数 ─────────────────────────────────────────────
            "SQUEEZE_OI_DROP_MAJOR":     0.5,        # 大币(BTC/ETH等) OI降幅触发%
            "SQUEEZE_OI_DROP_MID":       1.0,        # 中型山寨 OI降幅触发%
            "SQUEEZE_OI_DROP_MEME":      1.5,        # 小币/Meme OI降幅触发%
            "SQUEEZE_WICK_PCT":          1.0,        # 下影线最小反弹%（确认不是一直跌）
            "SQUEEZE_TAKER_MIN":         0.65,       # 轧空信号要求的最低Taker买入比
            # ── V3.0 动能突破参数 ─────────────────────────────────────────────
            "BREAKOUT_TAKER_MIN":        0.55,       # 突破信号要求的最低Taker买入比
            "SIGNAL_COOLDOWN_SECONDS":   5,          # 同一币信号冷却秒数
            "OI_POLL_INTERVAL":          10,         # OI轮询间隔（秒）
            # ── 一直做空策略 ──────────────────────────────────────────────────
            "FADE_ENABLED":            False,
            "FADE_AUTO_TRADE":         False,
            "FADE_PAPER_TRADE":        False,
            "FADE_TRIGGER_PCT":        4.0,
            "FADE_MAX_POSITIONS":      3,
            "FADE_POSITION_USDT":      50.0,
            "FADE_LEVERAGE":           10,
            "FADE_STOP_LOSS_PCT":      1.5,
            "FADE_TP1_PCT":            1.5,
            "FADE_TP1_RATIO":          0.5,
            "FADE_TP2_PCT":            3.0,
            "FADE_TP2_RATIO":          0.3,
            "FADE_WATCHLIST":          "",
            "FADE_COOLDOWN_MINUTES":   30,
            # ── 资金费率反转信号（中线）─────────────────────────────────────
            "ENABLE_FUNDING_REVERSAL": False,
            "FR_OI_SURGE_PCT":         15.0,
            "FR_FUNDING_THRESHOLD":    -0.001,
            # ── 全局市场过滤 ──────────────────────────────────────────────────
            "BTC_GUARD_PCT":           2.0,
        }
        self.settings: dict = self.load()

    def load(self) -> dict:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = self.default_settings.copy()
                merged.update(data)
                return merged
            except Exception as e:
                logging.getLogger(__name__).warning("配置文件损坏，使用默认值: %s", e)
        return self.default_settings.copy()

    def save(self, new_settings: dict) -> None:
        for k, v in new_settings.items():
            if k not in self.default_settings:
                continue
            typ = type(self.default_settings[k])
            if typ is bool:
                converted = str(v).lower() in ("true", "1", "on")
            elif typ is str:
                converted = str(v)
            else:
                try:
                    converted = typ(v)
                except (ValueError, TypeError):
                    continue
            if k in self._BOUNDS and typ in (int, float):
                lo, hi = self._BOUNDS[k]
                converted = max(lo, min(hi, converted))
            self.settings[k] = converted
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=4, ensure_ascii=False)


config_manager = ConfigManager()
