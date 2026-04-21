import asyncio
import logging

import uvicorn

import bot_state
from config import config_manager, LOGS_DIR, PANEL_HOST, PANEL_PORT, PANEL_TOKEN
from log_manager import setup_logging
from market_hub import hub

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging(log_dir=LOGS_DIR)
    logger.info("⏳ 正在启动 SqueezeBot...")

    # 启动全局市场数据中心 (所有机器人共享)
    bot_state.hub_task = asyncio.create_task(hub.run())
    logger.info("📡 MarketHub 已作为后台任务启动")

    # 若配置已启用超短线，自动拉起
    if config_manager.settings.get("SCALP_ENABLED", False):
        from bot_scalp import BinanceScalpBot
        bot_state.scalp_bot  = BinanceScalpBot()
        bot_state.scalp_task = asyncio.create_task(bot_state.scalp_bot.run())
        logger.info("⚡ 超短线机器人随主程序自动启动")

    # 若配置已启用妖币扫描器，自动拉起
    if config_manager.settings.get("YAOBI_ENABLED", False):
        from scanner.yaobi_scanner import YaobiScanner
        bot_state.yaobi_scanner = YaobiScanner()
        bot_state.yaobi_task    = asyncio.create_task(bot_state.yaobi_scanner.run())
        logger.info("🔍 妖币扫描器随主程序自动启动")

    uvi_config = uvicorn.Config(
        "web:app",
        host=PANEL_HOST,
        port=PANEL_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvi_config)

    auth_hint = "Token已启用" if PANEL_TOKEN else "本机模式"
    logger.info("🌐 Web 控制台: http://%s:%s (%s)", PANEL_HOST, PANEL_PORT, auth_hint)
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 收到 Ctrl+C，已安全停止。")
