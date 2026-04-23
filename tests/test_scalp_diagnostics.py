import unittest

from scalp_diagnostics import (
    build_entry_1m_profile,
    build_learning_report,
    diagnose_trade,
)


class TestScalpDiagnostics(unittest.TestCase):
    def test_diagnose_entry_bad_when_sl_then_original_direction_runs(self) -> None:
        result = diagnose_trade({
            "symbol": "BASUSDT",
            "direction": "SHORT",
            "close_reason": "SL",
            "pnl_usdt": -8.0,
            "mfe_pct": 0.35,
            "mae_pct": 1.2,
            "post_exit_mfe_pct": 7.35,
            "post_exit_30m_favorable_pct": 4.2,
        })

        self.assertEqual(result["trade_diagnosis"], "entry_bad")
        self.assertIn("entry_bad", result["diagnosis_tags"])

    def test_diagnose_direction_wrong_when_trade_never_gets_paid(self) -> None:
        result = diagnose_trade({
            "symbol": "BADUSDT",
            "direction": "LONG",
            "close_reason": "SL",
            "pnl_usdt": -6.0,
            "mfe_pct": 0.2,
            "mae_pct": 1.1,
            "post_exit_mfe_pct": 0.3,
            "post_exit_30m_favorable_pct": 0.2,
        })

        self.assertEqual(result["trade_diagnosis"], "direction_wrong")
        self.assertIn("direction_wrong", result["diagnosis_tags"])

    def test_entry_profile_records_pre_moves_ema_pullback_and_taker_trend(self) -> None:
        buf = []
        price = 100.0
        for i in range(30):
            close = price + i * 0.2
            buf.append({
                "o": close - 0.1,
                "h": close + 0.3,
                "l": close - 0.3,
                "c": close,
                "q": 1000.0,
                "Q": 450.0 + i * 3,
            })
        live = {"close": 106.5, "total_vol": 900.0, "taker_buy": 630.0}

        profile = build_entry_1m_profile(buf, live, "LONG", 106.5, atr_pct=0.55)

        self.assertGreater(profile["pre_entry_3m_pct"], 0)
        self.assertGreater(profile["ema20_deviation_pct"], 0)
        self.assertIn(profile["taker_trend_5m"], {"rising", "flat", "falling"})
        self.assertIn("breakout_after_pullback", profile)
        self.assertEqual(profile["kline_buffer_len"], 30)

    def test_learning_report_groups_loss_and_samples(self) -> None:
        trades = [
            {
                "symbol": "BASUSDT",
                "direction": "SHORT",
                "close_reason": "SL",
                "pnl_usdt": -8.0,
                "mfe_pct": 0.35,
                "mae_pct": 1.2,
                "post_exit_mfe_pct": 7.35,
                "post_exit_30m_favorable_pct": 4.2,
            },
            {
                "symbol": "RUNUSDT",
                "direction": "LONG",
                "close_reason": "TP3",
                "pnl_usdt": 5.0,
                "mfe_pct": 3.0,
                "mae_pct": 0.4,
                "tp1_hit": True,
                "post_exit_60m_favorable_pct": 4.5,
            },
        ]

        report = build_learning_report(trades)

        self.assertEqual(report["诊断分布"]["entry_bad"], 1)
        self.assertEqual(report["标签分布"]["valid_runner_missed"], 1)
        self.assertEqual(report["亏损归因"]["entry_bad"]["count"], 1)
        self.assertEqual(report["点位错样本"][0]["symbol"], "BASUSDT")
        self.assertEqual(report["卖飞样本"][0]["symbol"], "RUNUSDT")
        self.assertTrue(report["下一轮参数建议"])


if __name__ == "__main__":
    unittest.main()
