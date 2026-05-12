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
# 妖币雷达推送通知 (Telegram / Discord)，留空即不推。
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN",  "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID",    "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SURF_API_KEY       = os.getenv("SURF_API_KEY",       "YOUR_SURF_API_KEY")
OKX_API_KEY        = os.getenv("OKX_API_KEY",        "YOUR_OKX_API_KEY")
OKX_SECRET_KEY     = os.getenv("OKX_SECRET_KEY",     "YOUR_OKX_SECRET_KEY")
OKX_PASSPHRASE     = os.getenv("OKX_PASSPHRASE",     "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY",     "")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY",     "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "")
DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY",   "")
MINIMAX_API_KEY    = os.getenv("MINIMAX_API_KEY",    "")
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


def ai_credentials_status() -> dict:
    providers = {
        "openai": _valid_secret(os.getenv("OPENAI_API_KEY", OPENAI_API_KEY)),
        "gemini": _valid_secret(os.getenv("GEMINI_API_KEY", GEMINI_API_KEY)),
        "anthropic": _valid_secret(os.getenv("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)),
        "deepseek": _valid_secret(os.getenv("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)),
        "minimax": _valid_secret(os.getenv("MINIMAX_API_KEY", MINIMAX_API_KEY)),
    }
    return {
        "enabled": any(providers.values()),
        "providers": providers,
        "provider_count": sum(1 for enabled in providers.values() if enabled),
    }


class ConfigManager:
    PROFILE_VERSION = 2026050101
    # ── 进化策略锁定参数（Evolver 禁止修改）────────────────────────────────
    LOCKED_PARAMS = frozenset({
        "SCALP_MAX_POSITIONS",
        "SCALP_POSITION_USDT",
        "SCALP_RISK_PER_TRADE_USDT",
    })

    # ── 无效进化参数（当前无无效参数）──
    INEFFECTIVE_PARAMS = frozenset()

    # ── 进化策略允许修改的参数边界 ─────────────────────────────────────────
    # {key: [min, max]} 只有在此列表中的参数才允许自动修改
    PARAM_BOUNDS: dict[str, list[float]] = {
        "SCALP_STOP_LOSS_PCT":             [0.3,   10.0],
        "SCALP_TP1_RR":                    [0.5,    5.0],
        "SCALP_TP2_RR":                    [0.5,    8.0],
        "SCALP_TP1_RATIO":                 [0.1,    0.6],
        "SCALP_TP2_RATIO":                 [0.1,    0.5],
        "SCALP_TP3_TRAIL_PCT":             [0.2,   10.0],
        "SCALP_TIME_STOP_MINUTES":         [5,     360],
        "SCALP_TP2_TIMEOUT_MINUTES":       [5,     360],
        "SCALP_TP_CONFIRM_TICKS":          [1,       5],
        "SCALP_TREND_LATE_SIZE_MULT":      [0.1,    1.0],
        "SCALP_NET_BREAKEVEN_LOCK_PCT":    [0.0,    2.0],
        "SCALP_TP1_SOFT_BREAKEVEN_PCT":    [0.0,    2.0],
        "SCALP_REVERSAL_STOP_SL_FRACTION": [0.1,    1.0],
        "SQUEEZE_TAKER_MIN":              [0.50,   0.85],
        "SQUEEZE_TAKER_MIN_LONG":         [0.50,   0.85],
        "SQUEEZE_TAKER_MIN_SHORT":        [0.50,   0.85],
        "BREAKOUT_TAKER_MIN":             [0.45,   0.85],
        "BREAKOUT_MIN_PCT":               [0.05,   3.0],
        "BREAKOUT_ATR_MULT":              [0.2,    3.0],
        "BREAKOUT_ATR_MIN_PCT":           [0.2,    5.0],
        "BREAKOUT_ATR_MAX_PCT":           [0.3,    8.0],
        "BREAKOUT_MIN_VOL_RATIO":          [0.2,    3.0],
        "BREAKOUT_MAX_PREMOVE_5M_PCT":    [0.0,    5.0],
        "BREAKOUT_MAX_PREMOVE_15M_PCT":   [0.0,   10.0],
        "BREAKOUT_MAX_EMA20_DEVIATION_PCT":[0.0,   10.0],
        "CONTINUATION_TAKER_MIN":          [0.45,   0.85],
        "CONTINUATION_TAKER_MIN_LONG":     [0.45,   0.85],
        "CONTINUATION_TAKER_MIN_SHORT":    [0.45,   0.85],
        "CONTINUATION_HOT_TAKER_MIN":      [0.45,   0.85],
        "CONTINUATION_MIN_PULLBACK_PCT":   [0.05,   3.0],
        "CONTINUATION_ATR_MIN_PCT":        [0.0,    5.0],
        "CONTINUATION_ATR_MAX_PCT":        [0.0,    8.0],
        "CONTINUATION_MAX_EMA20_DEVIATION_PCT": [0.0, 10.0],
        "BTC_GUARD_REJECT_PCT":           [0.2,    5.0],
        "BTC_GUARD_WARN_PCT":             [0.1,    3.0],
        "SIGNAL_COOLDOWN_SECONDS":        [5,     120],
        "SCALP_SYMBOL_BAN_SL_COUNT":      [1,      10],
        "SCALP_SYMBOL_BAN_LOSS_R":        [0.5,   20.0],
        "SCALP_SYMBOL_BAN_DURATION_MINUTES":[0,   1440],
        "SCALP_MAX_DAILY_LOSS_USDT":      [10,  10000],
        "SCALP_MAX_DAILY_LOSS_R":         [1,     100],
        "STRATEGY_启动型_MIN_PRICE_CHANGE_15M": [0.05, 3.0],
        "STRATEGY_启动型_MAX_PRICE_CHANGE_1H":  [0.1,  8.0],
        "STRATEGY_启动型_MIN_VOL_RATIO":       [0.5,  5.0],
        "STRATEGY_启动型_MIN_OI_CHANGE_15M":    [0.1,  5.0],
        "STRATEGY_OI爆发_MIN_OI_CHANGE_1H":     [1.0, 20.0],
        "STRATEGY_OI爆发_MIN_VOL_RATIO":        [0.5,  8.0],
        "STRATEGY_OI爆发_MAX_PRICE_CHANGE_15M": [0.5, 10.0],
        "STRATEGY_静默建仓_MAX_PRICE_CHANGE_15M":[0.1,  3.0],
        "STRATEGY_静默建仓_MIN_OI_CHANGE_15M":  [0.05, 3.0],
        "STRATEGY_突破前夜_MIN_OI_CHANGE_15M":  [0.1,  5.0],
        "STRATEGY_突破前夜_MIN_VOL_RATIO":      [0.5,  5.0],
        "STRATEGY_早期启动_MIN_PRICE_CHANGE_15M":[0.1,  5.0],
        "STRATEGY_早期启动_MIN_PRICE_CHANGE_1H": [0.1, 10.0],
        "STRATEGY_早期启动_MAX_PRICE_CHANGE_24H":[5.0, 50.0],
        "STRATEGY_早期启动_MIN_VOL_RATIO":      [0.5,  5.0],
        "STRATEGY_早期启动_MIN_OI_CHANGE_15M":  [0.1,  5.0],
                "STRATEGY_WEIGHTS.STARTUP":              [0.0,  2.0],
        "STRATEGY_WEIGHTS.OI_EXPLOSION":         [0.0,  2.0],
        "STRATEGY_WEIGHTS.QUIET_ACCUM":           [0.0,  2.0],
        "STRATEGY_WEIGHTS.PRE_BREAKOUT":          [0.0,  2.0],
        "STRATEGY_WEIGHTS.EARLY_START":           [0.0,  2.0],
        "STRATEGY_WEIGHTS.UNKNOWN":               [0.0,  1.0],
        "STRATEGY_ENABLED.STARTUP":              [0.0,  1.0],
        "STRATEGY_ENABLED.OI_EXPLOSION":         [0.0,  1.0],
        "STRATEGY_ENABLED.QUIET_ACCUM":           [0.0,  1.0],
        "STRATEGY_ENABLED.PRE_BREAKOUT":          [0.0,  1.0],
        "STRATEGY_ENABLED.EARLY_START":           [0.0,  1.0],
    }
    PROFILE_MIGRATION_DEFAULTS = {
        # 当前回测/实盘观测后确认要强制落地的策略默认值。
        # 交易模式、开关、仓位金额、杠杆和 API 密钥不在这里覆盖。
        "SCALP_MAX_POSITIONS": 3,
        "SCALP_TP1_RATIO": 0.30,
        "SCALP_TP2_RATIO": 0.30,
        "SCALP_TP3_TRAIL_PCT": 8.0,
        "SCALP_CANDIDATE_LIMIT": 40,
        "SCALP_CANDIDATE_SOURCE_MODE": "YAOBI_ONLY",
        "SCALP_MAX_DAILY_LOSS_USDT": 200.0,
        "SCALP_MAX_DAILY_LOSS_R": 10.0,
        "SCALP_TP1_RR": 1.5,
        "SCALP_TP2_RR": 3.0,
        "SCALP_TIME_STOP_MINUTES": 45,
        "SCALP_TP2_TIMEOUT_MINUTES": 120,
        "SCALP_STRUCTURE_TRAIL_BARS": 20,
        "SCALP_TP3_AGGRESSIVE_RUNNER": False,
        "SCALP_SKIP_TP1_IN_STRONG_TREND": False,
        "SCALP_NET_BREAKEVEN_LOCK_PCT": 0.15,
        "SCALP_TP1_SOFT_BREAKEVEN_PCT": 0.35,
        "SCALP_REVERSAL_STOP_SL_FRACTION": 0.40,
        "SCALP_WS_STALE_SECONDS": 90,
        "SCALP_POSITION_CHECK_INTERVAL_SECONDS": 10,
        "SCALP_POSITION_STALE_SECONDS": 60,
        "SCALP_SKIP_UNKNOWN_STATE": True,
        "SCALP_MAX_CANDIDATE_AGE_MINUTES": 180,
        # L5: TP wick 双 tick 确认
        "SCALP_TP_CONFIRM_TICKS": 2,
        # L3: TREND_LATE 状态自动半仓
        "SCALP_TREND_LATE_SIZE_MULT": 0.5,
        "SCALP_BLOCK_TREND_LATE_ENTRY": True,
        # L7: 单币熔断阈值 + 滑动窗口 + 自定义熔断时长
        "SCALP_SYMBOL_BAN_WINDOW_MINUTES": 120,
        "SCALP_SYMBOL_BAN_SL_COUNT": 2,
        "SCALP_SYMBOL_BAN_LOSS_R": 2.0,
        "SCALP_SYMBOL_BAN_DURATION_MINUTES": 0,
        "FEE_RATE_PER_SIDE": 0.0004,
        "SLIPPAGE_RATE_PER_SIDE": 0.0005,
        "SQUEEZE_OI_DROP_MAJOR": 0.5,
        "SQUEEZE_OI_DROP_MID": 1.0,
        "SQUEEZE_OI_DROP_MEME": 1.5,
        "SQUEEZE_WICK_PCT": 1.0,
        # L2: squeeze taker 默认更敏感（旧 0.65 → 0.58），新增多/空独立项
        "SQUEEZE_TAKER_MIN": 0.60,
        "SQUEEZE_TAKER_MIN_LONG": 0.62,
        "SQUEEZE_TAKER_MIN_SHORT": 0.62,
        # L4: BTC 守卫分级（旧 BTC_GUARD_PCT 仅作 reject 兜底）
        "BTC_GUARD_REJECT_PCT": 1.5,
        "BTC_GUARD_WARN_PCT": 0.8,
        "BREAKOUT_TAKER_MIN": 0.62,
        "BREAKOUT_MIN_PCT": 0.15,
        "BREAKOUT_ATR_MULT": 0.7,
        # 放宽 ATR 区间：把妖币（ATR 1.5~2.5%）和大币（ATR 0.3~0.5%）都纳入
        "BREAKOUT_ATR_MIN_PCT": 0.50,
        "BREAKOUT_ATR_MAX_PCT": 2.00,
        # 放宽 volume 门槛：原 0.6 在深夜震荡市过滤太多
        "BREAKOUT_MIN_VOL_RATIO": 0.40,
        # ATR 自适应：根据 BTC 当前 1m ATR 动态缩放上下限
        "BREAKOUT_ATR_ADAPTIVE": True,
        "MARKET_VOL_BTC_ATR_BASELINE": 0.15,
        "MARKET_VOL_SCALE_MIN": 0.8,
        "MARKET_VOL_SCALE_MAX": 2.5,
        "BREAKOUT_MAX_PREMOVE_5M_PCT": 1.2,
        "BREAKOUT_MAX_PREMOVE_15M_PCT": 2.5,
        "BREAKOUT_MAX_PREMOVE_30M_PCT": 2.5,
        "BREAKOUT_MAX_EMA20_DEVIATION_PCT": 3.0,
        "CONTINUATION_PULLBACK_ENABLED": True,
        "CONTINUATION_TAKER_MIN": 0.58,
        "CONTINUATION_TAKER_MIN_LONG": 0.62,
        "CONTINUATION_TAKER_MIN_SHORT": 0.52,
        "CONTINUATION_HOT_TAKER_MIN": 0.52,
        "CONTINUATION_HOT_TAKER_MIN_LONG": 0.60,
        "CONTINUATION_HOT_TAKER_MIN_SHORT": 0.52,
        "CONTINUATION_MIN_PULLBACK_PCT": 0.20,
        "CONTINUATION_RECLAIM_LOOKBACK": 3,
        "CONTINUATION_ATR_MIN_PCT": 0.0,
        "CONTINUATION_ATR_MIN_PCT_LONG": 0.70,
        "CONTINUATION_ATR_MIN_PCT_SHORT": 0.30,
        "CONTINUATION_ATR_MAX_PCT": 2.50,
        "CONTINUATION_ATR_MAX_PCT_LONG": 2.00,
        "CONTINUATION_ATR_MAX_PCT_SHORT": 1.50,
        "CONTINUATION_MAX_EMA20_DEVIATION_PCT": 4.00,
        "CONTINUATION_MAX_EMA20_DEVIATION_PCT_LONG": 2.50,
        "CONTINUATION_MAX_EMA20_DEVIATION_PCT_SHORT": 2.50,
        "SIGNAL_COOLDOWN_SECONDS": 30,
        "OI_POLL_INTERVAL": 10,
        "SCALP_OI_PREFETCH_TOP_N": 30,
        "BTC_GUARD_PCT": 1.5,
        "SCALP_SURF_NEWS_ENABLED": False,
        "SCALP_SURF_NEWS_INTERVAL_MINUTES": 60,
        "SCALP_SURF_NEWS_TOP_N": 8,
                "SCALP_CIRCUIT_BREAKER_WINDOW": 300,
        "SCALP_CIRCUIT_BREAKER_THRESHOLD": 15,
        "SCALP_SURF_ENTRY_AI_ENABLED": False,
        "SCALP_SURF_ENTRY_AI_MIN_ABS_CHANGE": 80.0,
        "SCALP_USE_YAOBI_CONTEXT": True,
        "SCALP_REQUIRE_YAOBI_CONTEXT": True,
        "SCALP_YAOBI_CONTEXT_TOP_N": 30,
        "SCALP_YAOBI_MIN_SCORE": 60,
        "SCALP_YAOBI_MIN_ANOMALY_SCORE": 50,
        "SCALP_YAOBI_BLOCK_DECISION_BAN": True,
        "SCALP_YAOBI_BLOCK_WAIT_CONFIRM": True,
        "SCALP_YAOBI_BLOCK_HIGH_RISK": True,
        "SCALP_YAOBI_DIRECTION_GUARD": False,
        "SCALP_YAOBI_FUNDING_OI_GUARD": True,
        # B1 软警戒：默认不再硬拒绝拥挤方向，改为缩仓 (Hard=False → 缩到 SOFT_MULT)
        "SCALP_YAOBI_FUNDING_OI_GUARD_HARD": False,
        "SCALP_YAOBI_FUNDING_OI_SOFT_MULT": 0.5,
        # B2 SQUEEZE 多单：要求 OI 在最近 N 秒已停止下行（防反弹失败二次砸）
        "SCALP_SQUEEZE_OI_STABILIZE_ENABLED": True,
        "SCALP_SQUEEZE_OI_STABILIZE_LOOKBACK_SEC": 60,
        "SCALP_SQUEEZE_OI_REBOUND_PCT": 0.12,
        # B3 AI 剧本到期检查：opportunity_expires_at < now 时不再放行
        "SCALP_OPPORTUNITY_EXPIRY_GUARD": True,
        "SCALP_OPPORTUNITY_GUARD_ENABLED": True,
        "SCALP_REQUIRE_OPPORTUNITY_QUEUE": True,
        "SCALP_REQUIRE_OPPORTUNITY_PERMISSION": True,
        "SCALP_YAOBI_FUNDING_EXTREME_PCT": 0.05,
        "SCALP_YAOBI_OI_GUARD_MIN_24H_PCT": 50.0,
        # 妖币雷达推送通知（Telegram / Discord webhook）
        "YAOBI_NOTIFIER_ENABLED": False,
        "YAOBI_NOTIFIER_MIN_TIER": "L2_AMBUSH",  # L1_MAIN（只推 L1）/ L2_AMBUSH（L1+L2）/ ALL（含 RISK）
        "YAOBI_NOTIFIER_MAX_PER_BATCH": 8,       # 单条消息最多列几个币
        "YAOBI_SURF_NEWS_ENABLED": False,
        "YAOBI_SURF_NEWS_TOP_N": 20,
        "YAOBI_SURF_FALLBACK_SEARCH_LIMIT": 3,
        "YAOBI_SURF_AI_ENABLED": True,
        "YAOBI_SURF_TOP_N": 6,
        "YAOBI_SURF_AI_MODEL": "surf-ask",
        "YAOBI_OKX_HOT_LIMIT": 50,
        "YAOBI_OKX_HEAVY_TOP_N": 40,
        "YAOBI_OKX_PRICE_BATCH_SIZE": 100,
        "YAOBI_OKX_SEARCH_CACHE_MINUTES": 60,
        "YAOBI_FUTURES_TOP_N": 120,
        "YAOBI_BINANCE_SHORT_INTEL_ENABLED": True,
        "YAOBI_BINANCE_LIQUIDATION_WS_ENABLED": True,
        "YAOBI_OPPORTUNITY_TOP_N": 6,
        "YAOBI_OPPORTUNITY_MIN_SCORE": 45,
        "YAOBI_AI_ENABLED": True,
        "YAOBI_AI_REQUIRED_FOR_PERMISSION": True,
        "YAOBI_DUAL_AI_CONSENSUS_REQUIRED": False,
        "YAOBI_SURF_DIRECTION_MIN_CONFIDENCE": 55,
        "YAOBI_AI_PROVIDER_PRIORITY": "minimax",
        "YAOBI_AI_MODEL_OPENAI": "gpt-4o-mini",
        "YAOBI_AI_MODEL_GEMINI": "gemini-2.5-flash",
        "YAOBI_AI_MODEL_ANTHROPIC": "claude-3-5-haiku-latest",
        "YAOBI_AI_MODEL_DEEPSEEK": "deepseek-v4-flash",
        "YAOBI_AI_MODEL_MINIMAX": "MiniMax-M2.7",
        "YAOBI_AI_MAX_SYMBOLS_PER_RUN": 6,
        "YAOBI_AI_TOP_OUTPUT": 6,
        "YAOBI_AI_FAILURE_FALLBACK_ENABLED": True,
        "YAOBI_AI_FAILURE_FALLBACK_MIN_SCORE": 45,
        "YAOBI_AI_MIN_INTERVAL_MINUTES": 15,
        "YAOBI_AI_CACHE_TTL_MINUTES": 30,
        "YAOBI_PLAYBOOK_TTL_MINUTES": 45,
        "YAOBI_AI_DAILY_USD_CAP": 1.0,
        # ── 策略分类参数 ────────────────────────────────────────────────────
        "STRATEGY_启动型_MIN_PRICE_CHANGE_15M": 0.3,
        "STRATEGY_启动型_MAX_PRICE_CHANGE_1H": 2.0,
        "STRATEGY_启动型_MIN_VOL_RATIO": 1.2,
        "STRATEGY_启动型_MIN_OI_CHANGE_15M": 0.5,
        "STRATEGY_启动型_MAX_OI_CHANGE_1H": 3.0,
        "STRATEGY_OI爆发_MIN_OI_CHANGE_15M": 2.0,
        "STRATEGY_OI爆发_MIN_OI_CHANGE_1H": 5.0,
        "STRATEGY_OI爆发_MIN_VOL_RATIO": 1.5,
        "STRATEGY_OI爆发_MAX_PRICE_CHANGE_15M": 5.0,
        "STRATEGY_静默建仓_MAX_PRICE_CHANGE_15M": 1.0,
        "STRATEGY_静默建仓_MAX_PRICE_CHANGE_1H": 2.0,
        "STRATEGY_静默建仓_MIN_OI_CHANGE_15M": 0.3,
        "STRATEGY_静默建仓_MIN_OI_CHANGE_1H": 1.0,
        "STRATEGY_静默建仓_MIN_VOL_RATIO": 1.0,
        "STRATEGY_静默建仓_MAX_FUNDING_ABS": 0.01,
        "STRATEGY_突破前夜_MIN_OI_CHANGE_15M": 0.5,
        "STRATEGY_突破前夜_MIN_VOL_RATIO": 1.2,
        "STRATEGY_突破前夜_MAX_RETRACE_PCT": 0.5,
        "STRATEGY_早期启动_MIN_PRICE_CHANGE_15M": 0.5,
        "STRATEGY_早期启动_MIN_PRICE_CHANGE_1H": 1.0,
        "STRATEGY_早期启动_MAX_PRICE_CHANGE_24H": 15.0,
        "STRATEGY_早期启动_MIN_VOL_RATIO": 1.3,
        # ── 单币 AI 判单 ────────────────────────────────────────────────────
        "SINGLE_COIN_JUDGE_ENABLED": False,
        "SINGLE_COIN_JUDGE_CAN_VETO": False,
        "SINGLE_COIN_JUDGE_CAN_FORCE_TRADE": False,
        "SINGLE_COIN_JUDGE_CAN_CHANGE_SIZE": False,
        "SINGLE_COIN_JUDGE_CAN_CHANGE_TP_SL": False,
        "SINGLE_COIN_JUDGE_MAX_PER_SCAN": 1,
        "SINGLE_COIN_JUDGE_MAX_PER_HOUR": 10,
        "SINGLE_COIN_JUDGE_MAX_PER_DAY": 50,
        "SINGLE_COIN_JUDGE_SYMBOL_COOLDOWN_SEC": 900,
        "SINGLE_COIN_JUDGE_CACHE_TTL_SEC": 900,
        "SINGLE_COIN_JUDGE_MIN_SCORE": 75,
        "SINGLE_COIN_JUDGE_MIN_STRATEGY_CONFIDENCE": 0.6,
        "SINGLE_COIN_JUDGE_ALLOWED_TAGS": ["启动型", "OI爆发", "静默建仓", "突破前夜", "早期启动"],
        "SINGLE_COIN_JUDGE_TIMEOUT_SEC": 20,
        "SINGLE_COIN_JUDGE_DEFAULT_ON_ERROR": "WAIT",
        "SINGLE_COIN_JUDGE_PROMPT": "",
        "STRATEGY_早期启动_MIN_OI_CHANGE_15M": 0.5,
        "YAOBI_AI_MAX_INPUT_TOKENS": 8000,
        "YAOBI_AI_MAX_OUTPUT_TOKENS": 1200,
        "OKX_MIN_REQUEST_INTERVAL": 0.20,
        "SURF_MIN_REQUEST_INTERVAL": 0.20,
        # ── 多周期 Gate ──────────────────────────────────────────────────────
                "ORPHAN_SCAN_ENABLED": True,
        "ORPHAN_SCAN_INTERVAL_SEC": 60,
        "ORPHAN_AUTO_ATTACH": True,
        "ORPHAN_AUTO_PROTECT": True,
        "ORPHAN_AUTO_CLOSE_IF_PROTECT_FAIL": False,
                # ── 自动进化引擎 ──────────────────────────────────────────────────────
        "EVOLVER_ENABLED": True,
        "EVOLVER_AUTO_APPLY": True,
        "EVOLVER_INTERVAL_MINUTES": 60,
        "EVOLVER_RUN_AFTER_CLOSED_TRADES": 10,
        "EVOLVER_MIN_TOTAL_TRADES": 15,
        "EVOLVER_MIN_TRADES_PER_STRATEGY": 8,
        "EVOLVER_MAX_PARAM_CHANGE_PCT": 0.30,
        "EVOLVER_MAX_UPDATES_PER_RUN": 5,
        "EVOLVER_BACKUP_ENABLED": True,
        "EVOLVER_ROLLBACK_ENABLED": True,
        "EVOLVER_LOCK_TRADE_SIZE": True,
        "EVOLVER_LOCK_MAX_POSITIONS": True,
        "EVOLVER_ALLOW_STRATEGY_PARAM_CHANGE": True,
        "EVOLVER_ALLOW_RISK_PARAM_CHANGE": True,
        "EVOLVER_ALLOW_SL_TP_CHANGE": True,
        "EVOLVER_ALLOW_GATE_PARAM_CHANGE": True,
        "EVOLVER_ALLOW_STRATEGY_WEIGHT_CHANGE": True,
        "EVOLVER_ALLOW_DISABLE_STRATEGY": True,
        "EVOLVER_ALLOW_ENABLE_STRATEGY": True,
        "EVOLVER_DISABLE_STRATEGY_MIN_TRADES": 10,
        "EVOLVER_DISABLE_STRATEGY_WIN_RATE_BELOW": 0.30,
        "EVOLVER_DISABLE_STRATEGY_EXPECTANCY_BELOW": 0.0,
        "EVOLVER_DOWN_WEIGHT_WIN_RATE_BELOW": 0.45,
        "EVOLVER_DOWN_WEIGHT_EXPECTANCY_BELOW": 0.0,
        "EVOLVER_UP_WEIGHT_WIN_RATE_ABOVE": 0.58,
        "EVOLVER_UP_WEIGHT_EXPECTANCY_ABOVE": 0.0,
        "EVOLVER_POLICY_VERSION_PREFIX": "auto",
                "EVOLVER_EVAL_MIN_TRADES": 10,
        "EVOLVER_EVAL_MAX_MINUTES": 240,
        "EVOLVER_AUTO_ROLLBACK_ENABLED": True,
        "EVOLVER_ROLLBACK_IF_EXPECTANCY_BELOW": 0.0,
        "EVOLVER_ROLLBACK_IF_PNL_BELOW": 0.0,
        "EVOLVER_ROLLBACK_IF_WIN_RATE_DROP_PCT": 0.15,
        "EVOLVER_ROLLBACK_IF_MAX_DRAWDOWN_ABOVE": 50.0,
        "EVOLVER_ROLLBACK_COOLDOWN_MINUTES": 60,
        "EVOLVER_MIN_TRADES_BETWEEN_UPDATES": 30,
        "EVOLVER_MIN_MINUTES_BETWEEN_UPDATES": 60,
        "EVOLVER_MAX_ROLLBACKS_PER_DAY": 3,
        "EVOLVER_FREEZE_AFTER_ROLLBACK_MINUTES": 120,
                "EVOLVER_PATCH_MIN_TRADES": 20,
        "EVOLVER_PATCH_HARMFUL_COOLDOWN_MINUTES": 240,
        "EVOLVER_PATCH_ALLOW_STRENGTHEN": True,
        "EVOLVER_PATCH_MAX_CONSECUTIVE_HARMFUL": 2,
                "EVOLVER_COUNTERFACTUAL_VALIDATION_ENABLED": True,
        "EVOLVER_COUNTERFACTUAL_MIN_AFFECTED": 5,
        "EVOLVER_COUNTERFACTUAL_ACCEPT_MIN_PNL_DELTA": 0.0,
        "EVOLVER_COUNTERFACTUAL_ACCEPT_MIN_CONFIDENCE": 0.55,
        "EVOLVER_COUNTERFACTUAL_ALLOW_WEAK_ACCEPT": True,
        "EVOLVER_COUNTERFACTUAL_MAX_MISSING_FIELDS_RATIO": 0.5,
                # ── Evolver Runtime ────────────────────────────────────────────────────
        "EVOLVER_RUNTIME_ENABLED": True,
        "EVOLVER_LOCK_TIMEOUT_SEC": 900,
        "EVOLVER_MAX_RUNTIME_SEC": 600,
        "EVOLVER_FREEZE_ON_ERROR_COUNT": 3,
        "EVOLVER_ERROR_FREEZE_MINUTES": 120,
        "EVOLVER_DATA_MIN_FIELD_COVERAGE": 0.70,
        "EVOLVER_DATA_MIN_PNL_COVERAGE": 0.95,
        "EVOLVER_DATA_MIN_STRATEGY_TAG_COVERAGE": 0.95,
        "EVOLVER_DATA_MIN_POLICY_VERSION_COVERAGE": 0.90,
                "EVOLVER_LOG_RETENTION_DAYS": 30,
                # ── Autopilot Guard ─────────────────────────────────────────────────────
        "AUTOPILOT_GUARD_ENABLED": True,
        "AUTOPILOT_GUARD_INTERVAL_SEC": 300,
        "AUTOPILOT_FREEZE_EVOLVER_ON_GUARD_FAIL": True,
        "AUTOPILOT_REQUIRE_E2E_PASS": True,
        "AUTOPILOT_E2E_MAX_AGE_HOURS": 24,
        "AUTOPILOT_LOCKED_PARAM_AUTO_RESTORE": True,
        "EVOLVER_MAX_HISTORY_LINES": 10000,
        "EVOLVER_MAX_BACKUPS": 50,
        # 策略权重（0.0 = 禁用，1.0 = 正常，>1.0 = 增强）
        "STRATEGY_WEIGHT_启动型": 1.0,
        "STRATEGY_WEIGHT_OI爆发": 1.0,
        "STRATEGY_WEIGHT_静默建仓": 1.0,
        "STRATEGY_WEIGHT_突破前夜": 1.0,
        "STRATEGY_WEIGHT_早期启动": 1.0,
        # 策略启用开关
        "STRATEGY_ENABLED_启动型": True,
        "STRATEGY_ENABLED_OI爆发": True,
        "STRATEGY_ENABLED_静默建仓": True,
        "STRATEGY_ENABLED_突破前夜": True,
        "STRATEGY_ENABLED_早期启动": True,
                # ── Shadow Tracker ─────────────────────────────────────────────────────
        "SHADOW_TRACKER_ENABLED": True,
        "SHADOW_MAX_HOLD_MINUTES": 240,
        "SHADOW_UPDATE_INTERVAL_SEC": 30,
        "SHADOW_MIN_PRICE_MOVE_TO_UPDATE": 0.0,
                "SHADOW_CLOSE_ON_TP1": True,
        "SHADOW_CLOSE_ON_TP2": True,
        "SHADOW_CLOSE_ON_TP3": True,
        "MULTI_TF_GATE_ENABLED": False,
        "MULTI_TF_GATE_CAN_VETO": False,
        "MULTI_TF_GATE_DEFAULT_ON_ERROR": "WAIT",
        "MULTI_TF_4H_REQUIRED": True,
        "MULTI_TF_4H_ALLOW_NEUTRAL": True,
        "MULTI_TF_4H_BLOCK_AGAINST": True,
        "MULTI_TF_1H_BLOCK_EQUILIBRIUM": True,
        "MULTI_TF_1H_BLOCK_AGAINST": True,
        "MULTI_TF_15M_BLOCK_OVERHEATED": True,
        "MULTI_TF_NEAR_LEVEL_PCT": 0.5,
        "MULTI_TF_MIN_ROOM_TO_RESISTANCE_PCT_LONG": 0.8,
        "MULTI_TF_MIN_ROOM_TO_SUPPORT_PCT_SHORT": 0.8,
        "MULTI_TF_15M_MAX_CHASE_PCT": 3.0,
        "MULTI_TF_15M_MAX_ATR_DISTANCE": 1.8,
        "MULTI_TF_SUPPORT_RESISTANCE_LOOKBACK_1H": 80,
        "MULTI_TF_SUPPORT_RESISTANCE_LOOKBACK_4H": 80,
        "MULTI_TF_MIN_CANDLES_15M": 50,
        "MULTI_TF_MIN_CANDLES_1H": 50,
        "MULTI_TF_MIN_CANDLES_4H": 50,
    }

    _BOUNDS: dict[str, tuple] = {
        # 超短线 V3.0
        "SCALP_MAX_POSITIONS":         (1,      20),
        "SCALP_POSITION_USDT":         (1,      1_000_000),
        "SCALP_LEVERAGE":              (1,      125),
        "SCALP_STOP_LOSS_PCT":         (0.1,    50.0),
        "SCALP_TP1_RATIO":             (0.1,    0.9),
        "SCALP_TP2_RATIO":             (0.1,    0.9),
        "SCALP_TP3_TRAIL_PCT":         (0.1,    10.0),
        "SCALP_CANDIDATE_LIMIT":       (20,     500),
        "SCALP_RISK_PER_TRADE_USDT":   (1,      1_000_000),
        "SCALP_MAX_DAILY_LOSS_USDT":   (1,      1_000_000),
        "SCALP_MAX_DAILY_LOSS_R":      (1,      100),
        "SCALP_TP1_RR":                (0.5,    10.0),
        "SCALP_TP2_RR":                (1.0,    20.0),
        "SCALP_TIME_STOP_MINUTES":     (1,      360),
        "SCALP_TP2_TIMEOUT_MINUTES":   (5,      720),
        "SCALP_STRUCTURE_TRAIL_BARS":  (3,      30),
        "SCALP_NET_BREAKEVEN_LOCK_PCT": (0.0,   2.0),
        "SCALP_TP1_SOFT_BREAKEVEN_PCT": (0.0,   2.0),
        "SCALP_REVERSAL_STOP_SL_FRACTION": (0.1, 1.0),
        "SCALP_WS_STALE_SECONDS":     (15,     600),
        "SCALP_POSITION_CHECK_INTERVAL_SECONDS": (3, 120),
        "SCALP_POSITION_STALE_SECONDS": (15,    600),
        "SCALP_MAX_CANDIDATE_AGE_MINUTES": (5, 1440),
        "SCALP_TP_CONFIRM_TICKS":      (1,      5),
        "SCALP_TREND_LATE_SIZE_MULT":  (0.1,    1.0),
        "SCALP_SYMBOL_BAN_WINDOW_MINUTES": (10, 1440),
        "SCALP_SYMBOL_BAN_SL_COUNT":   (1,      10),
        "SCALP_SYMBOL_BAN_LOSS_R":     (0.5,    20.0),
        "SCALP_SYMBOL_BAN_DURATION_MINUTES": (0, 1440),
        "FEE_RATE_PER_SIDE":           (0.0,    0.01),
        "SLIPPAGE_RATE_PER_SIDE":      (0.0,    0.05),
        # V3.0 轧空猎杀 & 动能突破
        "SQUEEZE_OI_DROP_MAJOR":       (0.1,    5.0),
        "SQUEEZE_OI_DROP_MID":         (0.3,    10.0),
        "SQUEEZE_OI_DROP_MEME":        (0.3,    15.0),
        "SQUEEZE_WICK_PCT":            (0.3,    5.0),
        "SQUEEZE_TAKER_MIN":           (0.5,    0.9),
        "SQUEEZE_TAKER_MIN_LONG":      (0.5,    0.9),
        "SQUEEZE_TAKER_MIN_SHORT":     (0.5,    0.9),
        "BTC_GUARD_REJECT_PCT":        (0.3,    10.0),
        "BTC_GUARD_WARN_PCT":          (0.1,    5.0),
        "BREAKOUT_TAKER_MIN":          (0.4,    0.9),
        "BREAKOUT_MIN_PCT":            (0.01,   5.0),
        "BREAKOUT_ATR_MULT":           (0.0,    5.0),
        "BREAKOUT_ATR_MIN_PCT":        (0.0,    5.0),
        "BREAKOUT_ATR_MAX_PCT":        (0.0,    10.0),
        "BREAKOUT_MIN_VOL_RATIO":      (0.01,   5.0),
        "MARKET_VOL_BTC_ATR_BASELINE": (0.05,   1.0),
        "MARKET_VOL_SCALE_MIN":        (0.1,    2.0),
        "MARKET_VOL_SCALE_MAX":        (1.0,    10.0),
        "SCALP_YAOBI_FUNDING_OI_SOFT_MULT": (0.1, 1.0),
        "SCALP_SQUEEZE_OI_STABILIZE_LOOKBACK_SEC": (10, 600),
        "SCALP_SQUEEZE_OI_REBOUND_PCT": (0.0, 5.0),
        "BREAKOUT_MAX_PREMOVE_5M_PCT":  (0.0,   10.0),
        "BREAKOUT_MAX_PREMOVE_15M_PCT": (0.0,   20.0),
        "BREAKOUT_MAX_PREMOVE_30M_PCT": (0.0,   20.0),
        "BREAKOUT_MAX_EMA20_DEVIATION_PCT": (0.0, 10.0),
        "CONTINUATION_TAKER_MIN":       (0.4,    0.9),
        "CONTINUATION_TAKER_MIN_LONG":  (0.4,    0.9),
        "CONTINUATION_TAKER_MIN_SHORT": (0.4,    0.9),
        "CONTINUATION_HOT_TAKER_MIN":   (0.4,    0.9),
        "CONTINUATION_HOT_TAKER_MIN_LONG": (0.4, 0.9),
        "CONTINUATION_HOT_TAKER_MIN_SHORT": (0.4, 0.9),
        "CONTINUATION_MIN_PULLBACK_PCT": (0.0,   3.0),
        "CONTINUATION_RECLAIM_LOOKBACK": (1,     8),
        "CONTINUATION_ATR_MIN_PCT":     (0.0,    5.0),
        "CONTINUATION_ATR_MIN_PCT_LONG": (0.0,   5.0),
        "CONTINUATION_ATR_MIN_PCT_SHORT": (0.0,  5.0),
        "CONTINUATION_ATR_MAX_PCT":     (0.0,    10.0),
        "CONTINUATION_ATR_MAX_PCT_LONG": (0.0,   10.0),
        "CONTINUATION_ATR_MAX_PCT_SHORT": (0.0,  10.0),
        "CONTINUATION_MAX_EMA20_DEVIATION_PCT": (0.0, 15.0),
        "CONTINUATION_MAX_EMA20_DEVIATION_PCT_LONG": (0.0, 15.0),
        "CONTINUATION_MAX_EMA20_DEVIATION_PCT_SHORT": (0.0, 15.0),
        "SIGNAL_COOLDOWN_SECONDS":     (1,      60),
        "OI_POLL_INTERVAL":            (5,      60),
        "SCALP_OI_PREFETCH_TOP_N":     (0,      120),
        "BTC_GUARD_PCT":               (0.1,    10.0),
        "SCALP_SURF_NEWS_INTERVAL_MINUTES": (5, 1440),
        "SCALP_SURF_NEWS_TOP_N":       (1,      50),
        "SCALP_SURF_ENTRY_AI_MIN_ABS_CHANGE": (0.0, 500.0),
        "SCALP_YAOBI_CONTEXT_TOP_N":   (0,      120),
        "SCALP_YAOBI_MIN_SCORE":       (0,      100),
        "SCALP_YAOBI_MIN_ANOMALY_SCORE": (0,    100),
        "SCALP_YAOBI_FUNDING_EXTREME_PCT": (0.0, 1.0),
        "SCALP_YAOBI_OI_GUARD_MIN_24H_PCT": (0.0, 500.0),
        # 妖币扫描器
        "YAOBI_SCAN_INTERVAL":         (1,      1440),
        "YAOBI_MIN_SCORE":             (0,      100),
        "YAOBI_MIN_ANOMALY_SCORE":     (0,      100),
        "YAOBI_SURF_TOP_N":            (1,      20),
        "YAOBI_SURF_NEWS_TOP_N":       (1,      100),
        "YAOBI_SURF_FALLBACK_SEARCH_LIMIT": (0, 20),
        "YAOBI_SURF_DIRECTION_MIN_CONFIDENCE": (0, 100),
        "YAOBI_SQUARE_ROWS":           (1,      200),
        "YAOBI_SQUARE_BROWSER_SECONDS": (5,     300),
        "YAOBI_SQUARE_SCROLL_PAUSE_SECONDS": (0.5, 10.0),
        "YAOBI_SQUARE_SCROLL_RESET_EVERY": (5,  200),
        "YAOBI_OKX_HOT_LIMIT":         (1,      100),
        "YAOBI_OKX_HEAVY_TOP_N":       (1,      120),
        "YAOBI_OKX_PRICE_BATCH_SIZE":  (1,      100),
        "YAOBI_OKX_SEARCH_CACHE_MINUTES": (1,   1440),
        "YAOBI_FUTURES_TOP_N":         (20,     300),
        "YAOBI_OPPORTUNITY_TOP_N":      (1,      30),
        "YAOBI_OPPORTUNITY_MIN_SCORE":  (0,      100),
        "YAOBI_AI_MAX_SYMBOLS_PER_RUN": (1,      30),
        "YAOBI_AI_TOP_OUTPUT":          (1,      20),
        "YAOBI_AI_FAILURE_FALLBACK_MIN_SCORE": (0, 100),
        "YAOBI_AI_MIN_INTERVAL_MINUTES": (1,     1440),
        "YAOBI_AI_CACHE_TTL_MINUTES":   (1,      1440),
        "YAOBI_PLAYBOOK_TTL_MINUTES":   (5,      240),
        "YAOBI_AI_DAILY_USD_CAP":       (0.0,    100.0),
        "YAOBI_AI_MAX_INPUT_TOKENS":    (1000,   50000),
        "YAOBI_AI_MAX_OUTPUT_TOKENS":   (200,    8000),
        "OKX_MIN_REQUEST_INTERVAL":    (0.02,   5.0),
        "SURF_MIN_REQUEST_INTERVAL":   (0.02,   5.0),
    }

    def __init__(self):
        self.default_settings: dict = {
            "CONFIG_PROFILE_VERSION":   self.PROFILE_VERSION,
            # ── 策略参数由 PROFILE_MIGRATION_DEFAULTS 统一管理 ──────────────────
            **self.PROFILE_MIGRATION_DEFAULTS,
            # ── 以下为 UI 开关 / 连接设置 / 非策略默认值（不参与 migration） ──
            "SCALP_ENABLED":             False,
            "SCALP_AUTO_TRADE":          False,
            "SCALP_ENABLE_LONG":         True,
            "SCALP_ENABLE_SHORT":        True,
            "SCALP_POSITION_USDT":       100.0,
            "SCALP_LEVERAGE":            10,
            "SCALP_STOP_LOSS_PCT":       50.0,
            "SCALP_WATCHLIST":           "",
            "SCALP_PAPER_TRADE":         False,
            "MANUAL_REAL_TRADE_ENABLED": False,
            "SCALP_USE_DYNAMIC_SL":      True,
            "SCALP_RISK_PER_TRADE_USDT": 20.0,
            "SCALP_WS_DIRECT":           False,
            "SCALP_WS_COMBINED_STREAM":  True,
            # ── 妖币扫描 UI 开关 ────────────────────────────────────────────────
            "YAOBI_ENABLED":             False,
            "YAOBI_SCAN_INTERVAL":       15,
            "YAOBI_MIN_SCORE":           30,
            "YAOBI_MIN_ANOMALY_SCORE":   35,
            "YAOBI_CHAINS":              "eth,bsc,solana,base,arbitrum",
            "OBSIDIAN_VAULT_PATH":       r"C:\BOT\yaobi",
            "COINGLASS_API_KEY":         "",
            "YAOBI_SURF_ENABLED":        True,
            "YAOBI_SQUARE_ENABLED":      True,
            "YAOBI_SQUARE_ROWS":         50,
            "YAOBI_SQUARE_BROWSER_ENABLED": True,
            "YAOBI_SQUARE_BROWSER_HEADLESS": True,
            "YAOBI_SQUARE_BROWSER_SECONDS": 45,
            "YAOBI_SQUARE_SCROLL_PAUSE_SECONDS": 2,
            "YAOBI_SQUARE_SCROLL_RESET_EVERY": 20,
            "YAOBI_OKX_ENABLED":         True,
            "YAOBI_OKX_HOT_ENABLED":     True,
        }
        self.settings: dict = self.load()

    def _coerce_profile_version(self, value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=4, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)

    def _apply_profile_migration(self, merged: dict, loaded: dict) -> bool:
        stored_version = self._coerce_profile_version(loaded.get("CONFIG_PROFILE_VERSION", 0))
        if stored_version >= self.PROFILE_VERSION:
            return False

        changed = False
        for key, value in self.PROFILE_MIGRATION_DEFAULTS.items():
            if key in self.default_settings and merged.get(key) != value:
                merged[key] = value
                changed = True
        if merged.get("CONFIG_PROFILE_VERSION") != self.PROFILE_VERSION:
            merged["CONFIG_PROFILE_VERSION"] = self.PROFILE_VERSION
            changed = True
        return changed

    def load(self) -> dict:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = self.default_settings.copy()
                merged.update(data)
                migrated = self._apply_profile_migration(merged, data)
                self.settings = merged
                if migrated:
                    self._persist()
                    logging.getLogger(__name__).info(
                        "配置已迁移到默认参数版本 %s", self.PROFILE_VERSION
                    )
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
        self.settings["CONFIG_PROFILE_VERSION"] = self.PROFILE_VERSION
        self._persist()


config_manager = ConfigManager()
