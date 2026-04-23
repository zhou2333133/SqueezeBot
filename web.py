import asyncio
import collections
import csv
import hmac
import io
import json
import logging
import os
import queue as std_queue
import time
from datetime import datetime
from ipaddress import ip_address

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
import signals as _signals_mod
from signals import (
    scalp_signal_queue, scalp_signals_history, scalp_positions, scalp_trade_history,
)
from scanner.candidates import (
    candidates_queue, get_sorted_candidates, get_anomaly_candidates,
    clear_candidates, scan_status,
)
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


def _valid_panel_token(token: str | None) -> bool:
    if not PANEL_TOKEN:
        return True
    return bool(token) and hmac.compare_digest(str(token), PANEL_TOKEN)


def _auth_meta() -> dict:
    return {
        "token_required": bool(PANEL_TOKEN),
        "local_only": PANEL_LOCAL_ONLY,
    }


def _token_from_request(request: Request) -> str:
    return request.headers.get(PANEL_TOKEN_HEADER, "") or request.query_params.get("token", "")


@app.middleware("http")
async def panel_security(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in _AUTH_PUBLIC_PATHS:
        host = request.client.host if request.client else ""
        if not _client_is_local(host):
            return JSONResponse({"error": "local_only"}, status_code=403)
        if not _valid_panel_token(_token_from_request(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ─── 页面 ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"config": _redact_config(config_manager.settings), "auth": _auth_meta()},
    )


# ─── 面板认证 API ─────────────────────────────────────────────────────────────

@app.get("/api/auth/status")
async def auth_status():
    return JSONResponse(_auth_meta())


@app.post("/api/auth/check")
async def auth_check(request: Request):
    host = request.client.host if request.client else ""
    if not _client_is_local(host):
        return JSONResponse({"status": "error", "message": "local_only"}, status_code=403)
    if not _valid_panel_token(_token_from_request(request)):
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
    try:
        form = await request.form()
        symbol   = str(form.get("symbol", "")).strip().upper()
        side     = str(form.get("side",   "")).strip().upper()
        quantity = float(form.get("quantity", 0))
    except Exception:
        return JSONResponse({"status": "error", "message": "❌ 参数解析失败"}, status_code=400)

    if side not in ("BUY", "SELL"):
        return JSONResponse({"status": "error", "message": "❌ side 只能是 BUY 或 SELL"}, status_code=400)
    if quantity <= 0:
        return JSONResponse({"status": "error", "message": "❌ 数量必须大于 0"}, status_code=400)

    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            trader   = BinanceTrader(session)
            leverage = config_manager.settings.get("SCALP_LEVERAGE", 10)
            await trader.set_leverage(symbol, leverage)
            res = await trader.place_market_order(symbol, side, quantity)
        if res:
            return JSONResponse({
                "status":  "success",
                "message": f"✅ {side} {symbol} ×{quantity} 成功 (OrderID: {res.get('orderId', 'N/A')})",
            })
        return JSONResponse({"status": "error", "message": "❌ 下单失败，请检查 API Key 权限和余额"}, status_code=400)
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
    return JSONResponse({
        "running":         is_running,
        "positions_count": len(scalp_positions),
        "signals_count":   len(scalp_signals_history),
        "symbols_count":   sym_count,
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
    scalp_positions.clear()
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


@app.get("/api/scalp/equity")
async def get_scalp_equity():
    return JSONResponse(_scalp_equity_summary(list(scalp_trade_history)))


@app.delete("/api/scalp/trades")
async def clear_scalp_trades():
    scalp_trade_history.clear()
    return JSONResponse({"status": "success", "message": "✅ 历史成交已清空"})


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
        "passed": 0,
        "cfg_sq_oi_major": cfg.get("SQUEEZE_OI_DROP_MAJOR", 0.5),
        "cfg_sq_oi_mid": cfg.get("SQUEEZE_OI_DROP_MID", 1.0),
        "cfg_sq_oi_meme": cfg.get("SQUEEZE_OI_DROP_MEME", 1.5),
        "cfg_sq_taker": cfg.get("SQUEEZE_TAKER_MIN", 0.65),
        "cfg_bo_taker": cfg.get("BREAKOUT_TAKER_MIN", 0.62),
        "cfg_bo_min_pct": cfg.get("BREAKOUT_MIN_PCT", 0.10),
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
                scalp_positions.pop(symbol, None)
                paper_cleared += 1
            else:
                real_symbols.add(symbol)

    for symbol, pos in list(scalp_positions.items()):
        if pos.get("paper"):
            scalp_positions.pop(symbol, None)
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
    trades = list(scalp_trade_history)
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
            f"PnL={t.get('pnl_usdt')}U MFE={t.get('mfe_pct')}% MAE={t.get('mae_pct')}% "
            f"Post30={t.get('post_exit_30m_favorable_pct', 'NA')}%"
        )
    return "\n".join(lines) + "\n"


async def _build_scalp_analysis_pack() -> dict:
    trades = list(scalp_trade_history)
    bot = bot_state.scalp_bot
    runtime = {
        "scalp_running": bool(bot_state.scalp_task and not bot_state.scalp_task.done()),
        "yaobi_running": bool(bot_state.yaobi_task and not bot_state.yaobi_task.done()),
        "open_positions": len(scalp_positions),
        "scalp_signals": len(scalp_signals_history),
        "scalp_trades": len(trades),
    }
    provider_diag = {
        "provider_metrics": provider_metrics_snapshot(),
        "scan_sources": scan_status.get("sources", {}),
    }
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

    cfg = _redact_config(config_manager.settings)
    candidate_snapshot = get_sorted_candidates()[:100]
    pack = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "用途": "把这个 JSON/Markdown 发给 Codex，可用于复盘选币、入场、出场、数据源调用和参数问题。",
        "运行状态": runtime,
        "策略参数": cfg,
        "策略报告": await _scalp_report_or_none(),
        "净值曲线摘要": _scalp_equity_summary(trades),
        "成交分组统计": {
            "by_signal": _group_trade_stats(trades, "signal_label"),
            "by_direction": _group_trade_stats(trades, "direction"),
            "by_market_state": _group_trade_stats(trades, "market_state"),
            "by_close_reason": _group_trade_stats(trades, "close_reason"),
            "by_symbol": _group_trade_stats(trades, "symbol"),
        },
        "成交明细": trades,
        "信号样本": scalp_signals_history[-120:],
        "当前持仓": dict(scalp_positions),
        "过滤统计快照": _signals_mod.scalp_filter_stats,
        "候选池快照": {
            "symbols": list(getattr(bot, "candidate_symbols", []) or []),
            "meta": dict(getattr(bot, "candidate_meta", {}) or {}),
        },
        "关注池": list_watch_items(candidate_snapshot),
        "妖币扫描": {
            "status": dict(scan_status),
            "top_candidates": candidate_snapshot,
            "anomaly_candidates": get_anomaly_candidates(
                min_anomaly=int(config_manager.settings.get("YAOBI_MIN_ANOMALY_SCORE", 35)),
                limit=100,
            ),
            "decision_cards": [
                {
                    "symbol": c.get("symbol"),
                    "action": c.get("decision_action"),
                    "confidence": c.get("decision_confidence"),
                    "reasons": c.get("decision_reasons", []),
                    "risks": c.get("decision_risks", []),
                    "missing": c.get("decision_missing", []),
                    "note": c.get("decision_note", ""),
                    "oi_trend_grade": c.get("oi_trend_grade", ""),
                    "anomaly_score": c.get("anomaly_score", 0),
                    "score": c.get("score", 0),
                }
                for c in candidate_snapshot[:50]
            ],
        },
        "数据源诊断": provider_diag,
        "字段说明": {
            "mfe_pct": "持仓期间原方向最大有利波动百分比，用于判断是否拿住。",
            "mae_pct": "持仓期间最大逆向波动百分比，用于判断止损是否太紧。",
            "post_exit_*": "平仓后继续观察原方向 15/30/60/120 分钟，用于判断卖飞或方向是否选对。",
            "entry_context": "开仓瞬间的候选池、妖币共享上下文、候选入池后路径、OI、taker、ATR、短期涨跌快照。",
            "scalp_candidate_*": "从进入超短线候选池开始到开仓/当前的短线最大上/下波动，用于判断扫描器是否选到可交易波动。",
            "关注池": "人工关注/禁入/等待确认列表；用于复盘选币，不会自动生成新策略。",
            "decision_cards": "妖币扫描生成的人工决策解释卡：允许/等待/禁止/观察，只作筛选参考。",
        },
    }
    return pack


@app.get("/api/scalp/analysis-pack.json")
async def export_scalp_analysis_pack_json():
    pack = await _build_scalp_analysis_pack()
    payload = json.dumps(pack, ensure_ascii=False, indent=2, default=str)
    return StreamingResponse(
        iter([payload]),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=squeezebot_analysis_pack.json"},
    )


@app.get("/api/scalp/analysis-pack.md")
async def export_scalp_analysis_pack_markdown():
    pack = await _build_scalp_analysis_pack()
    payload = _analysis_markdown(pack)
    return StreamingResponse(
        iter([payload]),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=squeezebot_analysis_pack.md"},
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
        scalp_positions.pop(k, None)
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


@app.websocket("/ws/log")
async def ws_log_all(websocket: WebSocket):
    await _ws_pump(websocket, log_queue)


@app.websocket("/ws/log/scalp")
async def ws_log_scalp(websocket: WebSocket):
    await _ws_pump(websocket, scalp_log_queue)


@app.websocket("/ws/scalp/signals")
async def ws_scalp_signals(websocket: WebSocket):
    await _ws_pump(websocket, scalp_signal_queue, sleep=0.3)


# ─── 妖币扫描器 API ───────────────────────────────────────────────────────────

@app.get("/api/yaobi/status")
async def yaobi_status():
    try:
        from config import surf_credentials_status, okx_credentials_status
        from scanner.sources import binance_square
        credentials = {
            "surf": surf_credentials_status(),
            "okx": okx_credentials_status(),
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
        "credentials":   credentials,
        "errors":        scan_status.get("errors", [])[-3:],
    })


@app.get("/api/yaobi/diagnostics")
async def yaobi_diagnostics():
    from scanner.sources import okx_market, binance_square
    from config import surf_credentials_status
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            okx_diag, square_diag = await asyncio.gather(
                okx_market.diagnose(session),
                binance_square.diagnose(session, rows=5),
            )
        return JSONResponse({
            "okx": okx_diag,
            "surf": surf_credentials_status(),
            "binance_square": square_diag,
            "provider_metrics": provider_metrics_snapshot(),
        })
    except Exception as e:
        logger.error("妖币数据源诊断失败: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


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
