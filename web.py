import asyncio
import csv
import logging
import os
import queue as std_queue

import aiohttp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import bot_state
from config import config_manager, DATA_DIR
from log_manager import log_queue, swing_log_queue, scalp_log_queue, fade_log_queue
from signals import (
    signal_queue, signals_history,
    scalp_signal_queue, scalp_signals_history, scalp_positions,
    fade_signal_queue, fade_signals_history, fade_positions,
)
from scanner.candidates import (
    candidates_queue, get_sorted_candidates, clear_candidates, scan_status,
)
from trader import BinanceTrader

logger = logging.getLogger(__name__)

app = FastAPI(title="SqueezeBot Control Panel")

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ─── 页面 ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"config": config_manager.settings},
    )


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
    return JSONResponse(config_manager.settings)


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
            leverage = config_manager.settings.get("LEVERAGE", 5)
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


# ─── 中线状态 & 信号 ──────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    cfg = config_manager.settings
    return JSONResponse({
        "status":           "running",
        "auto_trade":       cfg.get("AUTO_TRADE_ENABLED",   False),
        "long_enabled":     cfg.get("ENABLE_LONG_STRATEGY", True),
        "short_enabled":    cfg.get("ENABLE_SHORT_STRATEGY", False),
        "interval_minutes": cfg.get("INTERVAL_MINUTES",     5),
        "leverage":         cfg.get("LEVERAGE",             5),
        "position_usdt":    cfg.get("POSITION_SIZE_USDT",  20),
    })


@app.get("/api/signals")
async def get_signals(limit: int = 50):
    return JSONResponse({"signals": signals_history[-limit:][::-1], "total": len(signals_history)})


@app.delete("/api/signals")
async def clear_signals():
    signals_history.clear()
    logger.info("中线信号历史已清空")
    return JSONResponse({"status": "success", "message": "✅ 信号历史已清空"})


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


# ─── WebSocket ────────────────────────────────────────────────────────────────

async def _ws_pump(websocket: WebSocket, q: std_queue.Queue, sleep: float = 0.1):
    """通用 WebSocket 日志/信号推送"""
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


@app.websocket("/ws/log/swing")
async def ws_log_swing(websocket: WebSocket):
    await _ws_pump(websocket, swing_log_queue)


@app.websocket("/ws/log/scalp")
async def ws_log_scalp(websocket: WebSocket):
    await _ws_pump(websocket, scalp_log_queue)


@app.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket):
    await _ws_pump(websocket, signal_queue, sleep=0.5)


@app.websocket("/ws/scalp/signals")
async def ws_scalp_signals(websocket: WebSocket):
    await _ws_pump(websocket, scalp_signal_queue, sleep=0.3)


# ─── 一直做空控制 API ─────────────────────────────────────────────────────────

@app.get("/api/fade/status")
async def fade_status():
    is_running = bool(bot_state.fade_task and not bot_state.fade_task.done())
    sym_count  = 0
    if bot_state.fade_bot and bot_state.fade_bot.monitored_symbols:
        sym_count = len(bot_state.fade_bot.monitored_symbols)
    return JSONResponse({
        "running":         is_running,
        "positions_count": len(fade_positions),
        "signals_count":   len(fade_signals_history),
        "symbols_count":   sym_count,
    })


@app.post("/api/fade/start")
async def fade_start():
    if bot_state.fade_task and not bot_state.fade_task.done():
        return JSONResponse({"status": "already_running", "message": "📉 已在运行中"})
    from bot_fade import BinanceFadeBot
    bot_state.fade_bot  = BinanceFadeBot()
    bot_state.fade_task = asyncio.create_task(bot_state.fade_bot.run())
    logger.info("📉 一直做空机器人通过 Web 面板启动")
    return JSONResponse({"status": "started", "message": "📉 一直做空机器人已启动"})


@app.post("/api/fade/stop")
async def fade_stop():
    if bot_state.fade_bot:
        bot_state.fade_bot.running = False
    if bot_state.fade_task and not bot_state.fade_task.done():
        bot_state.fade_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(bot_state.fade_task), timeout=3)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    bot_state.fade_task = None
    bot_state.fade_bot  = None
    fade_positions.clear()
    logger.info("📉 一直做空机器人通过 Web 面板停止")
    return JSONResponse({"status": "stopped", "message": "📉 一直做空机器人已停止"})


@app.post("/api/fade/refresh")
async def fade_refresh_symbols():
    if bot_state.fade_bot and bot_state.fade_bot.running:
        await bot_state.fade_bot.refresh_symbols()
        return JSONResponse({
            "status":  "success",
            "message": f"✅ 已刷新，监控 {len(bot_state.fade_bot.monitored_symbols)} 个币种",
        })
    return JSONResponse({"status": "error", "message": "❌ 机器人未运行"}, status_code=400)


@app.get("/api/fade/positions")
async def get_fade_positions():
    return JSONResponse({"positions": dict(fade_positions)})


@app.get("/api/fade/signals")
async def get_fade_signals(limit: int = 50):
    return JSONResponse({"signals": fade_signals_history[-limit:][::-1], "total": len(fade_signals_history)})


@app.delete("/api/fade/signals")
async def clear_fade_signals():
    fade_signals_history.clear()
    logger.info("一直做空信号历史已清空")
    return JSONResponse({"status": "success", "message": "✅ 做空信号已清空"})


@app.get("/api/fade/paper/positions")
async def get_fade_paper_positions():
    paper = {k: v for k, v in fade_positions.items() if v.get("paper")}
    total_pnl = sum(v.get("realized_pnl", 0) for v in paper.values())
    return JSONResponse({"positions": paper, "count": len(paper), "total_realized_pnl": round(total_pnl, 4)})


@app.delete("/api/fade/paper/positions")
async def clear_fade_paper_positions():
    paper_keys = [k for k, v in fade_positions.items() if v.get("paper")]
    for k in paper_keys:
        fade_positions.pop(k, None)
        if bot_state.fade_bot:
            bot_state.fade_bot.open_positions.pop(k, None)
    logger.info("📉 模拟仓位已全部清除 (%d 个)", len(paper_keys))
    return JSONResponse({"status": "success", "message": f"✅ 已清除 {len(paper_keys)} 个模拟仓位"})


@app.websocket("/ws/fade/signals")
async def ws_fade_signals(websocket: WebSocket):
    await _ws_pump(websocket, fade_signal_queue, sleep=0.3)


@app.websocket("/ws/log/fade")
async def ws_log_fade(websocket: WebSocket):
    await _ws_pump(websocket, fade_log_queue)


# ─── 妖币扫描器 API ───────────────────────────────────────────────────────────

@app.get("/api/yaobi/status")
async def yaobi_status():
    is_running = bool(bot_state.yaobi_task and not bot_state.yaobi_task.done())
    return JSONResponse({
        "running":       is_running,
        "last_scan":     scan_status.get("last_scan"),
        "scanning":      scan_status.get("scanning", False),
        "total_scanned": scan_status.get("total_scanned", 0),
        "scored":        scan_status.get("scored", 0),
        "sources":       scan_status.get("sources", {}),
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
    """触发单次立即扫描（不影响定时循环）"""
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
