import json
import asyncio
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
            self.assertEqual(rows[0]["opportunity_action"], "WATCH_LONG")
            self.assertEqual(rows[0]["opportunity_permission"], "ALLOW_IF_1M_SIGNAL")
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
                        "action": "WATCH_LONG",
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
            self.assertEqual(rows[0]["opportunity_action"], "WATCH_LONG")
            self.assertEqual(rows[0]["opportunity_permission"], "ALLOW_IF_1M_SIGNAL")
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
                        "action": "WATCH_LONG",
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
            opportunity_action="WATCH_LONG",
            opportunity_score=74,
        )

        payload = ai_gateway._compact_candidate(c)

        self.assertEqual(payload["price_1h"], 3.2)
        self.assertEqual(payload["price_4h"], 7.5)
        self.assertEqual(payload["long_account_pct"], 61.0)
        self.assertEqual(payload["surf_news_sentiment"], "positive")
        self.assertEqual(payload["rule_bias"], "LONG")
        self.assertIn("lesson_stats", payload)

    def test_gemini_request_body_enforces_json_schema(self) -> None:
        body = ai_gateway._gemini_request_body("sys", '{"k":1}', 512)
        gen = body["generationConfig"]
        self.assertEqual(gen["responseMimeType"], "application/json")
        self.assertIn("responseJsonSchema", gen)
        self.assertEqual(gen["maxOutputTokens"], 512)

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


if __name__ == "__main__":
    unittest.main()
