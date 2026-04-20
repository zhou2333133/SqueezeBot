import asyncio
import collections
import csv
import io
import logging
import os
import queue as std_queue

import aiohttp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import bot_state
from config import config_manager, DATA_DIR
from log_manager import log_queue, scalp_log_queue
import signals as _signals_mod
from signals import (
    scalp_signal_queue, scalp_signals_history, scalp_positions, scalp_trade_history,
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


@app.delete("/api/scalp/trades")
async def clear_scalp_trades():
    scalp_trade_history.clear()
    return JSONResponse({"status": "success", "message": "✅ 历史成交已清空"})


@app.get("/api/scalp/trades/csv")
async def export_scalp_trades_csv():
    trades = list(scalp_trade_history)
    if not trades:
        return JSONResponse({"message": "暂无成交记录"}, status_code=404)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=trades[0].keys())
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
    win_pnls  = [t["pnl_usdt"] for t in trades if t.get("pnl_usdt", 0) > 0]
    loss_pnls = [t["pnl_usdt"] for t in trades if t.get("pnl_usdt", 0) <= 0]

    by_reason = dict(collections.Counter(t.get("close_reason", "?") for t in trades))
    by_label  = dict(collections.Counter(t.get("signal_label", "?") for t in trades).most_common(10))
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
            "TP1_RR":           cfg.get("SCALP_TP1_RR"),
            "TP2_RR":           cfg.get("SCALP_TP2_RR"),
            "TP1平仓比例":      cfg.get("SCALP_TP1_RATIO"),
            "TP2平仓比例":      cfg.get("SCALP_TP2_RATIO"),
            "TP3追踪%":         cfg.get("SCALP_TP3_TRAIL_PCT"),
            "候选池上限":       cfg.get("SCALP_CANDIDATE_LIMIT"),
            "OI轮询秒":         cfg.get("OI_POLL_INTERVAL"),
            "信号冷却秒":       cfg.get("SIGNAL_COOLDOWN_SECONDS"),
            "轧空OI阈值-大币%": cfg.get("SQUEEZE_OI_DROP_MAJOR"),
            "轧空OI阈值-中币%": cfg.get("SQUEEZE_OI_DROP_MID"),
            "轧空OI阈值-Meme%": cfg.get("SQUEEZE_OI_DROP_MEME"),
            "挤压确认反弹%":    cfg.get("SQUEEZE_WICK_PCT"),
            "挤压Taker阈值":    cfg.get("SQUEEZE_TAKER_MIN"),
            "突破Taker阈值":    cfg.get("BREAKOUT_TAKER_MIN"),
        },
        "整体统计": {
            "总成交笔数":  total,
            "盈利笔数":    wins,
            "亏损笔数":    total - wins,
            "胜率%":       round(wins / total * 100, 1),
            "总盈亏USDT":  round(total_pnl, 4),
            "平均盈利USDT": round(sum(win_pnls) / len(win_pnls), 4) if win_pnls else 0,
            "平均亏损USDT": round(sum(loss_pnls) / len(loss_pnls), 4) if loss_pnls else 0,
            "盈亏比":      round(
                (sum(win_pnls) / len(win_pnls)) / abs(sum(loss_pnls) / len(loss_pnls)), 2
            ) if win_pnls and loss_pnls else "N/A",
        },
        "最佳单笔": {
            "品种": best.get("symbol"), "方向": best.get("direction"),
            "信号": best.get("signal_label"), "盈亏USDT": best.get("pnl_usdt"),
            "时间": best.get("entry_time"),
        },
        "最差单笔": {
            "品种": worst.get("symbol"), "方向": worst.get("direction"),
            "信号": worst.get("signal_label"), "盈亏USDT": worst.get("pnl_usdt"),
            "时间": worst.get("entry_time"),
        },
        "平仓原因分布":   by_reason,
        "信号类型分布":   by_label,
        "盈利最多币种TOP5":  dict(top5),
        "亏损最多币种TOP5":  dict(bot5),
        "过滤统计快照":  _signals_mod.scalp_filter_stats or "（5分钟后刷新）",
    }
    return JSONResponse(report)


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
