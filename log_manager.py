"""
日志路由系统
- 控制台输出
- 主内存队列 (全部日志)
- 模式专属队列: swing_log_queue / scalp_log_queue
- 按天轮转文件日志 (7 天保留)
"""
import logging
import logging.handlers
import os
import queue

log_queue:       queue.Queue = queue.Queue(maxsize=1000)
swing_log_queue: queue.Queue = queue.Queue(maxsize=500)
scalp_log_queue: queue.Queue = queue.Queue(maxsize=500)
fade_log_queue:  queue.Queue = queue.Queue(maxsize=500)

_LOG_FORMAT      = "[%(asctime)s] [%(levelname)-8s] %(message)s"
_FILE_LOG_FORMAT = "[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s"
_DATE_FORMAT     = "%H:%M:%S"


def _put(q: queue.Queue, msg: str) -> None:
    if q.full():
        try:
            q.get_nowait()
        except queue.Empty:
            pass
    try:
        q.put_nowait(msg)
    except queue.Full:
        pass


class _RoutingQueueHandler(logging.Handler):
    """路由日志 → 主队列 + 模式专属队列"""
    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            _put(log_queue, msg)
            name = record.name.lower()
            if "scalp" in name:
                _put(scalp_log_queue, msg)
            elif "fade" in name:
                _put(fade_log_queue, msg)
            else:
                _put(swing_log_queue, msg)
        except Exception:
            self.handleError(record)


def setup_logging(level: int = logging.INFO, log_dir: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in root.handlers[:]:
        root.removeHandler(h)

    fmt = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    mem = _RoutingQueueHandler()
    mem.setFormatter(fmt)
    root.addHandler(mem)

    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(
            os.path.join(log_dir, "squeeze_bot.log"),
            when="midnight", backupCount=7, encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(_FILE_LOG_FORMAT))
        root.addHandler(fh)
