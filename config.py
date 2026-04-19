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
        # 超短线
        "SCALP_TRIGGER_PCT":        (0.5,    20.0),
        "SCALP_WINDOW_MINUTES":    (1,      30),
        "SCALP_VOLUME_MULTIPLIER": (1.0,    10.0),
        "SCALP_ACTIVE_RANGE_PCT":  (1.0,    30.0),
        "SCALP_PULLBACK_PCT":      (0.5,    20.0),
        "SCALP_MEAN_REVERT_PCT":   (1.0,    30.0),
        "SCALP_MAX_POSITIONS":    (1,      20),
        "SCALP_POSITION_USDT":    (1,      1_000_000),
        "SCALP_LEVERAGE":         (1,      125),
        "SCALP_STOP_LOSS_PCT":    (0.1,    50.0),
        "SCALP_TP1_PCT":          (0.1,    100.0),
        "SCALP_TP1_RATIO":        (0.1,    0.9),
        "SCALP_TP2_PCT":          (0.1,    200.0),
        "SCALP_TP2_RATIO":        (0.1,    0.9),
        "SCALP_TP3_TRAIL_PCT":    (0.1,    5.0),
        "SCALP_SLOPE_THRESHOLD":  (0.05,   5.0),
        "SCALP_SLOPE_LOOKBACK":   (3,      30),
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
        # Global market filters
        "BTC_GUARD_PCT":          (0.5,    10.0),
        "SCALP_VWAP_MAX_DEV":     (0.5,    20.0),
        "SCALP_TAKER_RATIO_MIN":  (0.5,    0.9),
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
            # ── 超短线策略 ────────────────────────────────────────────────────
            "SCALP_ENABLED":           False,
            "SCALP_AUTO_TRADE":        False,
            "SCALP_TRIGGER_PCT":       2.5,
            "SCALP_WINDOW_MINUTES":    3,
            "SCALP_VOLUME_MULTIPLIER": 3.0,
            "SCALP_ENTRY_MODE":        "immediate",
            "SCALP_ENABLE_LONG":       True,
            "SCALP_ENABLE_SHORT":      True,
            "SCALP_MAX_POSITIONS":     3,
            "SCALP_POSITION_USDT":     50.0,
            "SCALP_LEVERAGE":          10,
            "SCALP_STOP_LOSS_PCT":     1.2,
            "SCALP_TP1_PCT":           1.0,
            "SCALP_TP1_RATIO":         0.3,
            "SCALP_TP2_PCT":           5.0,
            "SCALP_TP2_RATIO":         0.4,
            "SCALP_TP3_TRAIL_PCT":     1.5,
            "SCALP_WATCHLIST":         "",
            "SCALP_ACTIVE_RANGE_PCT":  3.0,
            "SCALP_PULLBACK_PCT":      1.5,
            "SCALP_MEAN_REVERT_PCT":   4.5,
            "SCALP_PAPER_TRADE":       False,
            "SCALP_SLOPE_THRESHOLD":   0.20,
            "SCALP_SLOPE_LOOKBACK":    8,
            "SCALP_CONFIRM_ENABLED":   True,
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
            # ── 全局市场过滤（超短线 & 一直做空共用）─────────────────────────
            "BTC_GUARD_PCT":           2.0,
            "SCALP_VWAP_MAX_DEV":      3.0,
            "SCALP_TAKER_RATIO_MIN":   0.55,
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
