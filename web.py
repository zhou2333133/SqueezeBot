from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import aiohttp
import os
import logging

from config import config_manager
from trader import BinanceTrader
from log_manager import log_queue

app = FastAPI(title="Squeeze Bot Control Panel")

# 确保 templates 文件夹存在
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """渲染主控制台"""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "config": config_manager.settings
    })

@app.post("/api/config")
async def update_config(request: Request):
    """更新配置文件"""
    form_data = await request.form()
    config_manager.save(dict(form_data))
    return JSONResponse({"status": "success", "message": "参数已保存生效！"})

@app.post("/api/trade")
async def manual_trade(symbol: str = Form(...), side: str = Form(...), quantity: float = Form(...)):
    """手动市价开平仓"""
    async with aiohttp.ClientSession() as session:
        trader = BinanceTrader(session)
        res = await trader.place_market_order(symbol.upper(), side.upper(), quantity)
        if res:
            return JSONResponse({"status": "success", "message": f"{side} {symbol} 下单成功，详见币安APP。"})
        return JSONResponse({"status": "error", "message": "下单失败，请检查 API Key 权限或余额！"}, status_code=400)

@app.websocket("/ws/log")
async def websocket_log_endpoint(websocket: WebSocket):
    """
    WebSocket 端点，用于向客户端流式传输日志。
    """
    await websocket.accept()
    try:
        while True:
            log_record: logging.LogRecord = await log_queue.get()
            await websocket.send_text(f"[{log_record.levelname}] {log_record.getMessage()}")
    except WebSocketDisconnect:
        print("日志 WebSocket 连接已断开")