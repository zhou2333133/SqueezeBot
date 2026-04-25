import asyncio

# 全局市场数据中心
hub_task: asyncio.Task | None = None

# 超短线机器人全局状态（run.py 初始化，web.py 读写）
scalp_task: asyncio.Task | None = None
scalp_bot = None  # BinanceScalpBot 实例

# 妖币扫描器全局状态
yaobi_task: asyncio.Task | None = None
yaobi_scanner = None  # YaobiScanner 实例

# V4AF 闪崩做空模块全局状态
flash_task: asyncio.Task | None = None
flash_bot = None  # FlashCrashBot 实例
