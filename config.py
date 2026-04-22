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
BINANCE_SQUARE_COOKIE      = os.getenv("BINANCE_SQUARE_COOKIE",      "")
BINANCE_SQUARE_CSRF_TOKEN  = os.getenv("BINANCE_SQUARE_CSRF_TOKEN",  "")
BINANCE_SQUARE_BNC_UUID    = os.getenv("BINANCE_SQUARE_BNC_UUID",    "")
BINANCE_SQUARE_OPENAPI_KEY = os.getenv("BINANCE_SQUARE_OPENAPI_KEY", "")

PANEL_HOST       = os.getenv("PANEL_HOST", "127.0.0.1")
PANEL_PORT       = int(os.getenv("PANEL_PORT", "8000"))
PANEL_TOKEN      = os.getenv("PANEL_TOKEN", "")
PANEL_LOCAL_ONLY = os.getenv("PANEL_LOCAL_ONLY", "true").lower() not in ("0", "false", "no", "off")

MAX_CONCURRENT_REQUESTS = 15

DATA_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LOGS_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
CONFIG_FILE = os.path.join(DATA_DIR, "settings.json")
MASKED_SECRET = "********"
SENSITIVE_SETTING_KEYS = {"COINGLASS_API_KEY"}


def _valid_secret(value: str | None, placeholder: str = "") -> bool:
    if not value:
        return False
    value = str(value).strip()
    return bool(value and value != placeholder and not value.startswith("YOUR_"))


def _split_env_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def configured_surf_keys() -> list[str]:
    keys: list[str] = []
    keys.extend(_split_env_list(os.getenv("SURF_API_KEYS", "")))
    keys.append(SURF_API_KEY)
    for i in range(2, 11):
        keys.append(os.getenv(f"SURF_API_KEY_{i}", ""))
    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if _valid_secret(key, "YOUR_SURF_API_KEY") and key not in seen:
            deduped.append(key)
            seen.add(key)
    return deduped


def surf_credentials_status() -> dict:
    return {
        "enabled": bool(configured_surf_keys()),
        "key_count": len(configured_surf_keys()),
        "multi_key_env": bool(os.getenv("SURF_API_KEYS", "")),
    }


_surf_key_cursor = 0


def next_surf_api_key() -> str:
    global _surf_key_cursor
    keys = configured_surf_keys()
    if not keys:
        return ""
    key = keys[_surf_key_cursor % len(keys)]
    _surf_key_cursor += 1
    return key


def configured_okx_credentials() -> list[dict]:
    creds: list[dict] = []

    def add(key: str | None, secret: str | None, passphrase: str | None, label: str) -> None:
        if (_valid_secret(key, "YOUR_OKX_API_KEY")
                and _valid_secret(secret, "YOUR_OKX_SECRET_KEY")
                and _valid_secret(passphrase, "")):
            creds.append({
                "key": str(key).strip(),
                "secret": str(secret).strip(),
                "passphrase": str(passphrase).strip(),
                "label": label,
            })

    add(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, "OKX_API_KEY")
    for i in range(2, 11):
        add(
            os.getenv(f"OKX_API_KEY_{i}", ""),
            os.getenv(f"OKX_SECRET_KEY_{i}", ""),
            os.getenv(f"OKX_PASSPHRASE_{i}", ""),
            f"OKX_API_KEY_{i}",
        )

    raw_json = os.getenv("OKX_API_CREDENTIALS", "")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                for idx, item in enumerate(parsed, 1):
                    if isinstance(item, dict):
                        add(
                            item.get("api_key") or item.get("key"),
                            item.get("secret_key") or item.get("secret"),
                            item.get("passphrase"),
                            f"OKX_API_CREDENTIALS[{idx}]",
                        )
        except Exception as e:
            logging.getLogger(__name__).warning("OKX_API_CREDENTIALS 解析失败: %s", e)

    deduped: list[dict] = []
    seen: set[str] = set()
    for cred in creds:
        if cred["key"] in seen:
            continue
        deduped.append(cred)
        seen.add(cred["key"])
    return deduped


def okx_credentials_status() -> dict:
    creds = configured_okx_credentials()
    return {
        "enabled": bool(creds),
        "key_count": len(creds),
        "labels": [c["label"] for c in creds],
        "base_url": "https://web3.okx.com",
    }


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
        "SCALP_MAX_DAILY_LOSS_R":      (1,      100),
        "SCALP_TP1_RR":                (0.5,    10.0),
        "SCALP_TP2_RR":                (1.0,    20.0),
        "SCALP_TIME_STOP_MINUTES":     (1,      360),
        "SCALP_TP2_TIMEOUT_MINUTES":   (5,      720),
        "SCALP_STRUCTURE_TRAIL_BARS":  (3,      30),
        "FEE_RATE_PER_SIDE":           (0.0,    0.01),
        "SLIPPAGE_RATE_PER_SIDE":      (0.0,    0.05),
        # V3.0 轧空猎杀 & 动能突破
        "SQUEEZE_OI_DROP_MAJOR":       (0.1,    5.0),
        "SQUEEZE_OI_DROP_MID":         (0.3,    10.0),
        "SQUEEZE_OI_DROP_MEME":        (0.3,    15.0),
        "SQUEEZE_WICK_PCT":            (0.3,    5.0),
        "SQUEEZE_TAKER_MIN":           (0.5,    0.9),
        "BREAKOUT_TAKER_MIN":          (0.4,    0.9),
        "BREAKOUT_MIN_PCT":            (0.01,   5.0),
        "BREAKOUT_ATR_MULT":           (0.0,    5.0),
        "BREAKOUT_MIN_VOL_RATIO":      (0.01,   5.0),
        "SIGNAL_COOLDOWN_SECONDS":     (1,      60),
        "OI_POLL_INTERVAL":            (5,      60),
        "BTC_GUARD_PCT":               (0.1,    10.0),
        # 妖币扫描器
        "YAOBI_SCAN_INTERVAL":         (1,      1440),
        "YAOBI_MIN_SCORE":             (0,      100),
        "YAOBI_MIN_ANOMALY_SCORE":     (0,      100),
        "YAOBI_SURF_TOP_N":            (1,      20),
        "YAOBI_SQUARE_ROWS":           (1,      200),
        "YAOBI_OKX_HOT_LIMIT":         (1,      100),
        "YAOBI_OKX_HEAVY_TOP_N":       (1,      120),
        "YAOBI_OKX_PRICE_BATCH_SIZE":  (1,      100),
        "OKX_MIN_REQUEST_INTERVAL":    (0.02,   5.0),
        "SURF_MIN_REQUEST_INTERVAL":   (0.02,   5.0),
    }

    def __init__(self):
        self.default_settings: dict = {
            # ── 超短线策略 V3.0 (Squeeze Hunter) ─────────────────────────────
            "SCALP_ENABLED":             False,
            "SCALP_AUTO_TRADE":          False,
            "SCALP_ENABLE_LONG":         True,
            "SCALP_ENABLE_SHORT":        True,
            "SCALP_MAX_POSITIONS":       3,
            "SCALP_POSITION_USDT":       100.0,
            "SCALP_LEVERAGE":            10,
            "SCALP_STOP_LOSS_PCT":       50.0,
            "SCALP_TP1_RATIO":           0.15,
            "SCALP_TP2_RATIO":           0.25,
            "SCALP_TP3_TRAIL_PCT":       5.0,
            "SCALP_WATCHLIST":           "",
            "SCALP_CANDIDATE_LIMIT":     80,
            "SCALP_PAPER_TRADE":         False,
            # ── 动态止损 & 风控 ───────────────────────────────────────────────
            "SCALP_USE_DYNAMIC_SL":      True,
            "SCALP_RISK_PER_TRADE_USDT": 20.0,
            "SCALP_MAX_DAILY_LOSS_USDT": 200.0,
            "SCALP_MAX_DAILY_LOSS_R":    10.0,
            "SCALP_TP1_RR":              1.5,
            "SCALP_TP2_RR":              3.5,
            "SCALP_TIME_STOP_MINUTES":   30,
            "SCALP_TP2_TIMEOUT_MINUTES": 120,
            "SCALP_STRUCTURE_TRAIL_BARS": 8,
            "FEE_RATE_PER_SIDE":         0.0004,
            "SLIPPAGE_RATE_PER_SIDE":    0.0005,
            # ── V3.0 轧空猎杀参数 ─────────────────────────────────────────────
            "SQUEEZE_OI_DROP_MAJOR":     0.5,
            "SQUEEZE_OI_DROP_MID":       1.0,
            "SQUEEZE_OI_DROP_MEME":      1.5,
            "SQUEEZE_WICK_PCT":          1.0,
            "SQUEEZE_TAKER_MIN":         0.65,
            # ── V3.0 动能突破参数 ─────────────────────────────────────────────
            "BREAKOUT_TAKER_MIN":        0.62,
            "BREAKOUT_MIN_PCT":          0.10,
            "BREAKOUT_ATR_MULT":         0.5,
            "BREAKOUT_MIN_VOL_RATIO":    0.35,
            "SIGNAL_COOLDOWN_SECONDS":   20,
            "OI_POLL_INTERVAL":          10,
            "BTC_GUARD_PCT":             2.0,
            # ── 妖币扫描器 ────────────────────────────────────────────────────
            "YAOBI_ENABLED":             False,
            "YAOBI_SCAN_INTERVAL":       15,
            "YAOBI_MIN_SCORE":           30,
            "YAOBI_MIN_ANOMALY_SCORE":   35,
            "YAOBI_CHAINS":              "eth,bsc,solana,base,arbitrum",
            "OBSIDIAN_VAULT_PATH":       r"C:\BOT\yaobi",
            "COINGLASS_API_KEY":         "",
            "YAOBI_SURF_ENABLED":        True,
            "YAOBI_SURF_TOP_N":          5,
            "YAOBI_SQUARE_ENABLED":      True,
            "YAOBI_SQUARE_ROWS":         50,
            "YAOBI_OKX_ENABLED":         True,
            "YAOBI_OKX_HOT_ENABLED":     True,
            "YAOBI_OKX_HOT_LIMIT":       50,
            "YAOBI_OKX_HEAVY_TOP_N":     40,
            "YAOBI_OKX_PRICE_BATCH_SIZE": 100,
            "OKX_MIN_REQUEST_INTERVAL":  0.20,
            "SURF_MIN_REQUEST_INTERVAL": 0.20,
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
            if k in SENSITIVE_SETTING_KEYS and str(v).strip() in ("", MASKED_SECRET):
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
