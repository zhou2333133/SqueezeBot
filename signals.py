import json
import os
import queue as std_queue
import time

from config import DATA_DIR

# ── 超短线信号 ────────────────────────────────────────────────────────────────
scalp_signal_queue:    std_queue.Queue = std_queue.Queue(maxsize=200)
scalp_signals_history: list[dict]      = []

# ── 超短线活跃仓位（共享状态，web.py 读取）────────────────────────────────────
scalp_positions: dict[str, dict] = {}

# ── 超短线历史成交记录 ────────────────────────────────────────────────────────
scalp_trade_history: list[dict] = []

# ── 超短线入场拦截日志（持久化，复盘包里用于回溯"为什么没开"）─────────────────
scalp_entry_block_log: list[dict] = []

# ── 策略标签成交明细（独立 JSONL，用于策略复盘统计）────────────────────────
strategy_trade_queue: std_queue.Queue = std_queue.Queue(maxsize=200)

MAX_SIGNALS = 200
MAX_TRADES  = 500
MAX_ENTRY_BLOCKS = 240

# ── 超短线过滤统计（实时共享，web.py 读取）────────────────────────────────────
scalp_filter_stats: dict = {}

LEDGER_FILE = os.path.join(DATA_DIR, "runtime_ledger.json")


def _load_ledger() -> None:
    if not os.path.exists(LEDGER_FILE):
        return
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    scalp_positions.update(data.get("scalp_positions") or {})
    scalp_trade_history.extend((data.get("scalp_trade_history") or [])[-MAX_TRADES:])
    scalp_entry_block_log.extend((data.get("scalp_entry_block_log") or [])[-MAX_ENTRY_BLOCKS:])


def _persist_ledger() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "scalp_positions": scalp_positions,
            "scalp_trade_history": scalp_trade_history[-MAX_TRADES:],
            "scalp_entry_block_log": scalp_entry_block_log[-MAX_ENTRY_BLOCKS:],
        }
        tmp = LEDGER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, LEDGER_FILE)
    except Exception:
        pass


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
    _persist_ledger()


def add_scalp_trade(trade: dict) -> None:
    scalp_trade_history.append(trade)
    if len(scalp_trade_history) > MAX_TRADES:
        scalp_trade_history.pop(0)
    _persist_ledger()


def add_scalp_entry_block(block: dict) -> None:
    scalp_entry_block_log.append(block)
    if len(scalp_entry_block_log) > MAX_ENTRY_BLOCKS:
        scalp_entry_block_log.pop(0)
    _persist_ledger()


def push_strategy_trade(trade: dict) -> None:
    """推送一笔带 strategy_tag 的成交到队列，供 strategy_stats 消费。"""
    _push(strategy_trade_queue, json.dumps(trade, ensure_ascii=False, default=str))


_load_ledger()
