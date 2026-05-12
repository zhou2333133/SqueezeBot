"""
Execution Router — 执行器统一路由。

职责：
  接收 order_intent → risk_guard 检查 → 路由到 PAPER/LIVE 执行器
  PAPER 和 LIVE 的唯一区别在此决定

返回结构（统一）：
  success: bool
  mode: "PAPER" | "LIVE"
  symbol: str
  side: str
  qty: float
  price: float
  order_id: int | None
  sl_order_id: int | None
  protection_failed: bool
  protection_reason: str
  error: str
  raw_response: dict | None
"""
import logging

logger = logging.getLogger(__name__)


async def execute(order_intent: dict, trader=None, cfg: dict | None = None) -> dict:
    """统一执行入口。

    order_intent 字段：
      action: "open"
      symbol, direction, side, exit_s: str
      entry_price, sl_price, quantity: float
      leverage: int
      pos_side: str
      is_paper: bool
      intended_risk_usdt: float  (optional)

    返回统一结果 dict。
    """
    symbol = order_intent.get("symbol", "")
    side = order_intent.get("side", "")
    direction = order_intent.get("direction", "")
    exit_s = order_intent.get("exit_s", "")
    entry_price = float(order_intent.get("entry_price", 0))
    sl_price = float(order_intent.get("sl_price", 0))
    quantity = float(order_intent.get("quantity", 0))
    leverage = int(order_intent.get("leverage", 10))
    pos_side = order_intent.get("pos_side", "")
    is_paper = bool(order_intent.get("is_paper", False))

    # ── PAPER 路径：虚拟成交 ──────────────────────────────────────────────
    if is_paper:
        return {
            "success": True,
            "mode": "PAPER",
            "symbol": symbol,
            "side": side,
            "qty": quantity,
            "price": entry_price,
            "order_id": None,
            "sl_order_id": None,
            "protection_failed": False,
            "protection_reason": "",
            "error": "",
            "raw_response": None,
        }

    # ── LIVE 路径 ─────────────────────────────────────────────────────────
    if not trader:
        return _error_result("LIVE", symbol, side, "trader_uninitialized")

    hedge_mode = await trader.is_hedge_mode()
    if hedge_mode is None:
        return _error_result("LIVE", symbol, side, "cannot_check_position_mode")
    if hedge_mode:
        logger.info("⚡ [%s] Hedge Mode → positionSide=%s", symbol, pos_side)

    # 杠杆
    lev_resp = await trader.set_leverage(symbol, leverage)
    if lev_resp is None and leverage > 5:
        logger.warning("⚡ [%s] 杠杆%.0fx失败，降级到5x", symbol, leverage)
        lev_resp = await trader.set_leverage(symbol, 5)
        if lev_resp and "leverage" in lev_resp:
            leverage = 5
    if not lev_resp or "leverage" not in lev_resp:
        return _error_result("LIVE", symbol, side, "leverage_set_failed")

    # IOC 市价单
    ioc_price = entry_price * 1.003 if direction == "LONG" else entry_price * 0.997
    trade_resp = await trader.place_limit_ioc_order(symbol, side, quantity, ioc_price, pos_side)
    if not trade_resp:
        return _error_result("LIVE", symbol, side, "ioc_order_failed")
    filled_qty = float(trade_resp.get("executedQty", 0))
    if filled_qty <= 0:
        return {
            "success": False,
            "mode": "LIVE",
            "symbol": symbol,
            "side": side,
            "qty": 0,
            "price": 0,
            "order_id": None,
            "sl_order_id": None,
            "protection_failed": False,
            "protection_reason": "",
            "error": "ioc_no_fill",
            "raw_response": trade_resp,
        }
    actual_entry = float(trade_resp.get("avgPrice") or entry_price)

    # 止损单
    sl_resp = await trader.place_stop_loss_order(symbol, exit_s, sl_price, filled_qty, pos_side)
    sl_order_id = sl_resp.get("orderId") if sl_resp else None
    protection_failed = False
    protection_reason = ""
    if not sl_order_id:
        logger.critical("⚡ [%s] 止损单挂单失败，立即 reduceOnly 市价撤出", symbol)
        emergency = await trader.place_reduce_only_market_order(symbol, exit_s, filled_qty, pos_side)
        if emergency:
            return {
                "success": False,
                "mode": "LIVE",
                "symbol": symbol,
                "side": side,
                "qty": filled_qty,
                "price": actual_entry,
                "order_id": trade_resp.get("orderId"),
                "sl_order_id": None,
                "protection_failed": True,
                "protection_reason": "stop_loss_order_failed_emergency_closed",
                "error": "stop_loss_order_failed_emergency_closed",
                "raw_response": emergency,
            }
        protection_failed = True
        protection_reason = "stop_loss_order_failed_emergency_failed"
        logger.critical("⚡ [%s] 紧急撤出失败，请人工检查交易所持仓", symbol)

    return {
        "success": True,
        "mode": "LIVE",
        "symbol": symbol,
        "side": side,
        "qty": filled_qty,
        "price": actual_entry,
        "order_id": trade_resp.get("orderId"),
        "sl_order_id": sl_order_id,
        "protection_failed": protection_failed,
        "protection_reason": protection_reason,
        "error": "",
        "raw_response": trade_resp,
    }


def _error_result(mode: str, symbol: str, side: str, error: str) -> dict:
    return {
        "success": False,
        "mode": mode,
        "symbol": symbol,
        "side": side,
        "qty": 0,
        "price": 0,
        "order_id": None,
        "sl_order_id": None,
        "protection_failed": False,
        "protection_reason": "",
        "error": error,
        "raw_response": None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 以下为兼容层 — 逐步迁移到 execute()
# ══════════════════════════════════════════════════════════════════════════════

def route_order(order_intent: dict, executor_ref) -> dict:
    """（兼容）路由一笔 order_intent 到对应执行器。"""
    action = order_intent.get("action", "open")
    if action == "open":
        return _route_open(order_intent, executor_ref)
    elif action in ("close", "close_qty"):
        return _route_close(order_intent, executor_ref)
    else:
        return {"success": False, "error": f"unknown_action:{action}"}


def _route_open(intent: dict, ex) -> dict:
    """（兼容）路由开仓。"""
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
    return _call_executor(ex, intent)


def _route_close(intent: dict, ex) -> dict:
    """（兼容）路由平仓。"""
    return _call_executor(ex, intent)


def _call_executor(ex, intent: dict) -> dict:
    """（兼容）调用实际执行器。"""
    action = intent.get("action", "open")
    if action == "open" and hasattr(ex, "_exec_open"):
        return ex._exec_open(**intent.get("_exec_kwargs", {}))
    if "close_qty" in action and hasattr(ex, "_exec_close_qty"):
        return ex._exec_close_qty(**intent.get("_exec_kwargs", {}))
    return {"success": False, "error": f"cannot_route:{action}"}


def verify_execution(exec_result, symbol: str, direction: str, quantity: float, cfg: dict) -> dict:
    """验证执行结果。主要检查 PAPER/LIVE 边界是否一致。"""
    result = {"verified": True, "warnings": []}
    if not exec_result or not getattr(exec_result, "success", False):
        result["verified"] = False
        return result
    is_paper = getattr(exec_result, "is_paper", False) if hasattr(exec_result, "is_paper") else False
    if is_paper and not cfg.get("SCALP_PAPER_TRADE", False):
        result["warnings"].append(f"{symbol}: PAPER mode mismatch")
    elif not is_paper and cfg.get("SCALP_PAPER_TRADE", True):
        result["warnings"].append(f"{symbol}: LIVE mode mismatch")
    return result
