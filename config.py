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
        # 妖币扫描器
        "YAOBI_SCAN_INTERVAL":         (1,      1440),
        "YAOBI_MIN_SCORE":             (0,      100),
        "YAOBI_SURF_TOP_N":            (1,      20),
    }

    def __init__(self):
        self.default_settings: dict = {
            # ── 超短线策略 V3.0 (Squeeze Hunter) ─────────────────────────────
            "SCALP_ENABLED":             False,
            "SCALP_AUTO_TRADE":          False,
            "SCALP_ENABLE_LONG":         True,
            "SCALP_ENABLE_SHORT":        True,
            "SCALP_MAX_POSITIONS":       3,
            "SCALP_POSITION_USDT":       50.0,
            "SCALP_LEVERAGE":            10,
            "SCALP_STOP_LOSS_PCT":       15.0,
            "SCALP_TP1_RATIO":           0.5,
            "SCALP_TP2_RATIO":           0.3,
            "SCALP_TP3_TRAIL_PCT":       1.5,
            "SCALP_WATCHLIST":           "",
            "SCALP_CANDIDATE_LIMIT":     80,
            "SCALP_PAPER_TRADE":         False,
            # ── 动态止损 & 风控 ───────────────────────────────────────────────
            "SCALP_USE_DYNAMIC_SL":      True,
            "SCALP_RISK_PER_TRADE_USDT": 5.0,
            "SCALP_MAX_DAILY_LOSS_USDT": 50.0,
            "SCALP_TP1_RR":              1.5,
            "SCALP_TP2_RR":              3.5,
            # ── V3.0 轧空猎杀参数 ─────────────────────────────────────────────
            "SQUEEZE_OI_DROP_MAJOR":     0.5,
            "SQUEEZE_OI_DROP_MID":       1.0,
            "SQUEEZE_OI_DROP_MEME":      1.5,
            "SQUEEZE_WICK_PCT":          1.0,
            "SQUEEZE_TAKER_MIN":         0.65,
            # ── V3.0 动能突破参数 ─────────────────────────────────────────────
            "BREAKOUT_TAKER_MIN":        0.55,
            "SIGNAL_COOLDOWN_SECONDS":   5,
            "OI_POLL_INTERVAL":          10,
            # ── 妖币扫描器 ────────────────────────────────────────────────────
            "YAOBI_ENABLED":             False,
            "YAOBI_SCAN_INTERVAL":       15,
            "YAOBI_MIN_SCORE":           30,
            "YAOBI_CHAINS":              "eth,bsc,solana,base,arbitrum",
            "OBSIDIAN_VAULT_PATH":       r"C:\BOT\yaobi",
            "COINGLASS_API_KEY":         "",
            "YAOBI_SURF_ENABLED":        True,
            "YAOBI_SURF_TOP_N":          5,
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
