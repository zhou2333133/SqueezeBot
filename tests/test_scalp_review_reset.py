import asyncio
import json
import unittest

import bot_state
import signals as signals_mod
from bot_scalp import BinanceScalpBot, ScalpPosition
from signals import scalp_positions, scalp_signals_history, scalp_trade_history
from web import reset_scalp_review_data, _build_scalp_analysis_pack


class TestScalpReviewReset(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_bot = bot_state.scalp_bot
        self._orig_trades = list(scalp_trade_history)
        self._orig_signals = list(scalp_signals_history)
        self._orig_positions = dict(scalp_positions)
        self._orig_filter_stats = dict(signals_mod.scalp_filter_stats)
        scalp_trade_history.clear()
        scalp_signals_history.clear()
        scalp_positions.clear()

    def tearDown(self) -> None:
        bot_state.scalp_bot = self._orig_bot
        scalp_trade_history.clear()
        scalp_trade_history.extend(self._orig_trades)
        scalp_signals_history.clear()
        scalp_signals_history.extend(self._orig_signals)
        scalp_positions.clear()
        scalp_positions.update(self._orig_positions)
        signals_mod.scalp_filter_stats = self._orig_filter_stats

    def test_reset_scalp_review_data_clears_analysis_pack_sources(self) -> None:
        bot = BinanceScalpBot()
        bot.candidate_meta["BASUSDT"] = {
            "last_price": 1.25,
            "scalp_candidate_seen_price": 1.0,
            "scalp_candidate_max_up_pct": 25.0,
        }
        bot._post_exit_watch["BASUSDT"] = [{"horizons": [15]}]
        bot._fstat["checked"] = 99
        bot.open_positions["BASUSDT"] = ScalpPosition(
            symbol="BASUSDT",
            direction="LONG",
            entry_price=1.0,
            quantity=1.0,
            quantity_remaining=1.0,
            sl_price=0.95,
            tp1_price=1.05,
            tp2_price=1.10,
            paper=True,
        )
        bot_state.scalp_bot = bot
        scalp_trade_history.append({"symbol": "BASUSDT", "pnl_usdt": 1.0})
        scalp_signals_history.append({"symbol": "BASUSDT"})
        scalp_positions["BASUSDT"] = bot.open_positions["BASUSDT"].to_dict()

        response = asyncio.run(reset_scalp_review_data())
        payload = json.loads(response.body.decode("utf-8"))
        pack = asyncio.run(_build_scalp_analysis_pack())

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["cleared"]["trades"], 1)
        self.assertEqual(payload["cleared"]["signals"], 1)
        self.assertEqual(payload["cleared"]["paper_positions"], 1)
        self.assertEqual(pack["运行状态"]["scalp_trades"], 0)
        self.assertEqual(pack["运行状态"]["scalp_signals"], 0)
        self.assertEqual(pack["成交明细"], [])
        self.assertEqual(pack["信号样本"], [])
        self.assertEqual(pack["当前持仓"], {})
        self.assertEqual(signals_mod.scalp_filter_stats["checked"], 0)
        self.assertEqual(bot.candidate_meta["BASUSDT"]["scalp_candidate_seen_price"], 1.25)
        self.assertEqual(bot.candidate_meta["BASUSDT"]["scalp_candidate_max_up_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
