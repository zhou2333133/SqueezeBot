import asyncio
import collections
import csv
import gzip
import hmac
import io
import json
import logging
import os
import queue as std_queue
import time
from datetime import datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

import aiohttp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import bot_state
from config import (
    config_manager, DATA_DIR, MASKED_SECRET, PANEL_LOCAL_ONLY, PANEL_TOKEN,
    SENSITIVE_SETTING_KEYS,
)
from log_manager import log_queue, scalp_log_queue
from scalp_diagnostics import apply_trade_diagnosis, build_learning_report
import signals as _signals_mod
from signals import (
    scalp_signal_queue, scalp_signals_history, scalp_positions, scalp_trade_history,
)
from scanner.candidates import (
    candidates_queue, get_sorted_candidates, get_anomaly_candidates,
    clear_candidates, scan_status, get_opportunity_queue, opportunity_update_queue,
    candidates_map,
)
from scanner.ai_gateway import provider_status as ai_provider_status
from scanner.knowledge_base import knowledge_status
from scanner.provider_metrics import provider_metrics_snapshot
from trader import BinanceTrader
from watchlist import (
    list_watch_items, upsert_watch_item, remove_watch_item,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="SqueezeBot Control Panel",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
PANEL_TOKEN_HEADER = "X-SqueezeBot-Token"
_AUTH_PUBLIC_PATHS = {"/api/auth/status", "/api/auth/check"}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_MODEL_OPTIONS = {
    "openai": [
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "o4-mini",
        "gpt-4o",
        "gpt-4o-mini",
    ],
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
        "gemini-3-flash-preview",
        "gemini-3.1-pro-preview",
    ],
    "anthropic": [
        "claude-opus-4-7",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        "claude-3-5-haiku-latest",
    ],
    "deepseek": [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-chat",
        "deepseek-reasoner",
    ],
    "surf": [
        "surf-ask",
        "surf-research",
    ],
}


def _redact_config(settings: dict) -> dict:
    safe = dict(settings)
    for key in SENSITIVE_SETTING_KEYS:
        if safe.get(key):
            safe[key] = MASKED_SECRET
    return safe


def _client_is_local(host: str | None) -> bool:
    if not PANEL_LOCAL_ONLY:
        return True
    if not host:
        return False
    if host in ("localhost",):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _host_without_port(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlsplit(raw)
        return (parsed.hostname or "").lower()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end].lower() if end >= 0 else raw.lower()
    return raw.split(":", 1)[0].lower()


def _host_header_allowed(request: Request) -> bool:
    host = _host_without_port(request.headers.get("host", ""))
    return _client_is_local(host)


def _same_origin_request(request: Request) -> bool:
    host = _host_without_port(request.headers.get("host", ""))
    if not host:
        return False
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        other = _host_without_port(value)
        return bool(other and hmac.compare_digest(other, host))
    # No browser origin headers: allow only when the anti-CSRF token is supplied
    # in a custom header (cross-site forms cannot attach this header).
    return bool(request.headers.get(PANEL_TOKEN_HEADER))


def _valid_panel_token(token: str | None, *, require_configured: bool = False) -> bool:
    if not PANEL_TOKEN:
        return not require_configured
    return bool(token) and hmac.compare_digest(str(token), PANEL_TOKEN)


def _auth_meta() -> dict:
    return {
        "token_required": bool(PANEL_TOKEN),
        "write_token_required": True,
        "token_configured": bool(PANEL_TOKEN),
        "local_only": PANEL_LOCAL_ONLY,
    }


def _token_from_request(request: Request, *, allow_query: bool = True) -> str:
    token = request.headers.get(PANEL_TOKEN_HEADER, "")
    if token or not allow_query:
        return token
    return request.query_params.get("token", "")


@app.middleware("http")
async def panel_security(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in _AUTH_PUBLIC_PATHS:
        host = request.client.host if request.client else ""
        if not _client_is_local(host) or not _host_header_allowed(request):
            return JSONResponse({"error": "local_only"}, status_code=403)
        unsafe = request.method.upper() in _UNSAFE_METHODS
        if unsafe and not _same_origin_request(request):
            return JSONResponse({"error": "csrf_check_failed"}, status_code=403)
        if unsafe and not PANEL_TOKEN:
            return JSONResponse(
                {"error": "panel_token_required", "message": "PANEL_TOKEN must be configured before write operations"},
                status_code=503,
            )
        if not _valid_panel_token(
            _token_from_request(request, allow_query=not unsafe),
            require_configured=unsafe,
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ─── 页面 ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "config": _redact_config(config_manager.settings),
            "auth": _auth_meta(),
            "model_options": _MODEL_OPTIONS,
        },
    )


# ─── 面板认证 API ─────────────────────────────────────────────────────────────

@app.get("/api/auth/status")
async def auth_status():
    return JSONResponse(_auth_meta())


@app.post("/api/auth/check")
async def auth_check(request: Request):
    host = request.client.host if request.client else ""
    if not _client_is_local(host) or not _host_header_allowed(request):
        return JSONResponse({"status": "error", "message": "local_only"}, status_code=403)
    if not PANEL_TOKEN:
        return JSONResponse({"status": "error", "message": "panel_token_required"}, status_code=503)
    if not _same_origin_request(request):
        return JSONResponse({"status": "error", "message": "csrf_check_failed"}, status_code=403)
    if not _valid_panel_token(_token_from_request(request, allow_query=False), require_configured=True):
        return JSONResponse({"status": "error", "message": "unauthorized"}, status_code=401)
    return JSONResponse({"status": "success"})


# ─── 全局配置 API ─────────────────────────────────────────────────────────────

@app.post("/api/config")
async def update_config(request: Request):
    try:
        form_data = await request.form()
        config_manager.save(dict(form_data))
        logger.info("配置已更新")
        return JSONResponse({"status": "success", "message": "✅ 参数已保存并立即生效！"})
    except Exception as e:
        logger.error("配置保存失败: %s", e, exc_info=True)
        return JSONResponse({"status": "error", "message": f"❌ 保存失败: {e}"}, status_code=500)


@app.get("/api/config")
async def get_config():
    return JSONResponse(_redact_config(config_manager.settings))


# ─── 人工关注池 API ───────────────────────────────────────────────────────────

async def _payload_from_request(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    form = await request.form()
    return dict(form)


@app.get("/api/watchlist")
async def get_watchlist():
    candidates = get_sorted_candidates(min_score=0)
    return JSONResponse({
        "items": list_watch_items(candidates),
        "total": len(list_watch_items()),
    })


@app.post("/api/watchlist")
async def save_watchlist_item(request: Request):
    try:
        payload = await _payload_from_request(request)
        symbol = str(payload.get("symbol", "")).strip()
        ban_value = payload.get("ban_trade", None)
        ban_trade = None if ban_value is None else str(ban_value).lower() in ("true", "1", "on")
        item = upsert_watch_item(
            symbol,
            status=str(payload.get("status", "观察")).strip() or "观察",
            reason=str(payload.get("reason", "")).strip(),
            manual_note=str(payload.get("manual_note", "")).strip(),
            ban_trade=ban_trade,
            watch_until=str(payload.get("watch_until", "")).strip(),
            source=str(payload.get("source", "manual")).strip() or "manual",
        )
        return JSONResponse({"status": "success", "item": item, "message": "✅ 已更新关注池"})
    except ValueError as e:
        return JSONResponse({"status": "error", "message": f"❌ {e}"}, status_code=400)
    except Exception as e:
        logger.error("关注池保存失败: %s", e, exc_info=True)
        return JSONResponse({"status": "error", "message": f"❌ 保存失败: {e}"}, status_code=500)


@app.delete("/api/watchlist/{symbol}")
async def delete_watchlist_item(symbol: str):
    removed = remove_watch_item(symbol)
    return JSONResponse({
        "status": "success" if removed else "not_found",
        "message": "✅ 已移除关注" if removed else "未找到该币种",
    })


# ─── 手动下单 ─────────────────────────────────────────────────────────────────

@app.post("/api/trade")
async def manual_trade(request: Request):
    if not config_manager.settings.get("MANUAL_REAL_TRADE_ENABLED", False):
        return JSONResponse({
            "status": "error",
            "message": "❌ 手动实盘下单默认禁用：实盘请优先使用超短线策略自动开仓。",
        }, status_code=403)

    try:
        form = await request.form()
        symbol   = str(form.get("symbol", "")).strip().upper()
        side     = str(form.get("side",   "")).strip().upper()
        quantity = float(form.get("quantity", 0))
        stop_price = float(form.get("stop_price", 0))
    except Exception:
        return JSONResponse({"status": "error", "message": "❌ 参数解析失败"}, status_code=400)

    if side not in ("BUY", "SELL"):
        return JSONResponse({"status": "error", "message": "❌ side 只能是 BUY 或 SELL"}, status_code=400)
    if quantity <= 0:
        return JSONResponse({"status": "error", "message": "❌ 数量必须大于 0"}, status_code=400)
    if stop_price <= 0:
        return JSONResponse({"status": "error", "message": "❌ 手动实盘下单必须提供 stop_price，禁止无保护单裸仓"}, status_code=400)

    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            trader   = BinanceTrader(session)
            leverage = config_manager.settings.get("SCALP_LEVERAGE", 10)
            await trader.set_leverage(symbol, leverage)
            res = await trader.place_market_order(symbol, side, quantity)
            if not res:
                return JSONResponse({"status": "error", "message": "❌ 下单失败，请检查 API Key 权限和余额"}, status_code=400)
            filled_qty = float(res.get("executedQty", 0) or 0)
            if filled_qty <= 0:
                filled_qty = quantity
            exit_side = "SELL" if side == "BUY" else "BUY"
            sl_resp = await trader.place_stop_loss_order(symbol, exit_side, stop_price)
            if not (sl_resp and sl_resp.get("orderId")):
                emergency = await trader.place_reduce_only_market_order(symbol, exit_side, filled_qty)
                if emergency:
                    return JSONResponse({
                        "status": "error",
                        "message": "❌ 手动单止损挂单失败，已 reduceOnly 市价撤出，未保留裸仓。",
                    }, status_code=500)
                return JSONResponse({
                    "status": "error",
                    "message": "❌ 手动单止损挂单失败且紧急撤出失败，请立即人工检查交易所持仓。",
                }, status_code=500)
        if res:
            return JSONResponse({
                "status":  "success",
                "message": f"✅ {side} {symbol} ×{filled_qty} 成功，止损已挂 @ {stop_price} (OrderID: {res.get('orderId', 'N/A')})",
            })
    except Exception as e:
        logger.error("手动下单异常: %s", e, exc_info=True)
        return JSONResponse({"status": "error", "message": f"❌ 下单异常: {e}"}, status_code=500)


# ─── 账户余额 ─────────────────────────────────────────────────────────────────

@app.get("/api/account/balance")
async def get_account_balance():
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            bal = await BinanceTrader(session).get_balance()
        return JSONResponse(bal or {})
    except Exception as e:
        logger.error("查询余额异常: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── CSV 数据 API ─────────────────────────────────────────────────────────────

@app.get("/api/data/symbols")
async def list_symbols():
    try:
        files = [f[:-4] for f in os.listdir(DATA_DIR) if f.endswith(".csv")]
        return JSONResponse({"symbols": sorted(files)})
    except Exception:
        return JSONResponse({"symbols": []})


@app.get("/api/data/{symbol}")
async def get_symbol_data(symbol: str, limit: int = 30):
    path = os.path.join(DATA_DIR, f"{symbol.upper()}.csv")
    if not os.path.exists(path):
        return JSONResponse({"error": "数据文件不存在"}, status_code=404)
    try:
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
        return JSONResponse({"symbol": symbol.upper(), "rows": rows[-limit:][::-1]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── 超短线控制 API ───────────────────────────────────────────────────────────

@app.get("/api/scalp/status")
async def scalp_status():
    is_running = bool(bot_state.scalp_task and not bot_state.scalp_task.done())
    sym_count  = 0
    if bot_state.scalp_bot and bot_state.scalp_bot.monitored_symbols:
        sym_count = len(bot_state.scalp_bot.monitored_symbols)
    live_suspended = bool(getattr(bot_state.scalp_bot, "_live_trading_suspended", False))
    live_suspended_reason = getattr(bot_state.scalp_bot, "_live_trading_suspended_reason", "")
    health = bot_state.scalp_bot.runtime_health_snapshot() if bot_state.scalp_bot else {}
    return JSONResponse({
        "running":         is_running,
        "positions_count": len(scalp_positions),
        "signals_count":   len(scalp_signals_history),
        "symbols_count":   sym_count,
        "live_trading_suspended": live_suspended,
        "live_trading_suspended_reason": live_suspended_reason,
        "ws_last_message_at": health.get("ws_last_message_at"),
        "ws_last_message_time": health.get("ws_last_message_time"),
        "ws_stale_seconds": health.get("ws_stale_seconds"),
        "ws_stale_symbol_count": health.get("ws_stale_symbol_count"),
    })


@app.post("/api/scalp/start")
async def scalp_start():
    if bot_state.scalp_task and not bot_state.scalp_task.done():
        return JSONResponse({"status": "already_running", "message": "⚡ 已在运行中"})
    from bot_scalp import BinanceScalpBot
    bot_state.scalp_bot  = BinanceScalpBot()
    bot_state.scalp_task = asyncio.create_task(bot_state.scalp_bot.run())
    logger.info("⚡ 超短线机器人通过 Web 面板启动")
    return JSONResponse({"status": "started", "message": "⚡ 超短线机器人已启动"})


@app.post("/api/scalp/stop")
async def scalp_stop():
    real_positions = [
        p for p in getattr(bot_state.scalp_bot, "open_positions", {}).values()
        if not getattr(p, "paper", True)
    ]
    real_positions += [p for p in scalp_positions.values() if isinstance(p, dict) and not p.get("paper")]
    if real_positions:
        return JSONResponse({
            "status": "error",
            "message": "❌ 仍有真实仓位，停止前请先手动平仓或确认交易所已无仓位",
            "real_positions": len(real_positions),
        }, status_code=409)
    if bot_state.scalp_bot:
        bot_state.scalp_bot.running = False
    if bot_state.scalp_task and not bot_state.scalp_task.done():
        bot_state.scalp_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(bot_state.scalp_task), timeout=3)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    bot_state.scalp_task = None
    bot_state.scalp_bot  = None
    for sym, pos in list(scalp_positions.items()):
        if isinstance(pos, dict) and pos.get("paper"):
            _signals_mod.set_scalp_position(sym, None)
    logger.info("⚡ 超短线机器人通过 Web 面板停止")
    return JSONResponse({"status": "stopped", "message": "⚡ 超短线机器人已停止"})


@app.post("/api/scalp/refresh")
async def scalp_refresh_symbols():
    if bot_state.scalp_bot and bot_state.scalp_bot.running:
        await bot_state.scalp_bot.refresh_symbols()
        return JSONResponse({
            "status":  "success",
            "message": f"✅ 已刷新，监控 {len(bot_state.scalp_bot.monitored_symbols)} 个币种",
        })
    return JSONResponse({"status": "error", "message": "❌ 机器人未运行"}, status_code=400)


@app.get("/api/scalp/positions")
async def get_scalp_positions():
    return JSONResponse({"positions": dict(scalp_positions)})


@app.post("/api/scalp/positions/{symbol}/close")
async def manual_close_scalp_position(symbol: str, request: Request):
    if not bot_state.scalp_bot:
        return JSONResponse({"status": "error", "message": "❌ 超短线机器人未运行，无法安全执行手动平仓"}, status_code=400)
    payload = await _payload_from_request(request)
    try:
        percent = float(payload.get("percent", payload.get("pct", 0)) or 0)
    except (TypeError, ValueError):
        percent = 0
    result = await bot_state.scalp_bot.manual_close_position(symbol, percent)
    status = 200 if result.get("status") == "success" else 400
    return JSONResponse(result, status_code=status)


@app.get("/api/scalp/signals")
async def get_scalp_signals(limit: int = 50):
    return JSONResponse({"signals": scalp_signals_history[-limit:][::-1], "total": len(scalp_signals_history)})


@app.delete("/api/scalp/signals")
async def clear_scalp_signals():
    scalp_signals_history.clear()
    logger.info("超短线信号历史已清空")
    return JSONResponse({"status": "success", "message": "✅ 超短线信号已清空"})


@app.get("/api/scalp/trades")
async def get_scalp_trades(limit: int = 200):
    trades = scalp_trade_history[-limit:][::-1]
    wins  = sum(1 for t in scalp_trade_history if t.get("pnl_usdt", 0) > 0)
    total = len(scalp_trade_history)
    return JSONResponse({
        "trades": trades,
        "total": total,
        "total_pnl": round(sum(t.get("pnl_usdt", 0) for t in scalp_trade_history), 4),
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
    })


@app.get("/api/scalp/trades/persistent")
async def get_scalp_trades_persistent():
    """从 strategy_trades.jsonl 读取永久成交记录（重启不丢失）。"""
    try:
        import os
        from config import DATA_DIR
        from persistence import read_jsonl
        from datetime import datetime
        trades_file = os.path.join(DATA_DIR, "strategy_trades.jsonl")
        if not os.path.exists(trades_file):
            return JSONResponse({"trades":[], "total":0, "total_pnl":0,
                                 "today_trades":0, "today_pnl":0, "by_date":{}})
        all_trades = read_jsonl(trades_file)
        today = datetime.now().strftime("%Y-%m-%d")
        total_pnl = sum(float(t.get("pnl_usdt", 0) or 0) for t in all_trades)
        today_trades = [t for t in all_trades if (t.get("exit_time") or t.get("entry_time") or "").startswith(today)]
        today_pnl = sum(float(t.get("pnl_usdt", 0) or 0) for t in today_trades)
        today_wins = sum(1 for t in today_trades if float(t.get("pnl_usdt", 0) or 0) >= 0)
        by_date = {}
        for t in all_trades:
            date = (t.get("exit_time") or t.get("entry_time") or "")[:10]
            if not date:
                continue
            if date not in by_date:
                by_date[date] = {"trades":0, "wins":0, "pnl":0.0}
            pnl = float(t.get("pnl_usdt", 0) or 0)
            by_date[date]["trades"] += 1
            if pnl >= 0:
                by_date[date]["wins"] += 1
            by_date[date]["pnl"] = round(by_date[date].get("pnl", 0) + pnl, 2)
        return JSONResponse({
            "trades": all_trades[-200:][::-1],
            "total": len(all_trades),
            "total_pnl": round(total_pnl, 2),
            "today_trades": len(today_trades),
            "today_pnl": round(today_pnl, 2),
            "today_wins": today_wins,
            "by_date": dict(sorted(by_date.items(), reverse=True)),
        })
    except Exception as e:
        logger.error("persistent trades error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/scalp/equity")
async def get_scalp_equity():
    return JSONResponse(_scalp_equity_summary(list(scalp_trade_history)))


@app.delete("/api/scalp/trades")
async def clear_scalp_trades():
    scalp_trade_history.clear()
    return JSONResponse({"status": "success", "message": "✅ 历史成交已清空"})


@app.post("/api/scalp/clear-display")
async def clear_scalp_display():
    """仅清空前端显示数据，不影响 AI 学习记忆和 strategy_trades.jsonl。"""
    scalp_trade_history.clear()
    scalp_signals_history.clear()
    _drain_queue(scalp_signal_queue)
    return JSONResponse({"status": "success",
                         "message": "✅ 前端显示已清空（AI 学习数据不受影响）",
                         "detail": "strategy_trades.jsonl / learning_memory.json / rule_selector_events.jsonl 均保留"})


def _drain_queue(q: std_queue.Queue) -> int:
    drained = 0
    while True:
        try:
            q.get_nowait()
            drained += 1
        except std_queue.Empty:
            return drained


def _empty_scalp_filter_stats() -> dict:
    cfg = config_manager.settings
    return {
        "checked": 0,
        "no_candidate": 0,
        "oi_miss": 0,
        "btc_guard": 0,
        "cooldown": 0,
        "symbol_banned": 0,
        "manual_block": 0,
        "yaobi_block": 0,
        "premove_block": 0,
        "vol_miss": 0,
        "state_block": 0,
        "atr_block": 0,
        "squeeze": 0,
        "breakout": 0,
        "continuation": 0,
        "passed": 0,
        "cfg_sq_oi_major": cfg.get("SQUEEZE_OI_DROP_MAJOR", 0.5),
        "cfg_sq_oi_mid": cfg.get("SQUEEZE_OI_DROP_MID", 1.0),
        "cfg_sq_oi_meme": cfg.get("SQUEEZE_OI_DROP_MEME", 1.5),
        "cfg_sq_taker": cfg.get("SQUEEZE_TAKER_MIN", 0.65),
        "cfg_bo_taker": cfg.get("BREAKOUT_TAKER_MIN", 0.62),
        "cfg_bo_min_pct": cfg.get("BREAKOUT_MIN_PCT", 0.15),
        "cfg_bo_atr_mult": cfg.get("BREAKOUT_ATR_MULT", 0.7),
        "cfg_bo_atr_min": cfg.get("BREAKOUT_ATR_MIN_PCT", 0.50),
        "cfg_bo_atr_max": cfg.get("BREAKOUT_ATR_MAX_PCT", 1.20),
        "cfg_bo_max_premove_30m": cfg.get("BREAKOUT_MAX_PREMOVE_30M_PCT", 3.0),
        "updated_at": time.time(),
    }


def _reset_scalp_candidate_paths(bot) -> int:
    if not bot:
        return 0
    reset = 0
    now_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_ts = time.monotonic()
    for meta in getattr(bot, "candidate_meta", {}).values():
        price = 0.0
        for key in ("scalp_candidate_last_price", "last_price", "yaobi_price_usd"):
            try:
                price = float(meta.get(key) or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price > 0:
                break
        if price <= 0:
            for key in (
                "scalp_candidate_seen_time",
                "scalp_candidate_seen_ts",
                "scalp_candidate_seen_price",
                "scalp_candidate_last_price",
                "scalp_candidate_max_up_pct",
                "scalp_candidate_max_down_pct",
                "scalp_candidate_elapsed_min",
            ):
                meta.pop(key, None)
            continue
        meta["scalp_candidate_seen_time"] = now_label
        meta["scalp_candidate_seen_ts"] = now_ts
        meta["scalp_candidate_seen_price"] = price
        meta["scalp_candidate_last_price"] = price
        meta["scalp_candidate_max_up_pct"] = 0.0
        meta["scalp_candidate_max_down_pct"] = 0.0
        meta["scalp_candidate_elapsed_min"] = 0.0
        reset += 1
    return reset


@app.delete("/api/scalp/review-data")
async def reset_scalp_review_data():
    trade_count = len(scalp_trade_history)
    signal_count = len(scalp_signals_history)
    scalp_trade_history.clear()
    scalp_signals_history.clear()
    drained_signals = _drain_queue(scalp_signal_queue)
    drained_logs = _drain_queue(log_queue)
    drained_scalp_logs = _drain_queue(scalp_log_queue)

    bot = bot_state.scalp_bot
    post_exit_count = 0
    paper_cleared = 0
    real_symbols: set[str] = set()
    if bot:
        post_exit_count = sum(len(v) for v in getattr(bot, "_post_exit_watch", {}).values())
        getattr(bot, "_post_exit_watch", {}).clear()
        for k in getattr(bot, "_fstat", {}):
            bot._fstat[k] = 0
        bot._fstat_ts = 0.0

        for symbol, pos in list(getattr(bot, "open_positions", {}).items()):
            if getattr(pos, "paper", False):
                bot.open_positions.pop(symbol, None)
                _signals_mod.set_scalp_position(symbol, None)
                paper_cleared += 1
            else:
                real_symbols.add(symbol)

    for symbol, pos in list(scalp_positions.items()):
        if pos.get("paper"):
            _signals_mod.set_scalp_position(symbol, None)
            paper_cleared += 1
        else:
            real_symbols.add(symbol)

    candidate_paths = _reset_scalp_candidate_paths(bot)
    _signals_mod.scalp_filter_stats = _empty_scalp_filter_stats()

    logger.info(
        "超短线复盘数据已重置: trades=%d signals=%d paper=%d post_exit=%d candidate_paths=%d real_left=%d",
        trade_count, signal_count, paper_cleared, post_exit_count, candidate_paths, len(real_symbols),
    )
    return JSONResponse({
        "status": "success",
        "message": "✅ 复盘数据已清空，新下载的分析包将从现在重新累计",
        "cleared": {
            "trades": trade_count,
            "signals": signal_count,
            "queued_signals": drained_signals,
            "logs": drained_logs,
            "scalp_logs": drained_scalp_logs,
            "paper_positions": paper_cleared,
            "post_exit_watches": post_exit_count,
            "candidate_paths": candidate_paths,
        },
        "real_positions_left": len(real_symbols),
    })


@app.get("/api/scalp/trades/csv")
async def export_scalp_trades_csv():
    trades = list(scalp_trade_history)
    if not trades:
        return JSONResponse({"message": "暂无成交记录"}, status_code=404)
    output = io.StringIO()
    fieldnames = []
    for trade in trades:
        for key in trade.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trades)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=scalp_trades.csv"},
    )


@app.get("/api/scalp/report")
async def get_scalp_report():
    trades = [apply_trade_diagnosis(t) for t in list(scalp_trade_history)]
    cfg    = config_manager.settings
    if not trades:
        return JSONResponse({"error": "暂无成交记录"}, status_code=404)

    total = len(trades)
    wins  = sum(1 for t in trades if t.get("pnl_usdt", 0) > 0)
    total_pnl = sum(t.get("pnl_usdt", 0) for t in trades)
    total_gross_pnl = sum(t.get("gross_pnl_usdt", t.get("pnl_usdt", 0)) for t in trades)
    total_fee = sum(t.get("fee_usdt", 0) for t in trades)
    total_slippage = sum(t.get("slippage_usdt", 0) for t in trades)
    win_pnls  = [t["pnl_usdt"] for t in trades if t.get("pnl_usdt", 0) > 0]
    loss_pnls = [t["pnl_usdt"] for t in trades if t.get("pnl_usdt", 0) <= 0]

    def trade_signal_label(t: dict) -> str:
        return t.get("signal_label") or t.get("signal") or "?"

    by_reason = dict(collections.Counter(t.get("close_reason", "?") for t in trades))
    by_label  = dict(collections.Counter(trade_signal_label(t) for t in trades).most_common(10))
    by_state  = dict(collections.Counter(t.get("market_state", "?") for t in trades).most_common(10))
    by_diag   = dict(collections.Counter(t.get("trade_diagnosis", "?") for t in trades).most_common(10))
    by_symbol = collections.Counter()
    sym_pnl: dict[str, float] = {}
    for t in trades:
        s = t.get("symbol", "?")
        by_symbol[s] += 1
        sym_pnl[s] = sym_pnl.get(s, 0) + t.get("pnl_usdt", 0)

    top5  = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)[:5]
    bot5  = sorted(sym_pnl.items(), key=lambda x: x[1])[:5]
    best  = max(trades, key=lambda t: t.get("pnl_usdt", 0))
    worst = min(trades, key=lambda t: t.get("pnl_usdt", 0))

    report = {
        "策略参数快照": {
            "杠杆":             cfg.get("SCALP_LEVERAGE"),
            "最大SL%保证金":    cfg.get("SCALP_STOP_LOSS_PCT"),
            "结构止损":         cfg.get("SCALP_USE_DYNAMIC_SL"),
            "每笔风险USDT":     cfg.get("SCALP_RISK_PER_TRADE_USDT"),
            "每日熔断USDT":     cfg.get("SCALP_MAX_DAILY_LOSS_USDT"),
            "每日熔断R":        cfg.get("SCALP_MAX_DAILY_LOSS_R"),
            "手续费率单边":      cfg.get("FEE_RATE_PER_SIDE"),
            "滑点率单边":        cfg.get("SLIPPAGE_RATE_PER_SIDE"),
            "TP1_RR":           cfg.get("SCALP_TP1_RR"),
            "TP2_RR":           cfg.get("SCALP_TP2_RR"),
            "TP1平仓比例":      cfg.get("SCALP_TP1_RATIO"),
            "TP2平仓比例":      cfg.get("SCALP_TP2_RATIO"),
            "TP3追踪%":         cfg.get("SCALP_TP3_TRAIL_PCT"),
            "TP3激进Runner":     cfg.get("SCALP_TP3_AGGRESSIVE_RUNNER"),
            "结构追踪K数":       cfg.get("SCALP_STRUCTURE_TRAIL_BARS"),
            "TP后净保本锁利%":   cfg.get("SCALP_NET_BREAKEVEN_LOCK_PCT"),
            "TP1软保本呼吸%":    cfg.get("SCALP_TP1_SOFT_BREAKEVEN_PCT"),
            "趋势反转止损比例":   cfg.get("SCALP_REVERSAL_STOP_SL_FRACTION"),
            "TP1时间止损分钟":   cfg.get("SCALP_TIME_STOP_MINUTES"),
            "TP2超时分钟":       cfg.get("SCALP_TP2_TIMEOUT_MINUTES"),
            "候选池上限":       cfg.get("SCALP_CANDIDATE_LIMIT"),
            "OI轮询秒":         cfg.get("OI_POLL_INTERVAL"),
            "信号冷却秒":       cfg.get("SIGNAL_COOLDOWN_SECONDS"),
            "BTC守卫%":         cfg.get("BTC_GUARD_PCT"),
            "轧空OI阈值-大币%": cfg.get("SQUEEZE_OI_DROP_MAJOR"),
            "轧空OI阈值-中币%": cfg.get("SQUEEZE_OI_DROP_MID"),
            "轧空OI阈值-Meme%": cfg.get("SQUEEZE_OI_DROP_MEME"),
            "挤压确认反弹%":    cfg.get("SQUEEZE_WICK_PCT"),
            "挤压Taker阈值":    cfg.get("SQUEEZE_TAKER_MIN"),
            "突破Taker阈值":    cfg.get("BREAKOUT_TAKER_MIN"),
            "突破最小幅度%":     cfg.get("BREAKOUT_MIN_PCT"),
            "突破ATR倍数":       cfg.get("BREAKOUT_ATR_MULT"),
            "突破ATR最小%":      cfg.get("BREAKOUT_ATR_MIN_PCT"),
            "突破ATR最大%":      cfg.get("BREAKOUT_ATR_MAX_PCT"),
            "当前K量比阈值":     cfg.get("BREAKOUT_MIN_VOL_RATIO"),
            "突破30m追涨过滤%":   cfg.get("BREAKOUT_MAX_PREMOVE_30M_PCT"),
            "Surf后台新闻":      cfg.get("SCALP_SURF_NEWS_ENABLED"),
            "Surf新闻间隔分钟":   cfg.get("SCALP_SURF_NEWS_INTERVAL_MINUTES"),
            "Surf新闻TopN":      cfg.get("SCALP_SURF_NEWS_TOP_N"),
            "Surf入场AI":       cfg.get("SCALP_SURF_ENTRY_AI_ENABLED"),
            "Surf入场AI涨跌阈值%": cfg.get("SCALP_SURF_ENTRY_AI_MIN_ABS_CHANGE"),
            "接入妖币扫描上下文":  cfg.get("SCALP_USE_YAOBI_CONTEXT"),
            "妖币共享TopN":       cfg.get("SCALP_YAOBI_CONTEXT_TOP_N"),
            "妖币共享最低分":      cfg.get("SCALP_YAOBI_MIN_SCORE"),
            "妖币共享异动阈值":    cfg.get("SCALP_YAOBI_MIN_ANOMALY_SCORE"),
            "妖币禁入拦截":       cfg.get("SCALP_YAOBI_BLOCK_DECISION_BAN"),
            "妖币等待确认拦截":    cfg.get("SCALP_YAOBI_BLOCK_WAIT_CONFIRM"),
            "妖币高风险拦截":      cfg.get("SCALP_YAOBI_BLOCK_HIGH_RISK"),
            "妖币OI费率护栏":      cfg.get("SCALP_YAOBI_FUNDING_OI_GUARD"),
            "妖币极端费率%":       cfg.get("SCALP_YAOBI_FUNDING_EXTREME_PCT"),
            "妖币OI护栏24h%":      cfg.get("SCALP_YAOBI_OI_GUARD_MIN_24H_PCT"),
        },
        "整体统计": {
            "总成交笔数":  total,
            "盈利笔数":    wins,
            "亏损笔数":    total - wins,
            "胜率%":       round(wins / total * 100, 1),
            "总盈亏USDT":  round(total_pnl, 4),
            "总毛盈亏USDT": round(total_gross_pnl, 4),
            "总手续费USDT": round(total_fee, 4),
            "总滑点USDT":  round(total_slippage, 4),
            "平均盈利USDT": round(sum(win_pnls) / len(win_pnls), 4) if win_pnls else 0,
            "平均亏损USDT": round(sum(loss_pnls) / len(loss_pnls), 4) if loss_pnls else 0,
            "盈亏比":      round(
                (sum(win_pnls) / len(win_pnls)) / abs(sum(loss_pnls) / len(loss_pnls)), 2
            ) if win_pnls and loss_pnls else "N/A",
        },
        "最佳单笔": {
            "品种": best.get("symbol"), "方向": best.get("direction"),
            "信号": trade_signal_label(best), "盈亏USDT": best.get("pnl_usdt"),
            "时间": best.get("entry_time"),
        },
        "最差单笔": {
            "品种": worst.get("symbol"), "方向": worst.get("direction"),
            "信号": trade_signal_label(worst), "盈亏USDT": worst.get("pnl_usdt"),
            "时间": worst.get("entry_time"),
        },
        "平仓原因分布":   by_reason,
        "信号类型分布":   by_label,
        "市场状态分布":   by_state,
        "交易诊断分布":   by_diag,
        "盈利最多币种TOP5":  dict(top5),
        "亏损最多币种TOP5":  dict(bot5),
        "过滤统计快照":  _signals_mod.scalp_filter_stats or "（5分钟后刷新）",
    }
    return JSONResponse(report)


async def _scalp_report_or_none() -> dict | None:
    if not scalp_trade_history:
        return None
    response = await get_scalp_report()
    if response.status_code != 200:
        return None
    return json.loads(response.body.decode("utf-8"))


def _group_trade_stats(trades: list[dict], key: str) -> dict:
    rows: dict[str, dict] = {}
    for t in trades:
        if key == "signal_label":
            name = str(t.get("signal_label") or t.get("signal") or "?")
        else:
            name = str(t.get(key) or "?")
        pnl = float(t.get("pnl_usdt", 0) or 0)
        row = rows.setdefault(name, {
            "count": 0,
            "wins": 0,
            "pnl_usdt": 0.0,
            "avg_mfe_pct": 0.0,
            "avg_mae_pct": 0.0,
            "_mfe_sum": 0.0,
            "_mae_sum": 0.0,
        })
        row["count"] += 1
        row["wins"] += 1 if pnl > 0 else 0
        row["pnl_usdt"] += pnl
        row["_mfe_sum"] += float(t.get("mfe_pct", 0) or 0)
        row["_mae_sum"] += float(t.get("mae_pct", 0) or 0)
    for row in rows.values():
        count = row["count"] or 1
        row["win_rate_pct"] = round(row["wins"] / count * 100, 1)
        row["pnl_usdt"] = round(row["pnl_usdt"], 4)
        row["avg_mfe_pct"] = round(row.pop("_mfe_sum") / count, 4)
        row["avg_mae_pct"] = round(row.pop("_mae_sum") / count, 4)
    return dict(sorted(rows.items(), key=lambda x: x[1]["pnl_usdt"]))


def _scalp_equity_summary(trades: list[dict]) -> dict:
    ordered = sorted(
        trades,
        key=lambda x: str(x.get("exit_time") or x.get("entry_time") or ""),
    )
    cfg = config_manager.settings
    risk_unit = float(cfg.get("SCALP_RISK_PER_TRADE_USDT", 20.0) or 20.0)
    cumulative = 0.0
    high = 0.0
    max_dd = 0.0
    loss_streak = 0
    max_loss_streak = 0
    points = []
    tp3_count = 0
    sl_count = 0
    total_fee = 0.0
    total_slippage = 0.0
    gross_profit = 0.0
    gross_loss = 0.0

    for idx, t in enumerate(ordered, 1):
        pnl = float(t.get("pnl_usdt", 0) or 0)
        gross = float(t.get("gross_pnl_usdt", pnl) or 0)
        cumulative += pnl
        high = max(high, cumulative)
        max_dd = max(max_dd, high - cumulative)
        if pnl <= 0:
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            loss_streak = 0
        if t.get("close_reason") == "TP3":
            tp3_count += 1
        if t.get("close_reason") == "SL":
            sl_count += 1
        total_fee += float(t.get("fee_usdt", 0) or 0)
        total_slippage += float(t.get("slippage_usdt", 0) or 0)
        if gross > 0:
            gross_profit += gross
        else:
            gross_loss += gross
        points.append({
            "n": idx,
            "time": t.get("exit_time") or t.get("entry_time") or "",
            "pnl": round(pnl, 4),
            "equity": round(cumulative, 4),
            "symbol": t.get("symbol", ""),
            "reason": t.get("close_reason", ""),
        })

    total_cost = total_fee + total_slippage
    return {
        "trades": len(ordered),
        "realized_pnl_usdt": round(cumulative, 4),
        "max_drawdown_usdt": round(max_dd, 4),
        "max_drawdown_r": round(max_dd / risk_unit, 2) if risk_unit > 0 else 0,
        "current_loss_streak": loss_streak,
        "max_loss_streak": max_loss_streak,
        "tp3_count": tp3_count,
        "sl_count": sl_count,
        "total_fee_usdt": round(total_fee, 4),
        "total_slippage_usdt": round(total_slippage, 4),
        "total_cost_usdt": round(total_cost, 4),
        "cost_to_gross_profit_pct": round(total_cost / gross_profit * 100, 1) if gross_profit > 0 else 0,
        "gross_profit_usdt": round(gross_profit, 4),
        "gross_loss_usdt": round(gross_loss, 4),
        "points": points[-200:],
    }


def _analysis_markdown(pack: dict) -> str:
    report = pack.get("策略报告") or {}
    stats = report.get("整体统计", {}) if isinstance(report, dict) else {}
    lines = [
        "# SqueezeBot 复盘包",
        "",
        f"- 生成时间: {pack.get('generated_at')}",
        f"- 成交笔数: {stats.get('总成交笔数', len(pack.get('成交明细', [])))}",
        f"- 总盈亏: {stats.get('总盈亏USDT', 0)} USDT",
        f"- 胜率: {stats.get('胜率%', 0)}%",
        "",
        "## 结论入口",
        "",
        "- 先看 `策略报告.整体统计`、`成交分组统计`、`过滤统计快照`。",
        "- 再看每笔 `entry_context`、`mfe_pct/mae_pct`、`post_exit_*` 判断方向、止损和卖飞。",
        "- 数据源使用情况看 `数据源诊断.provider_metrics`。",
        "",
        "## 成交分组统计",
        "",
    ]
    grouped = pack.get("成交分组统计", {})
    for group_name, rows in grouped.items():
        lines += [f"### {group_name}", ""]
        if not rows:
            lines.append("- 无")
            lines.append("")
            continue
        for name, row in rows.items():
            lines.append(
                f"- {name}: {row.get('count')}笔, 胜率 {row.get('win_rate_pct')}%, "
                f"PnL {row.get('pnl_usdt')}U, MFE {row.get('avg_mfe_pct')}%, MAE {row.get('avg_mae_pct')}%"
            )
        lines.append("")
    learning = pack.get("学习报告", {})
    if learning:
        lines += [
            "## 学习报告",
            "",
            f"- 样本状态: {learning.get('样本状态')}",
            f"- 诊断分布: {learning.get('诊断分布', {})}",
            "",
            "### 亏损归因",
            "",
        ]
        loss_attr = learning.get("亏损归因", {})
        if loss_attr:
            for name, row in loss_attr.items():
                lines.append(
                    f"- {name}: {row.get('count')}笔, PnL {row.get('pnl_usdt')}U, "
                    f"亏损笔数占比 {row.get('pct_of_losing_trades')}%, 亏损金额占比 {row.get('pct_of_loss_usdt')}%"
                )
        else:
            lines.append("- 无亏损样本")
        lines += ["", "### 下一轮参数建议", ""]
        for item in learning.get("下一轮参数建议", []):
            lines.append(
                f"- {item.get('topic')}: {item.get('condition')}；{item.get('suggestion')}"
            )
        lines.append("")
    equity = pack.get("净值曲线摘要", {})
    if equity:
        lines += [
            "## 净值与成本",
            "",
            f"- 最大回撤: {equity.get('max_drawdown_usdt', 0)}U / {equity.get('max_drawdown_r', 0)}R",
            f"- 总成本: {equity.get('total_cost_usdt', 0)}U，成本/毛盈利 {equity.get('cost_to_gross_profit_pct', 0)}%",
            f"- 最大连亏: {equity.get('max_loss_streak', 0)}，TP3 {equity.get('tp3_count', 0)} 次，SL {equity.get('sl_count', 0)} 次",
            "",
        ]
    watch_items = pack.get("关注池", [])
    if watch_items:
        lines += ["## 关注池", ""]
        for item in watch_items[:20]:
            lines.append(
                f"- {item.get('symbol')} {item.get('status')} | "
                f"OI={item.get('latest_oi_grade') or '-'} | "
                f"异动={item.get('latest_anomaly_score', 0)} | "
                f"决策={item.get('latest_decision_action') or '-'} | "
                f"{item.get('reason') or item.get('manual_note') or ''}"
            )
        lines.append("")
    lines += [
        "## 最近成交",
        "",
    ]
    for t in pack.get("成交明细", [])[-20:]:
        lines.append(
            f"- {t.get('exit_time', '')} {t.get('symbol')} {t.get('direction')} "
            f"{t.get('signal_label') or t.get('signal')} {t.get('close_reason')} "
            f"Diag={t.get('trade_diagnosis', 'NA')} PnL={t.get('pnl_usdt')}U "
            f"MFE={t.get('mfe_pct')}% MAE={t.get('mae_pct')}% "
            f"Post30={t.get('post_exit_30m_favorable_pct', 'NA')}%"
        )
    return "\n".join(lines) + "\n"


# ── 复盘包瘦身工具 ───────────────────────────────────────────────────────────
# slim 模式下保留的 entry_context 字段白名单（命中决策最相关的几类）
_SLIM_ENTRY_CONTEXT_KEYS = {
    "symbol", "direction", "signal_label", "market_state", "trigger_pct",
    "candidate_rank", "direction_bias",
    "change_24h", "vol_surge", "funding_rate", "fr_squeeze",
    "scalp_candidate_seen_time", "scalp_candidate_elapsed_min",
    "scalp_candidate_max_up_pct", "scalp_candidate_max_down_pct",
    "pre_entry_favorable_from_candidate_pct", "pre_entry_adverse_from_candidate_pct",
    "pre_entry_directional_30m_pct",
    "yaobi_score", "yaobi_anomaly_score", "yaobi_category",
    "yaobi_decision_action", "yaobi_decision_confidence",
    "yaobi_decision_reasons", "yaobi_decision_risks",
    "yaobi_oi_trend_grade", "yaobi_oi_change_24h_pct", "yaobi_oi_change_3d_pct",
    "yaobi_funding_rate_pct", "yaobi_volume_ratio", "yaobi_taker_buy_ratio_5m",
    "yaobi_opportunity_action", "yaobi_opportunity_permission",
    "yaobi_opportunity_confidence", "yaobi_opportunity_setup_state",
    "watchlist_status", "watchlist_reason",
    "oi_change_3m_pct", "current_taker_ratio", "atr_pct",
    "pre_entry_3m_pct", "pre_entry_5m_pct", "pre_entry_15m_pct",
    "ema20_deviation_pct", "directional_ema20_deviation_pct",
    "breakout_after_pullback", "taker_trend_5m",
    "ret20m_pct", "ret60m_pct",
}

_SLIM_CANDIDATE_KEYS = {
    "symbol", "score", "score_raw", "anomaly_score", "category",
    "decision_action", "decision_confidence", "decision_tier", "decision_subtype",
    "oi_trend_grade", "oi_change_24h_pct",
    "funding_rate_pct", "volume_ratio", "opportunity_action",
    "opportunity_permission", "opportunity_setup_state", "price_change_24h",
}


def _slim_entry_context(ctx: dict | None) -> dict | None:
    if not isinstance(ctx, dict):
        return ctx
    return {k: v for k, v in ctx.items() if k in _SLIM_ENTRY_CONTEXT_KEYS}


def _slim_trade(trade: dict) -> dict:
    t = dict(trade)
    if "entry_context" in t:
        t["entry_context"] = _slim_entry_context(t["entry_context"])
    if "entry_1m_profile" in t and isinstance(t["entry_1m_profile"], dict):
        t["entry_1m_profile"] = {
            k: v for k, v in t["entry_1m_profile"].items()
            if k in {"pre_entry_3m_pct", "pre_entry_5m_pct", "pre_entry_15m_pct",
                     "ema20_deviation_pct", "directional_ema20_deviation_pct",
                     "breakout_after_pullback", "taker_trend_5m",
                     "current_volume_ratio"}
        }
    return t


def _slim_candidate(c: dict) -> dict:
    return {k: c.get(k) for k in _SLIM_CANDIDATE_KEYS if c.get(k) is not None}


# ══════════════════════════════════════════════════════════════════════════════
# 复盘包辅助构建函数（新架构数据）
# ══════════════════════════════════════════════════════════════════════════════

def _build_rule_selector_section(filter_fn, days_ts: float = 0.0) -> dict:
    """第 3 层：规则选择器 + learning_memory。"""
    import os
    from config import DATA_DIR
    from persistence import read_jsonl

    section = {}

    # learning_memory 摘要
    try:
        from learning_memory import get_memory_summary, get_state_keys
        section["记忆摘要"] = get_memory_summary()
        section["已知市场状态"] = get_state_keys()[:20]
    except Exception as e:
        section["记忆摘要"] = {"error": str(e)}

    # rule_selector 事件
    events_file = os.path.join(DATA_DIR, "rule_selector_events.jsonl")
    if os.path.exists(events_file):
        events = read_jsonl(events_file)
        filtered = filter_fn(events, "ts")
        section["选择器事件"] = {
            "总计": len(events),
            "本日": len(filtered),
            "本日_block": sum(1 for e in filtered if e.get("decision") == "block"),
            "本日_warn": sum(1 for e in filtered if e.get("decision") == "warn"),
            "最近": [{k: v for k, v in e.items() if k != "ts"} for e in filtered[-20:]],
        }

    return section


def _build_evolver_section() -> dict:
    """第 4 层：Evolver 状态 + 历史。"""
    section = {}
    try:
        from risk_guard import get_evolver_status, get_recent_evolver_history
        section["状态"] = get_evolver_status()
        section["历史"] = get_recent_evolver_history(limit=20)
    except Exception as e:
        section["状态"] = {"error": str(e)}
    return section


def _build_guard_section(filter_fn, days_ts: float = 0.0) -> dict:
    """第 5 层：风控拦截事件。"""
    import os
    from config import DATA_DIR
    from persistence import read_jsonl
    section = {}
    events_file = os.path.join(DATA_DIR, "guard_events.jsonl")
    if os.path.exists(events_file):
        events = read_jsonl(events_file)
        filtered = filter_fn(events, "ts")
        section["总计"] = len(events)
        section["本日"] = len(filtered)
        if filtered:
            section["本日_WARNING"] = sum(1 for e in filtered if e.get("severity") == "WARNING")
            section["本日_ERROR"] = sum(1 for e in filtered if e.get("severity") == "ERROR")
            section["最近"] = [{"event": e.get("event_type"), "severity": e.get("severity"),
                                "reason": str(e.get("reason", ""))[:120]}
                               for e in filtered[-20:]]
    return section


def _build_param_attribution_section() -> dict:
    """第 4 层：参数补丁因果回溯。"""
    import os
    from config import DATA_DIR
    from persistence import read_jsonl
    section = {}
    patches_file = os.path.join(DATA_DIR, "param_patches.jsonl")
    if os.path.exists(patches_file):
        patches = read_jsonl(patches_file)
        active = [p for p in patches if p.get("status") == "ACTIVE"]
        section["总计"] = len(patches)
        section["活跃补丁"] = len(active)
        section["最近"] = [{"patch_id": p.get("param_patch_id", ""),
                            "key": p.get("key", ""),
                            "old": p.get("old"), "new": p.get("new"),
                            "verdict": p.get("_verdict", "pending"),
                            "status": p.get("status", "")}
                           for p in patches[-10:]]
    return section


def _build_boundary_section() -> dict:
    """用户边界快照。"""
    import json, os
    section = {}
    bc_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "boundary_config.json")
    if os.path.exists(bc_file):
        try:
            with open(bc_file, "r", encoding="utf-8") as f:
                section["boundary_config"] = json.load(f)
        except Exception as e:
            section["boundary_config"] = {"error": str(e)}
    # locked params
    try:
        from risk_guard import get_locked_params_status
        section["锁定参数"] = get_locked_params_status()
    except Exception as e:
        section["锁定参数"] = {"error": str(e)}
    return section


def _build_ai_report_section() -> dict:
    """AI 日报最新。"""
    try:
        from ai_reporter import get_latest_report
        report = get_latest_report()
        return {"最新日报": report} if report else {"最新日报": None}
    except Exception as e:
        return {"最新日报": {"error": str(e)}}


async def _build_scalp_analysis_pack(detail: str = "slim", date: str = "") -> dict:
    """构建 AI 复盘包。detail=slim|full, date=YYYY-MM-DD(指定日期)|空=全部。"""
    detail = (detail or "slim").lower()
    full_mode = detail == "full"
    _day_start = 0.0
    _day_end = 0.0
    if date:
        try:
            d = datetime.strptime(date.strip(), "%Y-%m-%d")
            _day_start = d.timestamp()
            _day_end = _day_start + 86400
        except (ValueError, TypeError):
            pass

    def _in_day_range(ts: float) -> bool:
        return True if _day_end == 0.0 else _day_start <= ts < _day_end

    def _parse_ts(s: str) -> float:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            return 0.0

    def _filter_trades(trades: list) -> list:
        if _day_end == 0.0:
            return trades
        return [t for t in trades if _in_day_range(_parse_ts(t.get("exit_time", t.get("entry_time", ""))))]

    def _filter_jsonl(rows: list, ts_key: str = "ts") -> list:
        if _day_end == 0.0:
            return rows
        return [r for r in rows if _in_day_range(float(r.get(ts_key, 0) or 0))]

    raw_trades_all = [apply_trade_diagnosis(t) for t in list(scalp_trade_history)]
    raw_trades = _filter_trades(raw_trades_all)
    trades = raw_trades if full_mode else [_slim_trade(t) for t in raw_trades]
    bot = bot_state.scalp_bot
    learning_report = build_learning_report(raw_trades)
    runtime = {
        "scalp_running": bool(bot_state.scalp_task and not bot_state.scalp_task.done()),
        "yaobi_running": bool(bot_state.yaobi_task and not bot_state.yaobi_task.done()),
        "open_positions": len(scalp_positions),
        "scalp_signals": len(scalp_signals_history),
        "scalp_trades": len(raw_trades),
        "detail_mode": detail,
    }
    if bot:
        runtime.update(bot.runtime_health_snapshot())
    provider_diag = {
        "provider_metrics": provider_metrics_snapshot(),
        "scan_sources": scan_status.get("sources", {}),
        "ai_status": scan_status.get("ai_status") or ai_provider_status(),
    }
    if full_mode:
        provider_diag["knowledge"] = knowledge_status()
        try:
            from config import surf_credentials_status, okx_credentials_status
            from scanner.sources import binance_square
            provider_diag["credentials"] = {
                "surf": surf_credentials_status(),
                "okx": okx_credentials_status(),
                "binance_square": binance_square.auth_status(),
            }
        except Exception as e:
            provider_diag["credentials_error"] = str(e)

    candidate_limit = 100 if full_mode else 20
    signal_sample_limit = 120 if full_mode else 40
    block_log_limit = 120 if full_mode else 30
    wait_diag_limit = 80 if full_mode else 20

    candidate_snapshot = get_sorted_candidates()[:candidate_limit]
    anomaly_snapshot = get_anomaly_candidates(
        min_anomaly=int(config_manager.settings.get("YAOBI_MIN_ANOMALY_SCORE", 35)),
        limit=candidate_limit,
    )

    pack = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "用途": "AI复盘包：默认 slim（精简快照、保留交易历史）；?detail=full 拿全量。",
        "运行状态": runtime,
        "策略参数": _redact_config(config_manager.settings) if full_mode else None,
        "策略报告": await _scalp_report_or_none(),
        "净值曲线摘要": _scalp_equity_summary(raw_trades),
        "成交分组统计": {
            "by_signal": _group_trade_stats(raw_trades, "signal_label"),
            "by_direction": _group_trade_stats(raw_trades, "direction"),
            "by_market_state": _group_trade_stats(raw_trades, "market_state"),
            "by_close_reason": _group_trade_stats(raw_trades, "close_reason"),
            "by_diagnosis": _group_trade_stats(raw_trades, "trade_diagnosis"),
            "by_symbol": _group_trade_stats(raw_trades, "symbol"),
        },
        "学习报告": learning_report,
        "成交明细": trades,
        "信号样本": scalp_signals_history[-signal_sample_limit:],
        "当前持仓": dict(scalp_positions),
        "过滤统计快照": _signals_mod.scalp_filter_stats,
        "未开仓诊断": {
            "最近拦截时间线": bot.entry_block_log_snapshot(limit=block_log_limit) if bot else [],
            "候选等待原因": bot.candidate_wait_diagnostics(limit=wait_diag_limit) if bot else [],
        },
        "候选池快照": {
            "symbols": list(getattr(bot, "candidate_symbols", []) or []),
            "meta": dict(getattr(bot, "candidate_meta", {}) or {}) if full_mode else None,
        },
        "关注池": list_watch_items(candidate_snapshot)[: 50 if full_mode else 20],
        "妖币扫描": {
            "status": dict(scan_status),
            "opportunity_queue": get_opportunity_queue(limit=30 if full_mode else 15),
            "top_candidates": (
                candidate_snapshot if full_mode
                else [_slim_candidate(c) for c in candidate_snapshot]
            ),
            "anomaly_candidates": (
                anomaly_snapshot if full_mode
                else [_slim_candidate(c) for c in anomaly_snapshot]
            ),
            "decision_cards": [
                {
                    "symbol": c.get("symbol"),
                    "action": c.get("decision_action"),
                    "confidence": c.get("decision_confidence"),
                    "reasons": c.get("decision_reasons", []),
                    "risks": c.get("decision_risks", []),
                    "missing": c.get("decision_missing", []) if full_mode else [],
                    "note": c.get("decision_note", "") if full_mode else "",
                    "oi_trend_grade": c.get("oi_trend_grade", ""),
                    "anomaly_score": c.get("anomaly_score", 0),
                    "score": c.get("score", 0),
                }
                for c in candidate_snapshot[: 50 if full_mode else 15]
            ],
        },
        "数据源诊断": provider_diag,

        # ── 新架构数据（第 3 层规则学习器 + 第 4 层 Evolver + 第 5 层风控）────
        "规则学习器": _build_rule_selector_section(_filter_jsonl, days_ts=_day_start),
        "进化模块": _build_evolver_section(),
        "风控拦截": _build_guard_section(_filter_jsonl, days_ts=_day_start),
        "参数归因": _build_param_attribution_section(),
        "边界配置": _build_boundary_section(),
        "AI日报": _build_ai_report_section(),
    }
    if full_mode:
        pack["字段说明"] = {
            "mfe_pct": "持仓期间原方向最大有利波动百分比，用于判断是否拿住。",
            "mae_pct": "持仓期间最大逆向波动百分比，用于判断止损是否太紧。",
            "post_exit_*": "平仓后继续观察原方向 15/30/60/120 分钟，用于判断卖飞或方向是否选对。",
            "trade_diagnosis": "规则化交易主诊断：entry_bad点位错、direction_wrong方向错、valid_runner_missed/exit_too_early卖飞、stop_too_tight止损过紧等。",
            "diagnosis_tags": "一笔交易可同时命中多个标签；学习报告按这些标签做样本归因。",
            "学习报告": "按规则汇总亏损归因、点位错样本、方向错样本、卖飞样本和下一轮参数建议；只做复盘建议，不会自动改策略。",
            "entry_context": "开仓瞬间的候选池、妖币共享上下文、候选入池后路径、OI、taker、ATR、短期涨跌快照。",
            "entry_1m_profile": "开仓时的1分钟画像：pre 3m/5m/15m、EMA20偏离、ATR、回踩突破、Taker趋势和当前K量比。",
            "scalp_candidate_*": "从进入超短线候选池开始到开仓/当前的短线最大上/下波动，用于判断扫描器是否选到可交易波动。",
            "未开仓诊断": "记录AI通过后卡在执行层的原因，包括机会队列、1m暖机、Taker、ATR、追涨过滤、状态机等。",
            "关注池": "人工关注/禁入/等待确认列表；用于复盘选币，不会自动生成新策略。",
            "decision_cards": "妖币扫描生成的人工决策解释卡：允许/等待/禁止/观察，只作筛选参考。",
            "opportunity_queue": "妖币扫描先做15分钟级选币，再由主 AI/Surf 对 Top 候选做方向与风险终审；获得机会许可的币，才交给1m策略等待开仓点。",
        }
    return pack


def _maybe_gzip_response(
    payload: str | bytes, request: Request, media_type: str, filename: str
) -> StreamingResponse:
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    accept = (request.headers.get("accept-encoding") or "").lower()
    if "gzip" in accept:
        compressed = gzip.compress(body, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(compressed))
        return StreamingResponse(iter([compressed]), media_type=media_type, headers=headers)
    headers["Content-Length"] = str(len(body))
    return StreamingResponse(iter([body]), media_type=media_type, headers=headers)


@app.get("/api/scalp/analysis-pack.json")
async def export_scalp_analysis_pack_json(request: Request, detail: str = "slim", date: str = ""):
    """下载复盘包。detail=slim|full, date=YYYY-MM-DD(指定日期)|空=全部。"""
    pack = await _build_scalp_analysis_pack(detail=detail, date=date)
    full_mode = (detail or "slim").lower() == "full"
    date_tag = f"_{date}" if date else ""
    if full_mode:
        payload = json.dumps(pack, ensure_ascii=False, indent=2, default=str)
    else:
        payload = json.dumps(pack, ensure_ascii=False, separators=(",", ":"), default=str)
    return _maybe_gzip_response(
        payload, request,
        media_type="application/json; charset=utf-8",
        filename=f"squeezebot_analysis_pack{date_tag}.json",
    )


@app.get("/api/scalp/analysis-pack/available-dates")
async def analysis_pack_available_dates():
    """返回有交易数据的日期列表，供前端日期选择器使用。"""
    try:
        import os
        from config import DATA_DIR
        from persistence import read_jsonl
        trades_file = os.path.join(DATA_DIR, "strategy_trades.jsonl")
        dates: set[str] = set()
        if os.path.exists(trades_file):
            trades = read_jsonl(trades_file)
            for t in trades:
                ts = t.get("exit_time") or t.get("entry_time") or ""
                if ts and len(ts) >= 10:
                    dates.add(ts[:10])
        return JSONResponse(sorted(dates, reverse=True))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/scalp/analysis-pack.md")
async def export_scalp_analysis_pack_markdown(request: Request, detail: str = "slim", date: str = ""):
    """下载复盘包 MD。detail=slim|full, date=YYYY-MM-DD(指定日期)|空=全部。"""
    pack = await _build_scalp_analysis_pack(detail=detail, date=date)
    payload = _analysis_markdown(pack)
    date_tag = f"_{date}" if date else ""
    return _maybe_gzip_response(
        payload, request,
        media_type="text/markdown; charset=utf-8",
        filename=f"squeezebot_analysis_pack{date_tag}.md",
    )


@app.get("/api/scalp/paper/positions")
async def get_scalp_paper_positions():
    paper = {k: v for k, v in scalp_positions.items() if v.get("paper")}
    total_pnl = sum(v.get("realized_pnl", 0) for v in paper.values())
    return JSONResponse({"positions": paper, "count": len(paper), "total_realized_pnl": round(total_pnl, 4)})


@app.delete("/api/scalp/paper/positions")
async def clear_scalp_paper_positions():
    paper_keys = [k for k, v in scalp_positions.items() if v.get("paper")]
    for k in paper_keys:
        _signals_mod.set_scalp_position(k, None)
        if bot_state.scalp_bot:
            bot_state.scalp_bot.open_positions.pop(k, None)
    logger.info("📋 模拟仓位已全部清除 (%d 个)", len(paper_keys))
    return JSONResponse({"status": "success", "message": f"✅ 已清除 {len(paper_keys)} 个模拟仓位"})


@app.get("/api/scalp/symbols")
async def get_scalp_symbols():
    syms = bot_state.scalp_bot.monitored_symbols if bot_state.scalp_bot else []
    return JSONResponse({"symbols": syms, "count": len(syms)})


@app.get("/api/scalp/filter-stats")
async def get_scalp_filter_stats():
    return JSONResponse(_signals_mod.scalp_filter_stats or {})


# ─── WebSocket ────────────────────────────────────────────────────────────────

async def _ws_pump(websocket: WebSocket, q: std_queue.Queue, sleep: float = 0.1):
    host = websocket.client.host if websocket.client else ""
    if not _client_is_local(host):
        await websocket.close(code=1008)
        return
    if not _valid_panel_token(websocket.query_params.get("token", "")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            try:
                msg = q.get_nowait()
                await websocket.send_text(msg)
            except std_queue.Empty:
                try:
                    await asyncio.sleep(sleep)
                except asyncio.CancelledError:
                    return
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.debug("WS 异常: %s", e)


@app.get("/api/log/scalp/recent")
async def get_scalp_log_recent(limit: int = 200):
    """返回最近 N 条超短线日志。"""
    from log_manager import scalp_log_recent
    return JSONResponse(scalp_log_recent[-limit:])


@app.websocket("/ws/log")
async def ws_log_all(websocket: WebSocket):
    await _ws_pump(websocket, log_queue)


@app.websocket("/ws/log/scalp")
async def ws_log_scalp(websocket: WebSocket):
    await _ws_pump(websocket, scalp_log_queue)


@app.websocket("/ws/scalp/signals")
async def ws_scalp_signals(websocket: WebSocket):
    await _ws_pump(websocket, scalp_signal_queue, sleep=0.3)


# ─── 策略统计 API ──────────────────────────────────────────────────────────────

@app.get("/api/strategy/stats")
async def strategy_stats(mode: str = "all"):
    try:
        from strategy_stats import get_dashboard
        return JSONResponse(get_dashboard(mode=mode))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/strategy/trades")
async def strategy_trades(limit: int = 50, mode: str = "all"):
    try:
        from strategy_stats import get_trades
        return JSONResponse(get_trades(limit=limit, mode=mode))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Evolver Dry-Run API ────────────────────────────────────────────────────────

@app.get("/api/evolver/dry-run")
async def evolver_dry_run():
    """只读模拟 Evolver 运行，不写任何配置。"""
    try:
        from strategy_evolver import load_trade_data, compute_strategy_metrics, \
            detect_failure_patterns, propose_param_updates

        result = {}

        # 1. 只读指标（不调 maybe_schedule_evolver_job，避免副作用）
        from config import config_manager
        cfg = config_manager.settings
        from strategy_evolver import _get_evolver_state
        evo_state = _get_evolver_state()
        result["当前状态"] = {
            "trades_since_last_policy": evo_state.get("trades_since_last_policy", 0),
            "current_policy_version": evo_state.get("current_policy_version", ""),
            "last_evolver_run_at": evo_state.get("last_evolver_run_at", 0.0),
            "min_trades_required": int(cfg.get("EVOLVER_RUN_AFTER_CLOSED_TRADES", 30) or 30),
            "trades_needed": max(0, int(cfg.get("EVOLVER_RUN_AFTER_CLOSED_TRADES", 30) or 30) - evo_state.get("trades_since_last_policy", 0)),
        }

        # 2. 数据与指标
        trades = load_trade_data(force=True)
        result["交易数据"] = {"总笔数": len(trades)}
        if len(trades) >= 15:
            metrics = compute_strategy_metrics(trades)
            result["策略指标"] = {tag: {k: m[k] for k in ("total_trades", "win_rate", "expectancy", "profit_factor")}
                                  for tag, m in metrics.items()}
            patterns = detect_failure_patterns(metrics, trades)
            result["失败模式"] = patterns[:10]

            # 3. 模拟 proposal（只读，不 apply）
            proposals = propose_param_updates(metrics, patterns, cfg)
            result["生成的 Proposal"] = [{"key": p["key"], "old": p.get("old"), "new": p.get("new"),
                                           "reason": p.get("reason", "")} for p in proposals]

        return JSONResponse(result)
    except Exception as e:
        logger.error("dry-run 失败: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── AI 复盘日报 API ─────────────────────────────────────────────────────────────

@app.get("/api/ai-report/latest")
async def ai_report_latest():
    """返回最新 AI 复盘报告。"""
    try:
        from ai_reporter import get_latest_report
        report = get_latest_report()
        if report is None:
            return JSONResponse({"error": "暂无报告"}, status_code=404)
        return JSONResponse(report)
    except Exception as e:
        logger.error("ai-report/latest 失败: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/ai-report/list")
async def ai_report_list(limit: int = 10):
    """返回最近 N 条 AI 复盘报告。"""
    try:
        from ai_reporter import get_recent_reports
        reports = get_recent_reports(limit=limit)
        return JSONResponse({"reports": reports, "total": len(reports)})
    except Exception as e:
        logger.error("ai-report/list 失败: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/ai-report/generate")
async def ai_report_generate():
    """触发 AI 生成一篇新的复盘报告。"""
    try:
        from ai_reporter import generate_report
        report = await generate_report()
        return JSONResponse(report)
    except Exception as e:
        logger.error("ai-report/generate 失败: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/ai-report/structured")
async def ai_report_structured(limit: int = 20):
    """返回结构化复盘数据（不依赖 LLM）。"""
    try:
        from persistence import read_jsonl
        import os
        from config import DATA_DIR

        result = {}

        # 1. 规则选择器事件
        events_file = os.path.join(DATA_DIR, "rule_selector_events.jsonl")
        if os.path.exists(events_file):
            events = read_jsonl(events_file)
            recent = events[-limit:] if len(events) > limit else events
            result["rule_selector_events"] = {
                "total": len(events),
                "recent": [{k: v for k, v in e.items() if k != "ts"}
                           for e in reversed(recent)],
                "block_count": sum(1 for e in events if e.get("decision") == "block"),
                "warn_count": sum(1 for e in events if e.get("decision") == "warn"),
            }

        # 2. Guard 事件
        guard_file = os.path.join(DATA_DIR, "guard_events.jsonl")
        if os.path.exists(guard_file):
            guards = read_jsonl(guard_file)
            recent_g = guards[-limit:] if len(guards) > limit else guards
            result["guard_events"] = {
                "total": len(guards),
                "recent": [{"event": g.get("event_type", ""),
                            "severity": g.get("severity", ""),
                            "reason": str(g.get("reason", ""))[:100]}
                           for g in reversed(recent_g)],
            }

        # 3. learning_memory 弱规则
        try:
            from learning_memory import get_memory_summary
            result["learning_memory"] = get_memory_summary()
        except Exception:
            result["learning_memory"] = {}

        # 4. 近期成交概况
        trades_file = os.path.join(DATA_DIR, "strategy_trades.jsonl")
        if os.path.exists(trades_file):
            trades = read_jsonl(trades_file)
            recent_t = trades[-limit:] if len(trades) > limit else trades
            wins = sum(1 for t in recent_t if float(t.get("pnl_usdt", 0) or 0) >= 0)
            pnl = sum(float(t.get("pnl_usdt", 0) or 0) for t in recent_t)
            result["recent_trades"] = {
                "total": len(recent_t),
                "wins": wins,
                "win_rate": round(wins / max(len(recent_t), 1), 2),
                "total_pnl": round(pnl, 2),
            }

        # 5. Evolver 状态
        try:
            from risk_guard import get_evolver_status
            evo = get_evolver_status()
            result["evolver"] = {
                "status": evo.get("runtime_status", "UNKNOWN"),
                "frozen": evo.get("frozen", False),
                "policy_version": evo.get("current_policy_version", ""),
                "rollback_count": evo.get("rollback_count", 0),
                "last_error": evo.get("last_error", ""),
            }
        except Exception:
            result["evolver"] = {}

        return JSONResponse(result)
    except Exception as e:
        logger.error("ai-report/structured 失败: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Evolver 状态 API ──────────────────────────────────────────────────────────

@app.get("/api/evolver/status")
async def evolver_status():
    try:
        from risk_guard import get_evolver_status, get_locked_params_status
        return JSONResponse({**get_evolver_status(), "locked_params": get_locked_params_status()})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/evolver/history")
async def evolver_history(limit: int = 20):
    try:
        from risk_guard import get_recent_evolver_history
        return JSONResponse(get_recent_evolver_history(limit=limit))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/evolver/patches")
async def evolver_patches(limit: int = 50):
    try:
        from risk_guard import get_recent_param_patches
        return JSONResponse(get_recent_param_patches(limit=limit))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/evolver/performance")
async def evolver_performance(limit: int = 20):
    try:
        from risk_guard import get_recent_evolver_history
        return JSONResponse(get_recent_evolver_history(limit=limit))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/evolver/health")
async def evolver_health():
    try:
        from risk_guard import run_evolver_health_check
        return JSONResponse(run_evolver_health_check())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/evolver/run-once")
async def evolver_run_once():
    try:
        from evolver_runtime import run_evolver_job
        result = run_evolver_job()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/evolver/pause")
async def evolver_pause():
    try:
        config_manager.settings["EVOLVER_ENABLED"] = False
        config_manager._persist()
        try:
            from evolver_runtime import _write_event, _get_state
            _write_event("PAUSED", _get_state(), reason="api_pause")
        except Exception:
            pass
        return JSONResponse({"status": "paused", "EVOLVER_ENABLED": False})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/evolver/resume")
async def evolver_resume():
    try:
        config_manager.settings["EVOLVER_ENABLED"] = True
        config_manager._persist()
        try:
            from evolver_runtime import _write_event, _get_state
            _write_event("RESUMED", _get_state(), reason="api_resume")
        except Exception:
            pass
        return JSONResponse({"status": "resumed", "EVOLVER_ENABLED": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Autopilot Guard API ────────────────────────────────────────────────────

@app.get("/api/autopilot/status")
async def autopilot_status():
    try:
        from autopilot_guard import (
            load_locked_param_snapshot, get_current_locked_param_values,
            get_guard_events, load_e2e_status, check_config_integrity
        )
        snapshot = load_locked_param_snapshot()
        current = get_current_locked_param_values()
        events = get_guard_events(limit=10)
        e2e = load_e2e_status()
        integrity = check_config_integrity()
        return JSONResponse({
            "guard_enabled": True,
            "snapshot": snapshot,
            "current_locked": current,
            "locked_params_ok": integrity.get("ok", True),
            "locked_issues": integrity.get("issues", []),
            "e2e": e2e,
            "recent_events": events,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/autopilot/events")
async def autopilot_events(limit: int = 50):
    try:
        from autopilot_guard import get_guard_events
        return JSONResponse(get_guard_events(limit=limit))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/autopilot/run-guard")
async def autopilot_run_guard():
    try:
        from autopilot_guard import run_periodic_guard
        result = run_periodic_guard()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/autopilot/reset-locked-snapshot")
async def autopilot_reset_snapshot():
    try:
        from autopilot_guard import save_locked_param_snapshot, write_guard_event
        snapshot = save_locked_param_snapshot()
        write_guard_event("MANUAL_SNAPSHOT_RESET", "CRITICAL",
                          "locked params snapshot manually reset",
                          {"snapshot": snapshot})
        return JSONResponse({"status": "snapshot_reset", "snapshot": snapshot})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/autopilot/unfreeze")
async def autopilot_unfreeze():
    try:
        from autopilot_guard import unfreeze_evolver_if_safe
        ok = unfreeze_evolver_if_safe()
        return JSONResponse({"unfrozen": ok})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── 妖币扫描器 API ───────────────────────────────────────────────────────────

@app.get("/api/yaobi/status")
async def yaobi_status():
    try:
        from config import surf_credentials_status, okx_credentials_status, ai_credentials_status
        from scanner.sources import binance_square
        credentials = {
            "surf": surf_credentials_status(),
            "okx": okx_credentials_status(),
            "ai": ai_credentials_status(),
            "binance_square": binance_square.auth_status(),
        }
    except Exception:
        credentials = {}
    is_running = bool(bot_state.yaobi_task and not bot_state.yaobi_task.done())
    return JSONResponse({
        "running":       is_running,
        "last_scan":     scan_status.get("last_scan"),
        "scanning":      scan_status.get("scanning", False),
        "total_scanned": scan_status.get("total_scanned", 0),
        "scored":        scan_status.get("scored", 0),
        "anomalies":     scan_status.get("anomalies", 0),
        "min_anomaly":   scan_status.get("min_anomaly", config_manager.settings.get("YAOBI_MIN_ANOMALY_SCORE", 35)),
        "sources":       scan_status.get("sources", {}),
        "provider_metrics": provider_metrics_snapshot(),
        "ai_status":     scan_status.get("ai_status") or ai_provider_status(),
        "opportunity_count": scan_status.get("opportunity_count", 0),
        "credentials":   credentials,
        "errors":        scan_status.get("errors", [])[-3:],
    })


@app.post("/api/yaobi/start")
async def yaobi_start():
    if bot_state.yaobi_task and not bot_state.yaobi_task.done():
        return JSONResponse({"status": "already_running", "message": "🔍 扫描器已在运行"})
    from scanner.yaobi_scanner import YaobiScanner
    bot_state.yaobi_scanner = YaobiScanner()
    bot_state.yaobi_task = asyncio.create_task(bot_state.yaobi_scanner.run())
    logger.info("🔍 妖币扫描器通过 Web 面板启动")
    return JSONResponse({"status": "started", "message": "🔍 妖币扫描器已启动"})


@app.post("/api/yaobi/stop")
async def yaobi_stop():
    if bot_state.yaobi_scanner:
        bot_state.yaobi_scanner.running = False
    if bot_state.yaobi_task and not bot_state.yaobi_task.done():
        bot_state.yaobi_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(bot_state.yaobi_task), timeout=3)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    bot_state.yaobi_task    = None
    bot_state.yaobi_scanner = None
    logger.info("🔍 妖币扫描器通过 Web 面板停止")
    return JSONResponse({"status": "stopped", "message": "🔍 妖币扫描器已停止"})


@app.post("/api/yaobi/scan")
async def yaobi_scan_now():
    if scan_status.get("scanning"):
        return JSONResponse({"status": "busy", "message": "⏳ 正在扫描中，请稍候"})
    from scanner.yaobi_scanner import YaobiScanner
    scanner = bot_state.yaobi_scanner or YaobiScanner()
    asyncio.create_task(scanner.run_once())
    return JSONResponse({"status": "started", "message": "🔍 手动扫描已触发"})


@app.get("/api/yaobi/candidates")
async def yaobi_candidates(
    min_score: int = 0,
    chain: str = "",
    source: str = "",
    limit: int = 100,
):
    items = get_sorted_candidates(min_score=min_score, chain=chain, source=source)
    return JSONResponse({"candidates": items[:limit], "total": len(items)})


@app.get("/api/yaobi/opportunities")
async def yaobi_opportunities(limit: int = 20):
    items = get_opportunity_queue(limit=limit)
    return JSONResponse({
        "opportunities": items,
        "total": len(items),
        "ai_status": scan_status.get("ai_status") or ai_provider_status(),
        "knowledge": knowledge_status(),
    })


@app.get("/api/yaobi/tiers")
async def yaobi_tiers(limit: int = 20):
    """三层决策面板：把候选按 decision_tier 分组返回（v2.8 风格）。"""
    all_candidates = get_sorted_candidates(min_score=0)
    by_tier = {"L1_MAIN": [], "L2_AMBUSH": [], "RISK_AVOID": []}
    for c in all_candidates:
        tier = c.get("decision_tier") or ""
        if tier in by_tier:
            by_tier[tier].append(c)
    # 按 score 排序，截断
    for tier in by_tier:
        by_tier[tier].sort(key=lambda x: int(x.get("score") or 0), reverse=True)
        by_tier[tier] = by_tier[tier][:limit]

    # 子家族分组（在每个 tier 内按 subtype 二级聚合）
    def group_by_subtype(items: list[dict]) -> dict:
        out: dict[str, list[dict]] = {}
        for c in items:
            key = c.get("decision_subtype") or "其他"
            out.setdefault(key, []).append(c)
        return out

    return JSONResponse({
        "l1_main": {
            "count": len(by_tier["L1_MAIN"]),
            "items": by_tier["L1_MAIN"],
            "subtypes": group_by_subtype(by_tier["L1_MAIN"]),
        },
        "l2_ambush": {
            "count": len(by_tier["L2_AMBUSH"]),
            "items": by_tier["L2_AMBUSH"],
            "subtypes": group_by_subtype(by_tier["L2_AMBUSH"]),
        },
        "risk_avoid": {
            "count": len(by_tier["RISK_AVOID"]),
            "items": by_tier["RISK_AVOID"],
            "subtypes": group_by_subtype(by_tier["RISK_AVOID"]),
        },
        "neutral_count": len(all_candidates)
            - len(by_tier["L1_MAIN"]) - len(by_tier["L2_AMBUSH"]) - len(by_tier["RISK_AVOID"]),
    })


@app.get("/api/yaobi/anomalies")
async def yaobi_anomalies(
    min_anomaly: int = 1,
    limit: int = 100,
):
    items = get_anomaly_candidates(min_anomaly=min_anomaly, limit=limit)
    return JSONResponse({"candidates": items, "total": len(items)})


@app.delete("/api/yaobi/candidates")
async def yaobi_clear_candidates():
    clear_candidates()
    logger.info("妖币候选库已清空")
    return JSONResponse({"status": "success", "message": "✅ 候选库已清空"})


@app.get("/api/yaobi/obsidian/status")
async def yaobi_obsidian_status():
    from scanner.obsidian import vault_path
    import os
    vp = vault_path()
    exists = os.path.isdir(vp)
    file_count = 0
    if exists:
        for _, _, files in os.walk(vp):
            file_count += len(files)
    return JSONResponse({
        "vault_path":  vp,
        "exists":      exists,
        "file_count":  file_count,
    })


@app.websocket("/ws/yaobi/candidates")
async def ws_yaobi_candidates(websocket: WebSocket):
    await _ws_pump(websocket, candidates_queue, sleep=0.5)


@app.websocket("/ws/yaobi/opportunities")
async def ws_yaobi_opportunities(websocket: WebSocket):
    await _ws_pump(websocket, opportunity_update_queue, sleep=0.5)
