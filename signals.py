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

# ── 超短线历史成交记录 ────────────────────────────────────────────────────────
scalp_trade_history: list[dict] = []

# ── 一直做空信号 ──────────────────────────────────────────────────────────────
fade_signal_queue:    std_queue.Queue = std_queue.Queue(maxsize=200)
fade_signals_history: list[dict]      = []

# ── 一直做空活跃仓位 ──────────────────────────────────────────────────────────
fade_positions: dict[str, dict] = {}

# ── 一直做空历史成交记录 ──────────────────────────────────────────────────────
fade_trade_history: list[dict] = []

MAX_SIGNALS = 200
MAX_TRADES  = 500

# ── 超短线过滤统计（实时共享，web.py 读取）────────────────────────────────────
scalp_filter_stats: dict = {}

# ── 中线模拟仓位 ──────────────────────────────────────────────────────────────
swing_paper_positions: dict[str, dict] = {}
swing_paper_trade_history: list[dict]  = []


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


def add_fade_signal(signal: dict) -> None:
    fade_signals_history.append(signal)
    if len(fade_signals_history) > MAX_SIGNALS:
        fade_signals_history.pop(0)
    try:
        _push(fade_signal_queue, json.dumps(signal, ensure_ascii=False, default=str))
    except Exception:
        pass


def set_fade_position(symbol: str, pos: dict | None) -> None:
    if pos is None:
        fade_positions.pop(symbol, None)
    else:
        fade_positions[symbol] = pos


def add_scalp_trade(trade: dict) -> None:
    scalp_trade_history.append(trade)
    if len(scalp_trade_history) > MAX_TRADES:
        scalp_trade_history.pop(0)


def add_fade_trade(trade: dict) -> None:
    fade_trade_history.append(trade)
    if len(fade_trade_history) > MAX_TRADES:
        fade_trade_history.pop(0)


def set_swing_paper_position(symbol: str, pos: dict | None) -> None:
    if pos is None:
        swing_paper_positions.pop(symbol, None)
    else:
        swing_paper_positions[symbol] = pos


def add_swing_paper_trade(trade: dict) -> None:
    swing_paper_trade_history.append(trade)
    if len(swing_paper_trade_history) > MAX_TRADES:
        swing_paper_trade_history.pop(0)
