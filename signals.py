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
