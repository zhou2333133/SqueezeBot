import json
import queue as std_queue

# ── 超短线信号 ────────────────────────────────────────────────────────────────
scalp_signal_queue:    std_queue.Queue = std_queue.Queue(maxsize=200)
scalp_signals_history: list[dict]      = []

# ── 超短线活跃仓位（共享状态，web.py 读取）────────────────────────────────────
scalp_positions: dict[str, dict] = {}

# ── 超短线历史成交记录 ────────────────────────────────────────────────────────
scalp_trade_history: list[dict] = []

MAX_SIGNALS = 200
MAX_TRADES  = 500

# ── 超短线过滤统计（实时共享，web.py 读取）────────────────────────────────────
scalp_filter_stats: dict = {}

# ── V4AF 闪崩做空模块 ────────────────────────────────────────────────────────
flash_signal_queue:   std_queue.Queue = std_queue.Queue(maxsize=200)
flash_signals_history: list[dict]    = []
flash_positions: dict[str, dict]     = {}
flash_trade_history: list[dict]      = []
flash_filter_stats: dict             = {}
flash_review_log: list[dict]         = []   # 智能时间止损每次重评估的决策记录

MAX_FLASH_SIGNALS = 200
MAX_FLASH_TRADES  = 500
MAX_FLASH_REVIEWS = 500


def _push(q: std_queue.Queue, msg: str) -> None:
    if q.full():
        try:
            q.get_nowait()
        except std_queue.Empty:
            pass
    try:
        q.put_nowait(msg)
    except std_queue.Full:
        pass


def add_scalp_signal(signal: dict) -> None:
    scalp_signals_history.append(signal)
    if len(scalp_signals_history) > MAX_SIGNALS:
        scalp_signals_history.pop(0)
    try:
        _push(scalp_signal_queue, json.dumps(signal, ensure_ascii=False, default=str))
    except Exception:
        pass


def set_scalp_position(symbol: str, pos: dict | None) -> None:
    if pos is None:
        scalp_positions.pop(symbol, None)
    else:
        scalp_positions[symbol] = pos


def add_scalp_trade(trade: dict) -> None:
    scalp_trade_history.append(trade)
    if len(scalp_trade_history) > MAX_TRADES:
        scalp_trade_history.pop(0)


# ── V4AF 写入接口 ─────────────────────────────────────────────────────────────
def add_flash_signal(signal: dict) -> None:
    flash_signals_history.append(signal)
    if len(flash_signals_history) > MAX_FLASH_SIGNALS:
        flash_signals_history.pop(0)
    try:
        _push(flash_signal_queue, json.dumps(signal, ensure_ascii=False, default=str))
    except Exception:
        pass


def set_flash_position(symbol: str, pos: dict | None) -> None:
    if pos is None:
        flash_positions.pop(symbol, None)
    else:
        flash_positions[symbol] = pos


def add_flash_trade(trade: dict) -> None:
    flash_trade_history.append(trade)
    if len(flash_trade_history) > MAX_FLASH_TRADES:
        flash_trade_history.pop(0)


def add_flash_review(review: dict) -> None:
    flash_review_log.append(review)
    if len(flash_review_log) > MAX_FLASH_REVIEWS:
        flash_review_log.pop(0)
