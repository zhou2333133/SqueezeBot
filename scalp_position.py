"""
超短线持仓数据模型
"""
import time
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ScalpPosition:
    symbol:             str
    direction:          str
    entry_price:        float
    quantity:           float
    quantity_remaining: float
    sl_price:           float
    tp1_price:          float
    tp2_price:          float
    tp1_hit:            bool       = False
    tp2_hit:            bool       = False
    trail_ref_price:    float      = 0.0
    signal_label:       str        = ""
    market_state:       str        = "UNKNOWN"
    tp1_ratio:          float      = 0.40
    tp2_ratio:          float      = 0.30
    trail_pct:          float      = 5.0
    structure_trail_bars: int      = 5
    time_stop_minutes:  float      = 30.0
    tp2_timeout_minutes: float     = 120.0
    paper:              bool       = False
    entry_time:         str        = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    entry_ts:           float      = field(default_factory=time.monotonic)
    sl_order_id:        int | None = None
    risk_usdt:          float      = 0.0
    realized_gross_pnl: float      = 0.0
    realized_pnl:       float      = 0.0
    fee_usdt:           float      = 0.0
    slippage_usdt:      float      = 0.0
    closed_quantity:    float      = 0.0
    current_price:      float      = 0.0
    last_price_ts:      float      = field(default_factory=time.monotonic)
    stale_position_check_used: bool = False
    max_favorable_pct:  float      = 0.0
    max_adverse_pct:    float      = 0.0
    max_favorable_time: str        = ""
    max_adverse_time:   str        = ""
    entry_context:      dict       = field(default_factory=dict)
    protection_failed:  bool       = False
    protection_reason:  str        = ""
    tp1_pending_hits:   int        = 0
    tp2_pending_hits:   int        = 0
    next_force_exit_at: float      = 0.0
    force_exit_attempts: int       = 0

    @classmethod
    def from_dict(cls, data: dict) -> "ScalpPosition":
        pos = cls(
            symbol=str(data.get("symbol", "")).upper(),
            direction=str(data.get("direction", "LONG")).upper(),
            entry_price=float(data.get("entry_price", 0.0) or 0.0),
            quantity=float(data.get("quantity", 0.0) or 0.0),
            quantity_remaining=float(data.get("quantity_remaining", data.get("quantity", 0.0)) or 0.0),
            sl_price=float(data.get("sl_price", 0.0) or 0.0),
            tp1_price=float(data.get("tp1_price", 0.0) or 0.0),
            tp2_price=float(data.get("tp2_price", 0.0) or 0.0),
            tp1_hit=bool(data.get("tp1_hit", False)),
            tp2_hit=bool(data.get("tp2_hit", False)),
            signal_label=str(data.get("signal_label", "")),
            market_state=str(data.get("market_state", "UNKNOWN")),
            tp1_ratio=float(data.get("tp1_ratio", 0.40) or 0.40),
            tp2_ratio=float(data.get("tp2_ratio", 0.30) or 0.30),
            risk_usdt=float(data.get("risk_usdt", 0.0) or 0.0),
            paper=bool(data.get("paper", False)),
            entry_time=str(data.get("entry_time", "")) or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            entry_context=dict(data.get("entry_context") or {}),
            protection_failed=bool(data.get("protection_failed", False)),
            protection_reason=str(data.get("protection_reason", "")),
        )
        pos.current_price = float(data.get("current_price", 0.0) or 0.0)
        pos.realized_pnl = float(data.get("realized_pnl", 0.0) or 0.0)
        pos.realized_gross_pnl = float(data.get("realized_gross_pnl", 0.0) or 0.0)
        pos.fee_usdt = float(data.get("fee_usdt", 0.0) or 0.0)
        pos.slippage_usdt = float(data.get("slippage_usdt", 0.0) or 0.0)
        pos.max_favorable_pct = float(data.get("mfe_pct", 0.0) or 0.0)
        pos.max_adverse_pct = float(data.get("mae_pct", 0.0) or 0.0)
        return pos

    def to_dict(self) -> dict:
        unreal = 0.0
        if self.current_price and self.entry_price:
            if self.direction == "LONG":
                unreal = (self.current_price - self.entry_price) / self.entry_price * 100
            else:
                unreal = (self.entry_price - self.current_price) / self.entry_price * 100
        return {
            "symbol":             self.symbol,
            "direction":          self.direction,
            "entry_price":        round(self.entry_price, 8),
            "quantity":           round(self.quantity, 6),
            "quantity_remaining": round(self.quantity_remaining, 6),
            "sl_price":           round(self.sl_price, 8),
            "tp1_price":          round(self.tp1_price, 8),
            "tp2_price":          round(self.tp2_price, 8),
            "tp1_hit":            self.tp1_hit,
            "tp2_hit":            self.tp2_hit,
            "signal_label":       self.signal_label,
            "market_state":       self.market_state,
            "tp1_ratio":          self.tp1_ratio,
            "tp2_ratio":          self.tp2_ratio,
            "risk_usdt":          round(self.risk_usdt, 4),
            "entry_context":      self.entry_context,
            "protection_failed":  self.protection_failed,
            "protection_reason":  self.protection_reason,
            "paper":              self.paper,
            "entry_time":         self.entry_time,
            "realized_pnl":       round(self.realized_pnl, 4),
            "realized_gross_pnl": round(self.realized_gross_pnl, 4),
            "fee_usdt":           round(self.fee_usdt, 4),
            "slippage_usdt":      round(self.slippage_usdt, 4),
            "mfe_pct":            round(self.max_favorable_pct, 3),
            "mae_pct":            round(self.max_adverse_pct, 3),
            "unrealized_pct":     round(unreal, 2),
            "current_price":      round(self.current_price, 8),
            "time_stop_minutes":  round(self.time_stop_minutes, 2),
            "tp2_timeout_minutes": round(self.tp2_timeout_minutes, 2),
            "position_age_minutes": round((time.monotonic() - self.entry_ts) / 60, 2),
            "position_last_price_age_seconds": round(max(0.0, time.monotonic() - self.last_price_ts), 2),
            "stale_position_check_used": self.stale_position_check_used,
        }
