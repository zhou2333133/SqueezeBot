import asyncio
import json
import unittest

from config import config_manager
from signals import scalp_trade_history
from web import get_scalp_report


class TestScalpReport(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_settings = config_manager.settings.copy()
        self._orig_trades = list(scalp_trade_history)
        scalp_trade_history.clear()
        config_manager.settings.update({
            "SCALP_LEVERAGE": 10,
            "SCALP_STOP_LOSS_PCT": 15.0,
            "SCALP_RISK_PER_TRADE_USDT": 5.0,
            "SCALP_MAX_DAILY_LOSS_USDT": 50.0,
            "SCALP_TP1_RR": 1.5,
            "SCALP_TP2_RR": 3.5,
            "SCALP_TP1_RATIO": 0.5,
            "SCALP_TP2_RATIO": 0.3,
            "SCALP_TP3_TRAIL_PCT": 1.5,
            "SQUEEZE_OI_DROP_MAJOR": 0.5,
            "SQUEEZE_OI_DROP_MID": 1.0,
            "SQUEEZE_OI_DROP_MEME": 1.5,
            "SQUEEZE_WICK_PCT": 1.0,
            "SQUEEZE_TAKER_MIN": 0.65,
            "BREAKOUT_TAKER_MIN": 0.55,
            "SIGNAL_COOLDOWN_SECONDS": 5,
            "OI_POLL_INTERVAL": 10,
            "BTC_GUARD_PCT": 2.0,
        })
        scalp_trade_history.extend([
            {
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "signal_label": "轧空猎杀多",
                "pnl_usdt": 12.5,
                "entry_time": "2026-04-20 10:00:00",
                "close_reason": "TP2",
            },
            {
                "symbol": "ETHUSDT",
                "direction": "SHORT",
                "signal_label": "动能突破空",
                "pnl_usdt": -4.2,
                "entry_time": "2026-04-20 11:00:00",
                "close_reason": "SL",
            },
        ])

    def tearDown(self) -> None:
        scalp_trade_history.clear()
        scalp_trade_history.extend(self._orig_trades)
        config_manager.settings.clear()
        config_manager.settings.update(self._orig_settings)

    def test_report_snapshot_uses_v3_fields(self) -> None:
        response = asyncio.run(get_scalp_report())
        payload = json.loads(response.body.decode("utf-8"))
        snapshot = payload["策略参数快照"]

        expected_keys = {
            "杠杆",
            "最大SL%保证金",
            "每笔风险USDT",
            "每日熔断USDT",
            "TP1_RR",
            "TP2_RR",
            "TP1平仓比例",
            "TP2平仓比例",
            "TP3追踪%",
            "OI轮询秒",
            "信号冷却秒",
            "BTC守卫%",
            "轧空OI阈值-大币%",
            "轧空OI阈值-中币%",
            "轧空OI阈值-Meme%",
            "挤压确认反弹%",
            "挤压Taker阈值",
            "突破Taker阈值",
        }
        self.assertTrue(expected_keys.issubset(snapshot.keys()))
        self.assertNotIn("活跃振幅%", snapshot)
        self.assertNotIn("回调阈值%", snapshot)
        self.assertNotIn("反转阈值%", snapshot)


if __name__ == "__main__":
    unittest.main()
