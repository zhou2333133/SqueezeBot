import asyncio

# 全局市场数据中心
hub_task: asyncio.Task | None = None

# 超短线机器人全局状态（run.py 初始化，web.py 读写）
scalp_task: asyncio.Task | None = None
scalp_bot = None  # BinanceScalpBot 实例

# 妖币扫描器全局状态
yaobi_task: asyncio.Task | None = None
yaobi_scanner = None  # YaobiScanner 实例

# 一直做空机器人全局状态
fade_task: asyncio.Task | None = None
fade_bot = None  # BinanceFadeBot 实例
