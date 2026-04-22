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
            "SCALP_MAX_DAILY_LOSS_R": 10.0,
            "SCALP_TP1_RR": 1.5,
            "SCALP_TP2_RR": 3.5,
            "SCALP_TP1_RATIO": 0.5,
            "SCALP_TP2_RATIO": 0.3,
            "SCALP_TP3_TRAIL_PCT": 1.5,
            "SCALP_STRUCTURE_TRAIL_BARS": 8,
            "SCALP_TIME_STOP_MINUTES": 30,
            "SCALP_TP2_TIMEOUT_MINUTES": 120,
            "FEE_RATE_PER_SIDE": 0.0004,
            "SLIPPAGE_RATE_PER_SIDE": 0.0005,
            "SQUEEZE_OI_DROP_MAJOR": 0.5,
            "SQUEEZE_OI_DROP_MID": 1.0,
            "SQUEEZE_OI_DROP_MEME": 1.5,
            "SQUEEZE_WICK_PCT": 1.0,
            "SQUEEZE_TAKER_MIN": 0.65,
            "BREAKOUT_TAKER_MIN": 0.55,
            "BREAKOUT_MIN_PCT": 0.10,
            "BREAKOUT_ATR_MULT": 0.7,
            "BREAKOUT_ATR_MIN_PCT": 0.50,
            "BREAKOUT_ATR_MAX_PCT": 1.20,
            "BREAKOUT_MIN_VOL_RATIO": 0.50,
            "SIGNAL_COOLDOWN_SECONDS": 30,
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
            "每日熔断R",
            "手续费率单边",
            "滑点率单边",
            "TP1_RR",
            "TP2_RR",
            "TP1平仓比例",
            "TP2平仓比例",
            "TP3追踪%",
            "结构追踪K数",
            "TP1时间止损分钟",
            "TP2超时分钟",
            "OI轮询秒",
            "信号冷却秒",
            "BTC守卫%",
            "轧空OI阈值-大币%",
            "轧空OI阈值-中币%",
            "轧空OI阈值-Meme%",
            "挤压确认反弹%",
            "挤压Taker阈值",
            "突破Taker阈值",
            "突破最小幅度%",
            "突破ATR倍数",
            "突破ATR最小%",
            "突破ATR最大%",
            "当前K量比阈值",
            "Surf后台新闻",
            "Surf新闻间隔分钟",
            "Surf新闻TopN",
            "Surf入场AI",
            "Surf入场AI涨跌阈值%",
        }
        self.assertTrue(expected_keys.issubset(snapshot.keys()))
        self.assertNotIn("活跃振幅%", snapshot)
        self.assertNotIn("回调阈值%", snapshot)
        self.assertNotIn("反转阈值%", snapshot)

    def test_report_accepts_legacy_signal_field(self) -> None:
        scalp_trade_history.clear()
        scalp_trade_history.extend([
            {
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "signal": "动能突破多",
                "pnl_usdt": 10.0,
                "entry_time": "2026-04-20 10:00:00",
                "close_reason": "TP3",
            },
            {
                "symbol": "ETHUSDT",
                "direction": "SHORT",
                "signal": "动能突破空",
                "pnl_usdt": -5.0,
                "entry_time": "2026-04-20 10:05:00",
                "close_reason": "SL",
            },
        ])

        response = asyncio.run(get_scalp_report())
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(payload["信号类型分布"], {"动能突破多": 1, "动能突破空": 1})
        self.assertEqual(payload["最佳单笔"]["信号"], "动能突破多")
        self.assertEqual(payload["最差单笔"]["信号"], "动能突破空")


if __name__ == "__main__":
    unittest.main()
