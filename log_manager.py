import logging
import asyncio
from logging.handlers import QueueHandler, QueueListener

# 创建一个全局的异步队列来存储日志记录
log_queue = asyncio.Queue()

def setup_logging():
    """
    配置日志系统，将日志输出到队列中。
    """
    # 获取根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 移除所有现有的 handlers，以避免重复输出
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 创建一个 handler，将日志记录放入我们的异步队列
    queue_handler = QueueHandler(log_queue)
    
    # 将 handler 添加到根 logger
    root_logger.addHandler(queue_handler)