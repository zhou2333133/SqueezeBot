"""测试 Evolver 短期风控闸门：失败模式识别 + guard proposal 生成 + 调度恢复。"""
import os
import json
import tempfile
import unittest
from strategy_evolver import detect_failure_patterns, propose_param_updates, compute_strategy_metrics

def _t(symbol, direction, pnl, close_reason="SL", strategy_tag="启动型",
       ai_provider="", side="LONG"):
    t = {
        "symbol": symbol, "direction": direction, "pnl_usdt": pnl,
        "close_reason": close_reason, "strategy_tag": strategy_tag,
        "entry_context": {"ai_provider": ai_provider} if ai_provider else {},
    }
    # 兼容 _features 字段
    if ai_provider:
        t["_features"] = {"ai_provider": ai_provider}
    return t


class TestRepeatedSymbolSL(unittest.TestCase):
    """模式7: 同一币种多次 SL"""

    def test_detects_repeated_sl(self):
        trades = [_t("BTCUSDT", "LONG", -10, "SL") for _ in range(3)]
        trades.append(_t("BTCUSDT", "LONG", 5, "TP1"))
        patterns = detect_failure_patterns({}, trades)
        matched = [p for p in patterns if p["pattern"] == "repeated_symbol_sl"]
        self.assertTrue(any("BTCUSDT" in p["detail"] for p in matched), "应检测到 BTCUSDT 重复 SL")

    def test_insufficient_sl_not_detected(self):
        trades = [_t("BTCUSDT", "LONG", -10, "SL") for _ in range(1)]
        trades.append(_t("BTCUSDT", "LONG", 5, "TP1"))
        patterns = detect_failure_patterns({}, trades)
        matched = [p for p in patterns if p["pattern"] == "repeated_symbol_sl"]
        self.assertFalse(matched, "1 笔 SL 不应触发")

    def test_exactly_2_sl_triggers(self):
        trades = [_t("BTCUSDT", "LONG", -10, "SL") for _ in range(2)]
        patterns = detect_failure_patterns({}, trades)
        matched = [p for p in patterns if p["pattern"] == "repeated_symbol_sl"]
        self.assertTrue(matched, "2 笔 SL 应触发（不再要求 trades>=3）")


class TestLongLossCluster(unittest.TestCase):
    """模式8: LONG 方向集中亏损"""

    def test_detects_long_loss_cluster(self):
        trades = [_t("BTCUSDT", "LONG", -5, "SL") for _ in range(7)]
        trades += [_t("BTCUSDT", "LONG", 3, "TP1") for _ in range(3)]
        patterns = detect_failure_patterns({}, trades)
        matched = [p for p in patterns if p["pattern"] == "long_loss_cluster"]
        self.assertTrue(matched, "10 笔 LONG 低胜率应检测到")

    def test_good_long_not_detected(self):
        trades = [_t("BTCUSDT", "LONG", 5, "TP1") for _ in range(7)]
        trades += [_t("BTCUSDT", "LONG", -3, "SL") for _ in range(3)]
        patterns = detect_failure_patterns({}, trades)
        matched = [p for p in patterns if p["pattern"] == "long_loss_cluster"]
        self.assertFalse(matched, "高胜率 LONG 不应触发")


class TestWeakMarketLongFailure(unittest.TestCase):
    """模式9: rule_fallback LONG 弱势市场亏损"""

    def test_detects_fallback_long_loss(self):
        trades = [_t("BTCUSDT", "LONG", -5, "SL", ai_provider="rule_fallback") for _ in range(6)]
        trades += [_t("BTCUSDT", "LONG", 3, "TP1", ai_provider="rule_fallback") for _ in range(2)]
        patterns = detect_failure_patterns({}, trades)
        matched = [p for p in patterns if p["pattern"] == "weak_market_long_failure"]
        self.assertTrue(matched, "fallback LONG 低胜率应检测到")

    def test_detects_via_yaobi_ai_provider(self):
        """真实 trade dict 使用 yaobi_ai_provider 字段"""
        trades = []
        for i in range(6):
            t = _t(f"COIN{i}USDT", "LONG", -5, "SL", ai_provider="rule_fallback")
            t["entry_context"]["yaobi_ai_provider"] = "rule_fallback"
            trades.append(t)
        trades += [_t("XUSDT", "LONG", 3, "TP1", ai_provider="minimax")]
        patterns = detect_failure_patterns({}, trades)
        matched = [p for p in patterns if p["pattern"] == "weak_market_long_failure"]
        self.assertTrue(matched, "yaobi_ai_provider 也应被识别")

    def test_ai_long_not_detected(self):
        trades = [_t("BTCUSDT", "LONG", -5, "SL", ai_provider="minimax") for _ in range(5)]
        patterns = detect_failure_patterns({}, trades)
        matched = [p for p in patterns if p["pattern"] == "weak_market_long_failure"]
        self.assertFalse(matched, "AI 通过的 LONG 不应触发")  # 不足 5 笔 fallback


class TestGuardProposals(unittest.TestCase):
    """验证 guard 模式能生成对应的 proposal"""

    def test_repeated_sl_proposal(self):
        cfg = {"RAPID_BLOCK_SL_COUNT": 3}
        patterns = [{"pattern": "repeated_symbol_sl", "strategy_tag": "ALL",
                     "detail": "BTCUSDT SL=2 pnl=-20.0"}]
        proposals = propose_param_updates({}, patterns, cfg)
        keys = [p["key"] for p in proposals]
        self.assertIn("RAPID_BLOCK_SL_COUNT", keys, "应生成 RAPID_BLOCK_SL_COUNT proposal")

    def test_long_loss_proposal(self):
        cfg = {"LONG_PAUSE_LOSS_COUNT": 3, "LONG_PAUSE_MINUTES": 60}
        patterns = [{"pattern": "long_loss_cluster", "strategy_tag": "ALL",
                     "detail": "LONG wr=0.30 exp=-1.5 n=20"}]
        proposals = propose_param_updates({}, patterns, cfg)
        keys = [p["key"] for p in proposals]
        self.assertIn("LONG_PAUSE_LOSS_COUNT", keys, "应生成 LONG_PAUSE_LOSS_COUNT proposal")
        self.assertIn("LONG_PAUSE_MINUTES", keys, "应生成 LONG_PAUSE_MINUTES proposal")

    def test_weak_market_proposal(self):
        cfg = {"MARKET_BREADTH_BTC_5M_DROP_PCT": 2.0, "MARKET_BREADTH_DECLINE_RATIO": 0.7}
        patterns = [{"pattern": "weak_market_long_failure", "strategy_tag": "ALL",
                     "detail": "fallback LONG wr=0.25 loss=6/8"}]
        proposals = propose_param_updates({}, patterns, cfg)
        keys = [p["key"] for p in proposals]
        self.assertIn("MARKET_BREADTH_BTC_5M_DROP_PCT", keys, "应生成 BTC 5m proposal")
        self.assertIn("MARKET_BREADTH_DECLINE_RATIO", keys, "应生成 decline ratio proposal")

    def test_proposal_respects_bounds(self):
        """测试 proposal 不越界"""
        from risk_guard import check_proposals
        cfg = {"RAPID_BLOCK_SL_COUNT": 3, "LONG_PAUSE_LOSS_COUNT": 3,
               "LONG_PAUSE_MINUTES": 60, "MARKET_BREADTH_BTC_5M_DROP_PCT": 2.0,
               "MARKET_BREADTH_DECLINE_RATIO": 0.7}
        patterns = [
            {"pattern": "repeated_symbol_sl", "strategy_tag": "ALL", "detail": "SL=2 pnl=-20"},
            {"pattern": "long_loss_cluster", "strategy_tag": "ALL", "detail": "LONG wr=0.30 n=20"},
            {"pattern": "weak_market_long_failure", "strategy_tag": "ALL", "detail": "fallback wr=0.25"},
        ]
        proposals = propose_param_updates({}, patterns, cfg)
        valid, rejected = check_proposals(proposals)
        self.assertGreater(len(valid), 0, "至少有一些 proposal 应通过")
        self.assertIn("RAPID_BLOCK_SL_COUNT", [p["key"] for p in valid],
                      "RAPID_BLOCK_SL_COUNT 应在 PARAM_BOUNDS 中，应通过")


class TestEvolverSchedulingRecovery(unittest.TestCase):
    """验证 Evolver 调度恢复：重启后 _scheduled_count 从 evolver_state 恢复"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='evo_test_')
        # evolver_state: 49 trades waiting
        with open(os.path.join(self.tmpdir, 'evolver_state.json'), 'w') as f:
            json.dump({"trades_since_last_policy": 49}, f)
        # runtime state: IDLE
        with open(os.path.join(self.tmpdir, 'evolver_runtime_state.json'), 'w') as f:
            json.dump({"status": "IDLE"}, f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _call_scheduler(self, overrides=None):
        """Helper: patch paths, reset counter, call maybe_schedule_evolver_job."""
        import evolver_runtime as er
        import strategy_evolver as se
        self._orig_rt = er.STATE_FILE
        self._orig_ev = se.EVOLVER_STATE_FILE
        er.STATE_FILE = os.path.join(self.tmpdir, 'evolver_runtime_state.json')
        se.EVOLVER_STATE_FILE = os.path.join(self.tmpdir, 'evolver_state.json')
        er._scheduled_count = 0
        er._last_job_ts = 0
        er._last_schedule_ts = 0

        from config import config_manager
        cfg = dict(config_manager.settings)
        cfg.update({
            'EVOLVER_RUNTIME_ENABLED': True,
            'EVOLVER_RUN_AFTER_CLOSED_TRADES': 30,
            'EVOLVER_INTERVAL_MINUTES': 0,
        })
        if overrides:
            cfg.update(overrides)
        return er.maybe_schedule_evolver_job(cfg)

    def _restore(self):
        import evolver_runtime as er
        import strategy_evolver as se
        er.STATE_FILE = getattr(self, '_orig_rt', er.STATE_FILE)
        se.EVOLVER_STATE_FILE = getattr(self, '_orig_ev', se.EVOLVER_STATE_FILE)

    def test_scheduling_recovery_works(self):
        """重启后 _scheduled_count 应恢复为 49，调度通过"""
        result = self._call_scheduler()
        self._restore()
        self.assertTrue(result.get('scheduled'), "调度应通过（49 >= 30）")

    def test_scheduling_skips_when_trades_below_threshold(self):
        """trades_since_last_policy=10 < 30 → 应跳过"""
        with open(os.path.join(self.tmpdir, 'evolver_state.json'), 'w') as f:
            json.dump({"trades_since_last_policy": 10}, f)
        result = self._call_scheduler()
        self._restore()
        self.assertFalse(result.get('scheduled'), "10 < 30，应跳过")
        self.assertIn("trades_", result.get("reason", ""), "跳过原因应包含 trades_")

    def test_job_result_has_meaningful_feedback(self):
        """job 执行后应返回有意义的 status 而非空白"""
        # 设置全局 config 让 job 真正跑
        from config import config_manager
        saved = dict(config_manager.settings)
        config_manager.settings['EVOLVER_ENABLED'] = True
        config_manager.settings['EVOLVER_RUNTIME_ENABLED'] = True
        config_manager.settings['EVOLVER_RUN_AFTER_CLOSED_TRADES'] = 30
        config_manager.settings['EVOLVER_INTERVAL_MINUTES'] = 0
        config_manager.settings['EVOLVER_MIN_TOTAL_TRADES'] = 10
        config_manager.settings['EVOLVER_MIN_TRADES_PER_STRATEGY'] = 5

        result = self._call_scheduler()
        config_manager.settings.clear()
        config_manager.settings.update(saved)
        self._restore()

        self.assertTrue(result.get('scheduled'))
        jr = result.get('job_result', {})
        self.assertIn(jr.get('status', ''), ('SUCCESS', 'SKIPPED', 'NO_CHANGES'),
                      "job 应返回合法状态")
        self.assertTrue(jr.get('reason', ''), "job 应有非空 reason")


if __name__ == "__main__":
    unittest.main()
