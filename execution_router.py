"""
Execution Router — 执行器统一路由。

职责：
  接收 order_intent → risk_guard 检查 → 路由到 PAPER/LIVE 执行器
  PAPER 和 LIVE 的唯一区别在此决定

当前实现：
  作为轻量路由层，实际执行委托给 bot_scalp.py 现有的 _exec_* 方法。
  后续可完整提取为独立执行器。
"""
import logging

logger = logging.getLogger(__name__)


def route_order(order_intent: dict, executor_ref) -> dict:
    """路由一笔 order_intent 到对应执行器。

    order_intent 结构:
      symbol, direction, side, exit_s, entry_price, sl_price,
      quantity, leverage, pos_side, is_paper, ...

    executor_ref: 持有 _exec_open/_exec_close_qty 等方法的对象
    """
    action = order_intent.get("action", "open")
    if action == "open":
        return _route_open(order_intent, executor_ref)
    elif action in ("close", "close_qty"):
        return _route_close(order_intent, executor_ref)
    else:
        return {"success": False, "error": f"unknown_action:{action}"}


def _route_open(intent: dict, ex) -> dict:
    """路由开仓。"""
    from risk_guard import check_open_order
    result = check_open_order(
        symbol=intent.get("symbol", ""),
        direction=intent.get("direction", ""),
        amount_usdt=intent.get("amount_usdt", 0),
        leverage=intent.get("leverage", 1),
        current_positions=intent.get("current_positions", 0),
        daily_loss_usdt=intent.get("daily_loss_usdt", 0),
    )
    if not result.get("allow", False):
        return {"success": False, "rejected_reason": result.get("reason", "risk_guard")}
    # Route to executor
    return _call_executor(ex, intent)


def _route_close(intent: dict, ex) -> dict:
    """路由平仓。"""
    return _call_executor(ex, intent)


def _call_executor(ex, intent: dict) -> dict:
    """调用实际执行器。
    这里直接委托给 bot_scalp 的 _exec_* 方法。
    """
    action = intent.get("action", "open")
    if action == "open" and hasattr(ex, "_exec_open"):
        return ex._exec_open(**intent.get("_exec_kwargs", {}))
    if "close_qty" in action and hasattr(ex, "_exec_close_qty"):
        return ex._exec_close_qty(**intent.get("_exec_kwargs", {}))
    return {"success": False, "error": f"cannot_route:{action}"}
