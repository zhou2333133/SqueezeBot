import asyncio
import uvicorn
from bot import BinanceSqueezeBot
from log_manager import setup_logging


async def main():
    # 0. 初始化日志系统
    setup_logging()
    print("⏳ 正在初始化 Squeeze Bot 与 Web 控制台...")
    
    # 1. 在主进程中启动机器人核心扫描任务
    bot_task = BinanceSqueezeBot()
    asyncio.create_task(bot_task.run())

    # 2. 启动主控制台 Web Server
    config = uvicorn.Config("web:app", host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())