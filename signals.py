import json
import queue as std_queue

# ── 中线策略信号 ──────────────────────────────────────────────────────────────
signal_queue:    std_queue.Queue = std_queue.Queue(maxsize=200)
signals_history: list[dict]      = []

# ── 超短线信号 ────────────────────────────────────────────────────────────────
scalp_signal_queue:    std_queue.Queue = std_queue.Queue(maxsize=200)
scalp_signals_history: list[dict]      = []

# ── 超短线活跃仓位（共享状态，web.py 读取）────────────────────────────────────
scalp_positions: dict[str, dict] = {}

MAX_SIGNALS = 200


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


def add_signal(signal: dict) -> None:
    signals_history.append(signal)
    if len(signals_history) > MAX_SIGNALS:
        signals_history.pop(0)
    try:
        _push(signal_queue, json.dumps(signal, ensure_ascii=False, default=str))
    except Exception:
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
