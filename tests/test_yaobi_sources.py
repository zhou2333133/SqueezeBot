import json
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from scanner import ai_gateway
from scanner.candidates import Candidate, clear_candidates, get_anomaly_candidates, get_opportunity_queue, upsert_candidate
from scanner.sources import binance_futures, binance_square, okx_market, surf_api
from scanner.sources.binance_liquidations import liquidation_stats, record_liquidation_event
from scanner.yaobi_scanner import YaobiScanner


class TestYaobiSources(unittest.TestCase):
    def test_okx_price_info_array_payload_parsing(self) -> None:
        data = {
            "code": "0",
            "data": [{
                "price": "0.00123",
                "marketCap": "1234567",
                "liquidity": "45678",
                "holders": "3210",
                "priceChange24H": "12.5",
                "volume24H": "99999",
                "txs5M": "17",
            }],
        }

        item = okx_market._first_data(data)

        self.assertEqual(item["price"], "0.00123")
        self.assertEqual(okx_market._float_any(item, "price"), 0.00123)
        self.assertEqual(okx_market._int_any(item, "holders"), 3210)
        self.assertEqual(okx_market._float_any(item, "priceChange24H"), 12.5)

    def test_okx_sol_address_preserves_case(self) -> None:
        sol_addr = "SoLCaseSensitive123abcXYZ"
        self.assertEqual(okx_market._normalize_address("501", sol_addr), sol_addr)
        self.assertEqual(okx_market._normalize_address("1", "0xABCDEF"), "0xabcdef")

    def test_okx_price_info_parser_returns_batch_key_fields(self) -> None:
        row = {
            "chainIndex": "1",
            "tokenContractAddress": "0xABCDEF",
            "price": "1.25",
            "top10HoldPercent": "66.6",
            "txs24H": "123",
        }
        parsed = okx_market._parse_price_info(row)
        self.assertEqual(parsed["chain_id"], "1")
        self.assertEqual(parsed["address"], "0xabcdef")
        self.assertEqual(parsed["price_usd"], 1.25)
        self.assertEqual(parsed["tx_count_24h"], 123)

    def test_okx_data_list_accepts_nested_token_list_response(self) -> None:
        data = {
            "code": "0",
            "data": {
                "cursor": "next",
                "tokenList": [
                    {"chainIndex": "1", "tokenSymbol": "WETH"},
                    {"chainIndex": "501", "tokenSymbol": "SOL"},
                ],
            },
        }

        rows = okx_market._data_list(data)

        self.assertEqual([x["tokenSymbol"] for x in rows], ["WETH", "SOL"])

    def test_okx_trade_side_and_usd_parsing_accepts_common_field_variants(self) -> None:
        buy = {"side": "BUY", "usdValue": "1200"}
        sell = {"transactionType": "swap_sell", "amountUsd": "800"}

        self.assertEqual(okx_market._trade_side(buy), "buy")
        self.assertEqual(okx_market._trade_usd(buy), 1200.0)
        self.assertEqual(okx_market._trade_side(sell), "sell")
        self.assertEqual(okx_market._trade_usd(sell), 800.0)

    def test_binance_square_extracts_nested_posts_and_mentions(self) -> None:
        payload = {
            "code": "000000",
            "data": {
                "content": [{
                    "postId": "abc",
                    "content": {"bodyTextOnly": "Watching $PEPE and SOLUSDT after breakout"},
                    "stat": {"likeCount": "12", "viewCount": "500"},
                }]
            },
        }

        posts = binance_square._extract_posts(payload)
        mentions = binance_square.extract_ticker_mentions(posts)

        self.assertEqual(len(posts), 1)
        self.assertIn("PEPE", mentions)
        self.assertIn("SOL", mentions)
        self.assertEqual(mentions["PEPE"]["count"], 1)

    def test_anomaly_radar_combines_okx_futures_and_sentiment(self) -> None:
        c = Candidate(
            symbol="RAVE",
            has_futures=True,
            price_change_24h=-44.9,
            oi_change_24h_pct=16.4,
            square_mentions=10,
            funding_rate_pct=0.339,
            whale_long_ratio=0.07,
            retail_short_pct=80.0,
            okx_large_trade_pct=0.35,
            okx_buy_ratio=0.75,
            surf_news_titles=["KOL discusses RAVE", "RAVE funding turns unusual"],
        )

        YaobiScanner()._apply_anomaly_radar([c])

        self.assertGreaterEqual(c.anomaly_score, 50)
        self.assertIn("OKX大单", c.anomaly_tags)
        self.assertIn("散户极空", c.anomaly_tags)
        self.assertEqual(c.long_short_text, "多7%/空93%")
        self.assertGreaterEqual(c.sentiment_heat, 12)

    def test_decision_cards_explain_candidate_action(self) -> None:
        c = Candidate(
            symbol="HIGH",
            has_futures=True,
            score=72,
            anomaly_score=65,
            oi_trend_grade="S",
            oi_change_7d_pct=95,
            oi_change_3d_pct=36,
            sentiment_label="bullish",
            funding_rate_pct=-0.04,
            retail_short_pct=70,
        )

        YaobiScanner()._apply_decision_cards([c])

        self.assertEqual(c.decision_action, "允许交易")
        self.assertGreaterEqual(c.decision_confidence, 55)
        self.assertTrue(c.decision_reasons)

    def test_oi_trend_fields_grade_sustained_growth(self) -> None:
        oi_hist = [{"sumOpenInterest": str(100 + i * 3)} for i in range(50)]
        klines = [[0, 0, 0, 0, str(100 + i)] for i in range(10)]

        fields = binance_futures._calc_oi_trend_fields(oi_hist, klines)

        self.assertIn(fields["oi_trend_grade"], {"A", "S"})
        self.assertGreater(fields["oi_change_7d_pct"], 40)
        self.assertGreater(fields["oi_consistency_score"], 80)

    def test_short_activity_score_reacts_to_oi_volume_and_taker(self) -> None:
        score = binance_futures._short_activity_score(
            oi_5m_pct=2.5,
            oi_15m_pct=6.0,
            volume_5m_ratio=3.0,
            taker_buy_ratio=0.66,
            funding_pct=-0.06,
            long_account_pct=34,
            top_long_pct=67,
            price_24h=18,
            oi_volume_ratio=9,
        )

        self.assertGreaterEqual(score, 60)

    def test_liquidation_cache_rolls_up_windows(self) -> None:
        record_liquidation_event({"o": {"s": "TESTUSDT", "S": "SELL", "ap": "2", "q": "1000"}})

        stats = liquidation_stats({"TESTUSDT"}, windows_min=(5,))

        self.assertEqual(stats["TESTUSDT"]["liquidation_5m_usd"], 2000.0)

    def test_opportunity_queue_prefers_short_term_confirmed_direction(self) -> None:
        clear_candidates()
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["YAOBI_AI_ENABLED"] = False
            config_manager.settings["YAOBI_AI_REQUIRED_FOR_PERMISSION"] = False
            c = Candidate(
                symbol="BAS",
                has_futures=True,
                score=62,
                anomaly_score=55,
                contract_activity_score=70,
                oi_change_15m_pct=5.2,
                oi_change_5m_pct=1.4,
                volume_5m_ratio=2.8,
                taker_buy_ratio_5m=0.64,
                funding_rate_pct=-0.07,
                retail_short_pct=68,
            )
            scanner = YaobiScanner()
            scanner._apply_anomaly_radar([c])
            scanner._apply_decision_cards([c])
            import asyncio
            asyncio.run(scanner._apply_opportunity_queue([c]))

            rows = get_opportunity_queue()

            self.assertEqual(rows[0]["symbol"], "BAS")
            self.assertEqual(rows[0]["opportunity_action"], "WATCH_LONG_CONTINUATION")
            self.assertEqual(rows[0]["opportunity_permission"], "ALLOW_IF_1M_SIGNAL")
            self.assertEqual(rows[0]["opportunity_trigger_family"], "BREAKOUT")
            self.assertIn(rows[0]["opportunity_setup_state"], {"ARMED", "HOT"})
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)
            clear_candidates()

    def test_ai_required_permission_keeps_rule_watch_in_observe_until_ai_approves(self) -> None:
        clear_candidates()
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["YAOBI_AI_ENABLED"] = True
            config_manager.settings["YAOBI_AI_REQUIRED_FOR_PERMISSION"] = True
            config_manager.settings["YAOBI_DUAL_AI_CONSENSUS_REQUIRED"] = False
            config_manager.settings["YAOBI_AI_MAX_SYMBOLS_PER_RUN"] = 1
            c = Candidate(
                symbol="BAS",
                has_futures=True,
                score=62,
                anomaly_score=55,
                contract_activity_score=70,
                oi_change_15m_pct=5.2,
                oi_change_5m_pct=1.4,
                volume_5m_ratio=2.8,
                taker_buy_ratio_5m=0.64,
                funding_rate_pct=-0.07,
                retail_short_pct=68,
            )
            scanner = YaobiScanner()
            scanner._apply_anomaly_radar([c])
            scanner._apply_decision_cards([c])

            async def fake_ai(_session, _cands):
                return {
                    "items": [{
                        "symbol": "BAS",
                        "action": "WATCH_LONG_CONTINUATION",
                        "permission": "ALLOW_IF_1M_SIGNAL",
                        "confidence": 81,
                        "summary": "AI confirms long bias",
                        "reasons": ["OI/taker aligned"],
                        "risks": [],
                        "required_confirmation": ["wait for local 1m breakout"],
                    }],
                    "status": {"last_reason": "ok", "last_provider": "gemini"},
                }

            with patch("scanner.yaobi_scanner.analyze_opportunities", fake_ai):
                asyncio.run(scanner._apply_opportunity_queue([c]))

            rows = get_opportunity_queue()
            self.assertEqual(rows[0]["opportunity_action"], "WATCH_LONG_CONTINUATION")
            self.assertEqual(rows[0]["opportunity_permission"], "ALLOW_IF_1M_SIGNAL")
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)
            clear_candidates()

    def test_ai_transport_failure_falls_back_to_high_score_rule_playbook(self) -> None:
        clear_candidates()
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["YAOBI_AI_ENABLED"] = True
            config_manager.settings["YAOBI_AI_REQUIRED_FOR_PERMISSION"] = True
            config_manager.settings["YAOBI_AI_FAILURE_FALLBACK_ENABLED"] = True
            config_manager.settings["YAOBI_AI_FAILURE_FALLBACK_MIN_SCORE"] = 45
            config_manager.settings["YAOBI_DUAL_AI_CONSENSUS_REQUIRED"] = False
            c = Candidate(
                symbol="SKR",
                has_futures=True,
                score=70,
                anomaly_score=60,
                contract_activity_score=72,
                price_change_24h=38.0,
                price_change_1h=0.4,
                oi_change_24h_pct=74.0,
                oi_change_15m_pct=1.2,
                oi_change_5m_pct=0.1,
                volume_5m_ratio=0.7,
                taker_buy_ratio_5m=0.46,
                surf_ai_risk_level="LOW",
            )
            scanner = YaobiScanner()
            scanner._apply_anomaly_radar([c])
            scanner._apply_decision_cards([c])

            async def fake_ai_failure(_session, _cands):
                return {
                    "items": [],
                    "status": {
                        "last_reason": "all_failed",
                        "last_error": "ClientConnectorSSLError: ssl handshake failure",
                    },
                }

            with patch("scanner.yaobi_scanner.analyze_opportunities", fake_ai_failure):
                asyncio.run(scanner._apply_opportunity_queue([c]))

            rows = get_opportunity_queue()
            self.assertEqual(rows[0]["opportunity_action"], "WATCH_LONG_CONTINUATION")
            self.assertEqual(rows[0]["opportunity_permission"], "ALLOW_IF_1M_SIGNAL")
            self.assertIn(rows[0]["opportunity_setup_state"], {"ARMED", "HOT"})
            self.assertIn("AI终审连接失败", " ".join(rows[0]["opportunity_reasons"]))
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)
            clear_candidates()

    def test_dual_ai_consensus_blocks_when_surf_direction_disagrees(self) -> None:
        clear_candidates()
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["YAOBI_AI_ENABLED"] = True
            config_manager.settings["YAOBI_AI_REQUIRED_FOR_PERMISSION"] = True
            config_manager.settings["YAOBI_DUAL_AI_CONSENSUS_REQUIRED"] = True
            config_manager.settings["YAOBI_SURF_AI_ENABLED"] = True
            config_manager.settings["YAOBI_SURF_DIRECTION_MIN_CONFIDENCE"] = 55
            c = Candidate(
                symbol="BAS",
                has_futures=True,
                score=62,
                anomaly_score=55,
                contract_activity_score=70,
                oi_change_15m_pct=5.2,
                oi_change_5m_pct=1.4,
                volume_5m_ratio=2.8,
                taker_buy_ratio_5m=0.64,
                funding_rate_pct=-0.07,
                retail_short_pct=68,
                surf_ai_bias="SHORT",
                surf_ai_confidence=80,
                surf_ai_risk_level="LOW",
                surf_ai_reason="Flow fades after spike",
            )
            scanner = YaobiScanner()
            scanner._apply_anomaly_radar([c])
            scanner._apply_decision_cards([c])

            async def fake_ai(_session, _cands):
                return {
                    "items": [{
                        "symbol": "BAS",
                        "action": "WATCH_LONG_CONTINUATION",
                        "permission": "ALLOW_IF_1M_SIGNAL",
                        "confidence": 81,
                        "summary": "Gemini confirms long bias",
                        "reasons": ["OI/taker aligned"],
                        "risks": [],
                        "required_confirmation": ["wait for local 1m breakout"],
                    }],
                    "status": {"last_reason": "ok", "last_provider": "gemini"},
                }

            with patch("scanner.yaobi_scanner._surf_enabled", return_value=True):
                with patch("scanner.yaobi_scanner.analyze_opportunities", fake_ai):
                    asyncio.run(scanner._apply_opportunity_queue([c]))

            rows = get_opportunity_queue()
            self.assertEqual(rows[0]["opportunity_action"], "OBSERVE")
            self.assertEqual(rows[0]["opportunity_permission"], "OBSERVE")
            self.assertIn("Surf偏SHORT与Gemini偏LONG不一致", rows[0]["opportunity_risks"][0])
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)
            clear_candidates()

    def test_surf_veto_only_keeps_gemini_direction_when_strict_consensus_off(self) -> None:
        clear_candidates()
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["YAOBI_AI_ENABLED"] = True
            config_manager.settings["YAOBI_AI_REQUIRED_FOR_PERMISSION"] = True
            config_manager.settings["YAOBI_DUAL_AI_CONSENSUS_REQUIRED"] = False
            config_manager.settings["YAOBI_SURF_AI_ENABLED"] = True
            c = Candidate(
                symbol="SKR",
                has_futures=True,
                score=68,
                anomaly_score=58,
                contract_activity_score=72,
                oi_change_15m_pct=8.5,
                oi_change_5m_pct=1.1,
                volume_5m_ratio=0.7,
                taker_buy_ratio_5m=0.46,
                price_change_24h=28.0,
                surf_ai_bias="SHORT",
                surf_ai_confidence=72,
                surf_ai_risk_level="LOW",
                surf_ai_reason="Local flow fades after expansion",
            )
            scanner = YaobiScanner()
            scanner._apply_anomaly_radar([c])
            scanner._apply_decision_cards([c])

            async def fake_ai(_session, _cands):
                return {
                    "items": [{
                        "symbol": "SKR",
                        "action": "WATCH_SHORT_FADE",
                        "permission": "ALLOW_IF_1M_SIGNAL",
                        "confidence": 79,
                        "summary": "Use only local fade setup",
                        "reasons": ["Strong 15m trend but 5m flow weakens"],
                        "risks": [],
                        "required_confirmation": ["only allow local squeeze fade, not breakout chase"],
                    }],
                    "status": {"last_reason": "ok", "last_provider": "gemini"},
                }

            with patch("scanner.yaobi_scanner._surf_enabled", return_value=True):
                with patch("scanner.yaobi_scanner.analyze_opportunities", fake_ai):
                    asyncio.run(scanner._apply_opportunity_queue([c]))

            rows = get_opportunity_queue()
            self.assertEqual(rows[0]["opportunity_action"], "WATCH_SHORT_FADE")
            self.assertEqual(rows[0]["opportunity_permission"], "ALLOW_IF_1M_SIGNAL")
            self.assertEqual(rows[0]["opportunity_trigger_family"], "SQUEEZE")
            self.assertIn(rows[0]["opportunity_setup_state"], {"ARMED", "HOT"})
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)
            clear_candidates()

    def test_soft_surf_high_risk_does_not_force_block(self) -> None:
        clear_candidates()
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["YAOBI_AI_ENABLED"] = False
            config_manager.settings["YAOBI_AI_REQUIRED_FOR_PERMISSION"] = False
            c = Candidate(
                symbol="SKR",
                has_futures=True,
                score=68,
                anomaly_score=60,
                contract_activity_score=72,
                price_change_24h=28.0,
                price_change_1h=3.0,
                oi_change_15m_pct=8.5,
                oi_change_24h_pct=70.0,
                oi_change_5m_pct=0.6,
                volume_5m_ratio=0.8,
                taker_buy_ratio_5m=0.46,
                surf_ai_risk_level="HIGH",
                surf_ai_reason="Mixed signals, neutral funding, balanced long/short positions",
            )
            scanner = YaobiScanner()
            scanner._apply_anomaly_radar([c])
            scanner._apply_decision_cards([c])
            asyncio.run(scanner._apply_opportunity_queue([c]))

            rows = get_opportunity_queue()
            self.assertNotEqual(rows[0]["opportunity_action"], "BLOCK")
            self.assertNotEqual(c.decision_action, "禁止交易")
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)
            clear_candidates()

    def test_active_playbook_is_carried_forward_within_ttl(self) -> None:
        clear_candidates()
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["YAOBI_AI_ENABLED"] = False
            config_manager.settings["YAOBI_AI_REQUIRED_FOR_PERMISSION"] = False
            old = Candidate(
                symbol="MOVR",
                has_futures=True,
                score=12,
                anomaly_score=12,
                opportunity_action="WATCH_SHORT_FADE",
                opportunity_permission="ALLOW_IF_1M_SIGNAL",
                opportunity_trigger_family="SQUEEZE",
                opportunity_setup_state="HOT",
                opportunity_expires_at="2099-01-01 00:00:00",
            )
            upsert_candidate(old)
            c = Candidate(
                symbol="MOVR",
                has_futures=True,
                score=18,
                anomaly_score=14,
                contract_activity_score=25,
                price_change_24h=18.0,
                price_change_1h=0.2,
                oi_change_15m_pct=0.3,
                taker_buy_ratio_5m=0.49,
                volume_5m_ratio=0.6,
            )
            scanner = YaobiScanner()
            scanner._apply_anomaly_radar([c])
            scanner._apply_decision_cards([c])
            asyncio.run(scanner._apply_opportunity_queue([c]))

            rows = get_opportunity_queue()
            self.assertEqual(rows[0]["opportunity_action"], "WATCH_SHORT_FADE")
            self.assertEqual(rows[0]["opportunity_permission"], "ALLOW_IF_1M_SIGNAL")
            self.assertIn("沿用上一轮AI剧本窗口", " ".join(rows[0]["opportunity_reasons"]))
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)
            clear_candidates()

    def test_surf_ai_batch_review_updates_direction_fields(self) -> None:
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["YAOBI_SURF_AI_MODEL"] = "surf-ask"
            scanner = YaobiScanner()
            candidates = [
                Candidate(symbol="AAA", score=70, surf_news_titles=["AAA breakout"], price_change_24h=12.0),
                Candidate(symbol="BBB", score=68, surf_news_titles=["BBB funding squeeze"], price_change_24h=-6.0),
            ]

            async def fake_chat(_session, _prompt, model=None, timeout_sec=18.0, reasoning_effort="low"):
                self.assertEqual(model, "surf-ask")
                return True, json.dumps({
                    "items": [
                        {"symbol": "AAA", "bias": "LONG", "risk": "LOW", "confidence": 77, "reason": "buyers keep control"},
                        {"symbol": "BBB", "bias": "SHORT", "risk": "MEDIUM", "confidence": 66, "reason": "flow weakens"},
                    ]
                }), 200

            with patch("scanner.yaobi_scanner._surf_enabled", return_value=True):
                with patch("scanner.yaobi_scanner.surf_chat_completion", fake_chat):
                    asyncio.run(scanner._surf_ai_review(None, candidates))

            self.assertEqual(candidates[0].surf_ai_bias, "LONG")
            self.assertEqual(candidates[0].surf_ai_risk_level, "LOW")
            self.assertEqual(candidates[0].surf_ai_confidence, 77)
            self.assertEqual(candidates[1].surf_ai_bias, "SHORT")
            self.assertEqual(candidates[1].surf_ai_risk_level, "MEDIUM")
            self.assertEqual(candidates[1].surf_ai_confidence, 66)
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)

    def test_ai_gateway_compact_candidate_includes_directional_samples(self) -> None:
        c = Candidate(
            symbol="BAS",
            price_change_1h=3.2,
            price_change_4h=7.5,
            price_change_24h=18.0,
            long_account_pct=61.0,
            surf_news_sentiment="positive",
            surf_news_titles=["Protocol upgrade scheduled"],
            opportunity_action="WATCH_LONG_CONTINUATION",
            opportunity_score=74,
        )

        payload = ai_gateway._compact_candidate(c)

        self.assertEqual(payload["price_1h"], 3.2)
        self.assertEqual(payload["price_4h"], 7.5)
        self.assertEqual(payload["long_account_pct"], 61.0)
        self.assertEqual(payload["surf_news_sentiment"], "positive")
        self.assertEqual(payload["rule_bias"], "LONG")
        self.assertIn("lesson_stats", payload)

    def test_ai_gateway_prompt_evaluates_playbook_without_1m_signal(self) -> None:
        from config import config_manager
        orig = config_manager.settings.copy()
        old_usage = ai_gateway._USAGE_FILE
        old_cache = ai_gateway._CACHE_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ai_gateway._USAGE_FILE = os.path.join(tmp, "usage.json")
                ai_gateway._CACHE_FILE = os.path.join(tmp, "cache.json")
                config_manager.settings["YAOBI_AI_ENABLED"] = True
                config_manager.settings["YAOBI_AI_PROVIDER_PRIORITY"] = "gemini"
                config_manager.settings["YAOBI_AI_DAILY_USD_CAP"] = 0.0
                captured = {}

                async def fake_gemini(_session, system_prompt, payload, _max_output):
                    captured["system_prompt"] = system_prompt
                    captured["payload"] = payload
                    return json.dumps({
                        "opportunities": [{
                            "symbol": "SKR",
                            "action": "WATCH_LONG_CONTINUATION",
                            "permission": "ALLOW_IF_1M_SIGNAL",
                            "confidence": 82,
                            "summary": "15m playbook is tradable",
                            "reasons": ["5m/15m flow agrees"],
                            "risks": [],
                            "required_confirmation": ["local 1m pullback entry"],
                        }]
                    }), 800

                c = Candidate(symbol="SKR", has_futures=True, opportunity_action="WATCH_LONG_CONTINUATION")
                with patch("scanner.ai_gateway.GEMINI_API_KEY", "test-key"):
                    with patch("scanner.ai_gateway.ai_credentials_status", return_value={"enabled": True}):
                        with patch("scanner.ai_gateway._call_gemini", fake_gemini):
                            result = asyncio.run(ai_gateway.analyze_opportunities(None, [c]))

                self.assertEqual(result["items"][0]["permission"], "ALLOW_IF_1M_SIGNAL")
                self.assertIn("Do not wait for or ask for 1m candles", captured["payload"])
                self.assertIn("Do not downgrade a candidate to OBSERVE just because no 1m signal is supplied", captured["system_prompt"])
        finally:
            ai_gateway._USAGE_FILE = old_usage
            ai_gateway._CACHE_FILE = old_cache
            config_manager.settings.clear()
            config_manager.settings.update(orig)

    def test_ai_gateway_reuses_last_success_during_min_interval(self) -> None:
        from config import config_manager
        orig = config_manager.settings.copy()
        old_usage = ai_gateway._USAGE_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                usage_path = os.path.join(tmp, "usage.json")
                ai_gateway._USAGE_FILE = usage_path
                config_manager.settings["YAOBI_AI_ENABLED"] = True
                config_manager.settings["YAOBI_AI_MIN_INTERVAL_MINUTES"] = 15
                config_manager.settings["YAOBI_AI_CACHE_TTL_MINUTES"] = 30
                ai_gateway._save_json(usage_path, {
                    "_meta": {"last_call_ts": ai_gateway.time.time()},
                    ai_gateway._LATEST_SUCCESS_KEY: {
                        "ts": ai_gateway.time.time(),
                        "provider": "gemini",
                        "items": [{
                            "symbol": "SKR",
                            "action": "WATCH_SHORT_FADE",
                            "permission": "ALLOW_IF_1M_SIGNAL",
                            "confidence": 81,
                            "summary": "reuse",
                            "reasons": ["cached"],
                            "risks": [],
                            "required_confirmation": ["wait 1m fade signal"],
                        }],
                    },
                })
                c = Candidate(symbol="SKR", opportunity_action="OBSERVE")

                with patch("scanner.ai_gateway.ai_credentials_status", return_value={"enabled": True}):
                    result = asyncio.run(ai_gateway.analyze_opportunities(None, [c]))

                self.assertEqual(result["status"]["last_reason"], "min_interval_reuse")
                self.assertEqual(result["items"][0]["action"], "WATCH_SHORT_FADE")
                self.assertEqual(result["items"][0]["provider"], "gemini")
        finally:
            ai_gateway._USAGE_FILE = old_usage
            config_manager.settings.clear()
            config_manager.settings.update(orig)

    def test_gemini_request_body_enforces_json_schema(self) -> None:
        body = ai_gateway._gemini_request_body("sys", '{"k":1}', 512)
        gen = body["generationConfig"]
        self.assertEqual(gen["responseMimeType"], "application/json")
        self.assertIn("responseJsonSchema", gen)
        self.assertEqual(gen["maxOutputTokens"], 512)

    def test_gemini_non_json_response_is_retried_with_fallback_model(self) -> None:
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["YAOBI_AI_MODEL_GEMINI"] = "gemini-2.5-flash"
            seen = []

            class DummyResp:
                def __init__(self, status, data):
                    self.status = status
                    self._data = data
                async def json(self, content_type=None):
                    return self._data
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc, tb):
                    return False

            class DummySession:
                def post(self, url, headers=None, json=None, timeout=None):
                    seen.append(url)
                    if "gemini-2.5-flash" in url:
                        return DummyResp(200, {"candidates": [{"content": {"parts": [{"text": "not-json"}]}}]})
                    return DummyResp(200, {"candidates": [{"content": {"parts": [{"text": "{\"opportunities\":[]}"}]}}]})

            text, _tokens = asyncio.run(ai_gateway._call_gemini(DummySession(), "sys", '{"k":1}', 256))
            self.assertEqual(text, "{\"opportunities\":[]}")
            self.assertGreaterEqual(len(seen), 2)
            self.assertTrue(any("gemini-2.5-flash-lite" in url or "gemini-2.5-pro" in url or "gemini-3-flash-preview" in url for url in seen[1:]))
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)

    def test_anomaly_candidates_sorted_by_anomaly_score(self) -> None:
        clear_candidates()
        try:
            low = Candidate(symbol="LOW", anomaly_score=20, score=90)
            high = Candidate(symbol="HIGH", anomaly_score=75, score=10)
            upsert_candidate(low)
            upsert_candidate(high)

            rows = get_anomaly_candidates(min_anomaly=30, limit=10)

            self.assertEqual([r["symbol"] for r in rows], ["HIGH"])
        finally:
            clear_candidates()

    def test_surf_project_terms_and_news_match_symbol_alias(self) -> None:
        terms = surf_api.project_terms("BTCUSDT")
        self.assertIn("btc", terms)
        self.assertIn("bitcoin", terms)

        item = {
            "project_name": "Bitcoin",
            "text": "Bitcoin ETF flows remain strong",
        }
        self.assertTrue(surf_api.news_matches_symbol(item, "BTCUSDT"))

    def test_surf_normalizes_news_items(self) -> None:
        rows = surf_api.normalize_news_items([{
            "id": "n1",
            "title": "Ethereum upgrade ships",
            "summary": "Mainnet upgrade completed",
            "source": "COINDESK",
            "project_name": "Ethereum",
            "published_at": 1776810000,
        }])

        self.assertEqual(rows[0]["title"], "Ethereum upgrade ships")
        self.assertIn("Mainnet upgrade completed", rows[0]["text"])

    def test_surf_throttle_tracks_last_request_per_key(self) -> None:
        from config import config_manager
        orig = config_manager.settings.copy()
        try:
            config_manager.settings["SURF_MIN_REQUEST_INTERVAL"] = 0.001
            surf_api._last_request_at.clear()

            asyncio.run(surf_api._throttle("test-key"))

            self.assertIn("test-key", surf_api._last_request_at)
        finally:
            config_manager.settings.clear()
            config_manager.settings.update(orig)
            surf_api._last_request_at.clear()


if __name__ == "__main__":
    unittest.main()
