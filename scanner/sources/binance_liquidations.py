"""
Binance public liquidation stream cache.

The stream is used as a short-term heat signal for the opportunity queue. It is
not a complete liquidation database; Binance pushes snapshots, so the numbers
are best treated as activity/intensity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)

_WS_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
_EVENTS: deque[dict] = deque(maxlen=5000)


def record_liquidation_event(payload: dict) -> None:
    order = payload.get("o", payload)
    symbol = str(order.get("s", "")).upper()
    if not symbol:
        return
    price = _as_float(order.get("ap") or order.get("p"))
    qty = _as_float(order.get("q"))
    notional = price * qty if price > 0 and qty > 0 else _as_float(order.get("notional"))
    _EVENTS.append({
        "ts": _as_float(payload.get("E") or order.get("T") or time.time() * 1000) / 1000,
        "symbol": symbol,
        "side": str(order.get("S", "")).upper(),
        "price": price,
        "qty": qty,
        "notional": notional,
    })


def liquidation_stats(symbols: list[str] | set[str], windows_min: tuple[int, ...] = (5, 15)) -> dict[str, dict]:
    now = time.time()
    wanted = {str(s).upper() for s in symbols}
    stats = {
        sym: {
            f"liquidation_{w}m_usd": 0.0
            for w in windows_min
        } | {
            f"liquidation_{w}m_count": 0
            for w in windows_min
        }
        for sym in wanted
    }
    for event in list(_EVENTS):
        sym = event.get("symbol", "")
        if sym not in wanted:
            continue
        age_min = (now - float(event.get("ts", 0) or 0)) / 60
        for window in windows_min:
            if age_min <= window:
                stats[sym][f"liquidation_{window}m_usd"] += float(event.get("notional", 0) or 0)
                stats[sym][f"liquidation_{window}m_count"] += 1
    for row in stats.values():
        for key, value in list(row.items()):
            if key.endswith("_usd"):
                row[key] = round(value, 2)
    return stats


async def collect_liquidations(is_running: Callable[[], bool]) -> None:
    while is_running():
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.ws_connect(_WS_URL, heartbeat=20) as ws:
                    logger.info("🔍 Binance 强平流已连接")
                    async for msg in ws:
                        if not is_running():
                            return
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if isinstance(data, list):
                                for item in data:
                                    if isinstance(item, dict):
                                        record_liquidation_event(item)
                            elif isinstance(data, dict):
                                record_liquidation_event(data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if is_running():
                logger.debug("Binance 强平流异常: %s", e)
                await asyncio.sleep(10)


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
