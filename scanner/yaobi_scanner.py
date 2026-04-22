"""
妖币扫描器 — 主调度器
数据链路:
  GeckoTerminal + DEX Screener → Binance Square
  → Surf新闻预过滤 → OKX价格补全 → 合约OI扫描
  → OKX链验证+大单分析 → 币安资金费率+散户多空
  → 打分 → Surf AI终审(Top-N) → Obsidian
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, date
from typing import Optional

import aiohttp

from config import config_manager, surf_credentials_status
from scanner.candidates import (
    Candidate, upsert_candidate, scan_status, candidates_map, clear_candidates,
)
from scanner.scorer import score as score_candidate
from scanner.sources.geckoterminal import fetch_trending_pools, fetch_new_pools
from scanner.sources.dexscreener import (
    fetch_token_boosts, fetch_latest_profiles, fetch_token_detail,
)
from scanner.sources.okx_market import (
    CHAIN_IDS,
    CHAIN_NAMES,
    get_hot_tokens,
    get_token_advanced_info,
    get_token_holders,
    get_token_price_info_batch,
    get_token_trades,
    search_tokens,
)
from scanner.sources.binance_square import fetch_hot_posts, extract_ticker_mentions
from scanner.sources.binance_futures import scan_futures_oi
from scanner.sources.surf_api import (
    chat_completion as surf_chat_completion,
    fetch_news_feed as surf_fetch_news_feed,
    news_matches_symbol as surf_news_matches_symbol,
    project_terms as surf_project_terms,
    search_news as surf_search_news,
)
from scanner.provider_metrics import record_provider_call, record_provider_skip
from scanner.obsidian import (
    init_rulebook, write_daily_candidates, write_daily_digest,
    write_square_archive, write_token_doc,
)

logger = logging.getLogger(__name__)

_FUTURES_URL   = "https://fapi.binance.com/fapi/v1/exchangeInfo"
_SCAN_NETWORKS = ["eth", "bsc", "solana", "base", "arbitrum"]

_NEG_KW = {"hack", "exploit", "rug", "scam", "delist", "suspend", "crash",
           "fraud", "lawsuit", "ponzi", "breach", "stolen", "vulnerability"}
_POS_KW = {"partnership", "listing", "upgrade", "adoption", "mainnet", "launch",
           "integration", "backed", "grant", "milestone", "record"}
_NEG_GATES = {"not", "no", "never", "successfully", "patches", "fixed",
              "resolved", "mitigated", "prevents", "blocked"}


def _surf_enabled() -> bool:
    return bool(surf_credentials_status().get("enabled"))


class YaobiScanner:
    def __init__(self):
        self.running = False
        self._futures_symbols: set[str] = set()
        self._futures_cached_at: Optional[datetime] = None
        self._last_obsidian_date: Optional[date] = None

    async def run(self) -> None:
        self.running = True
        try:
            init_rulebook()
        except Exception as e:
            logger.warning("规则库初始化: %s", e)
        logger.info("🔍 妖币扫描器启动")
        while self.running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("妖币扫描器异常: %s", e, exc_info=True)
                scan_status["errors"].append(str(e)[:120])
                if len(scan_status["errors"]) > 20:
                    scan_status["errors"].pop(0)
            interval = int(config_manager.settings.get("YAOBI_SCAN_INTERVAL", 15))
            logger.info("🔍 下次扫描: %d 分钟后", interval)
            try:
                await asyncio.sleep(interval * 60)
            except asyncio.CancelledError:
                break
        self.running = False
        logger.info("🔍 妖币扫描器已停止")

    async def run_once(self) -> None:
        scan_status["scanning"] = True
        start = datetime.now()
        logger.info("🔍 妖币扫描开始 (%s)", start.strftime("%H:%M:%S"))

        candidates: dict[str, Candidate] = {}
        square_posts: list[dict] = []

        async with aiohttp.ClientSession(trust_env=True) as session:
            # ── 1. 期货符号缓存 ──────────────────────────────────────────────
            await self._refresh_futures(session)

            # ── 2. GeckoTerminal ─────────────────────────────────────────────
            gecko_tasks = [fetch_trending_pools(session)] + [
                fetch_new_pools(session, net) for net in _SCAN_NETWORKS[:3]
            ]
            gecko_results = await asyncio.gather(*gecko_tasks, return_exceptions=True)
            gecko_count = 0
            for idx, r in enumerate(gecko_results):
                if isinstance(r, list):
                    self._ingest_items(candidates, r,
                                       trend_start_rank=(1 if idx == 0 else 0))
                    gecko_count += len(r)
            scan_status["sources"]["gecko"] = {
                "count": gecko_count, "last_run": start.strftime("%H:%M"),
            }
            logger.info("🔍 GeckoTerminal: %d 个池子", gecko_count)

            # ── 3. DEX Screener ──────────────────────────────────────────────
            boost_r, profile_r = await asyncio.gather(
                fetch_token_boosts(session),
                fetch_latest_profiles(session),
                return_exceptions=True,
            )
            dex_count = 0
            for r in (boost_r, profile_r):
                if isinstance(r, list):
                    self._ingest_items(candidates, r)
                    dex_count += len(r)
            scan_status["sources"]["dexscreener"] = {
                "count": dex_count, "last_run": start.strftime("%H:%M"),
            }
            logger.info("🔍 DEX Screener: %d 个项目", dex_count)

            # ── 4. OKX 热门榜 ────────────────────────────────────────────────
            okx_hot_count = 0
            if config_manager.settings.get("YAOBI_OKX_ENABLED", True) and config_manager.settings.get("YAOBI_OKX_HOT_ENABLED", True):
                try:
                    hot_limit = int(config_manager.settings.get("YAOBI_OKX_HOT_LIMIT", 50))
                    chain_names = [
                        x.strip().lower()
                        for x in str(config_manager.settings.get("YAOBI_CHAINS", "")).split(",")
                        if x.strip()
                    ]
                    chain_ids = [CHAIN_IDS.get(x, "") for x in chain_names]
                    chain_ids = [x for x in chain_ids if x]
                    if not chain_ids:
                        chain_ids = ["501", "8453", "56", "1"]
                    okx_tasks = [get_hot_tokens(session, chain_index=cid, limit=hot_limit) for cid in chain_ids[:5]]
                    okx_results = await asyncio.gather(*okx_tasks, return_exceptions=True)
                    for r in okx_results:
                        if isinstance(r, list):
                            self._ingest_items(candidates, r)
                            okx_hot_count += len(r)
                    scan_status["sources"]["okx_hot"] = {
                        "count": okx_hot_count, "last_run": start.strftime("%H:%M"),
                    }
                    logger.info("🔍 OKX 热门榜: %d 个项目", okx_hot_count)
                except Exception as e:
                    logger.warning("OKX热门榜失败: %s", e)
            else:
                scan_status["sources"]["okx_hot"] = {
                    "count": 0, "last_run": start.strftime("%H:%M"), "disabled": True,
                }

            # ── 5. Binance Square ────────────────────────────────────────────
            if config_manager.settings.get("YAOBI_SQUARE_ENABLED", True):
                try:
                    square_rows = int(config_manager.settings.get("YAOBI_SQUARE_ROWS", 50))
                    square_posts = await fetch_hot_posts(session, rows=square_rows)
                    ticker_map = extract_ticker_mentions(square_posts)
                    self._apply_square_mentions(candidates, ticker_map)
                    scan_status["sources"]["binance_square"] = {
                        "count": len(square_posts), "last_run": start.strftime("%H:%M"),
                    }
                    logger.info("🔍 广场: %d 条热帖, 提及 %d 个币种", len(square_posts), len(ticker_map))
                except Exception as e:
                    logger.warning("广场抓取失败: %s", e)
            else:
                scan_status["sources"]["binance_square"] = {
                    "count": 0, "last_run": start.strftime("%H:%M"), "disabled": True,
                }

            # ── 6. Surf 新闻预过滤 ───────────────────────────────────────────
            if _surf_enabled() and config_manager.settings.get("YAOBI_SURF_ENABLED", True):
                cand_list = list(candidates.values())
                cand_list = await self._surf_news_filter(session, cand_list)
                # Rebuild dict from filtered list
                removed = set(candidates.keys()) - {c.key() for c in cand_list}
                for k in removed:
                    candidates.pop(k, None)
                logger.info("🔍 Surf新闻过滤: 剩余 %d 个候选 (淘汰 %d 个负面)", len(cand_list), len(removed))

            # ── 7. OKX Market 价格批量补全 ───────────────────────────────────
            enriched = 0
            enrich_targets = [
                c for c in candidates.values()
                if c.address and c.chain_id
            ]
            try:
                batch_data = await get_token_price_info_batch(
                    session,
                    [{"chainIndex": c.chain_id, "tokenContractAddress": c.address} for c in enrich_targets],
                )
                for c in enrich_targets:
                    info = batch_data.get(f"{c.chain_id}:{c.address}")
                    if not info:
                        continue
                    c.price_usd      = info["price_usd"]      or c.price_usd
                    c.market_cap     = info["market_cap"]      or c.market_cap
                    c.liquidity      = info["liquidity"]       or c.liquidity
                    c.holder_count   = info["holder_count"]    or c.holder_count
                    c.price_change_1h  = info["price_change_1h"]
                    c.price_change_4h  = info["price_change_4h"]
                    c.price_change_24h = info["price_change_24h"] or c.price_change_24h
                    c.volume_24h       = info["volume_24h"]    or c.volume_24h
                    txs_now = info.get("tx_count_5m", 0)
                    if c.txs_5m > 0 and txs_now > 0:
                        c.txs_5m_accel = txs_now / c.txs_5m
                    c.txs_5m = txs_now
                    enriched += 1
            except Exception as e:
                logger.debug("OKX批量补全异常: %s", e)
            logger.info("🔍 OKX 补充 %d 个链上数据", enriched)

            # ── 8. DEX Screener 价格兜底 ─────────────────────────────────────
            needs_detail = [
                c for c in candidates.values()
                if c.price_usd == 0 and c.address
            ]
            for c in needs_detail[:20]:
                try:
                    detail = await fetch_token_detail(session, c.chain, c.address)
                    if detail:
                        c.price_usd        = detail.get("price_usd")        or c.price_usd
                        c.price_change_1h  = detail.get("price_change_1h")  or c.price_change_1h
                        c.price_change_24h = detail.get("price_change_24h") or c.price_change_24h
                        c.volume_24h       = detail.get("volume_24h")       or c.volume_24h
                        c.liquidity        = detail.get("liquidity")        or c.liquidity
                        c.market_cap       = detail.get("market_cap")       or c.market_cap
                        c.links.update(detail.get("links", {}))
                    await asyncio.sleep(0.05)
                except Exception:
                    pass

        # ── 9. Binance 期货标记 ──────────────────────────────────────────────
        for c in candidates.values():
            if (c.symbol.upper() + "USDT") in self._futures_symbols:
                c.has_futures = True

        # ── 10. 聪明钱启发式检测 ─────────────────────────────────────────────
        for c in candidates.values():
            if (c.liquidity > 50_000
                    and c.volume_24h > 0
                    and c.volume_24h / c.liquidity > 2.0
                    and c.price_change_24h > 15
                    and c.gecko_trend_rank > 0):
                c.smart_money_signal = True
                c.smart_money_detail = (
                    f"量/池={c.volume_24h / c.liquidity:.1f}x "
                    f"趋势榜#{c.gecko_trend_rank}"
                )

        # ── 11. 合约OI扫描 + 资金费率 + 散户多空 ────────────────────────────
        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                top_n = int(config_manager.settings.get("YAOBI_FUTURES_TOP_N", 120))
                futures_items = await scan_futures_oi(session, top_n=top_n)
                fut_new = fut_enrich = 0
                for item in futures_items:
                    sym = item.get("symbol", "")
                    if not sym:
                        continue
                    existing = candidates.get(sym)
                    if existing:
                        existing.oi_change_24h_pct = item.get("oi_change_24h_pct", 0)
                        existing.oi_change_3d_pct  = item.get("oi_change_3d_pct", 0)
                        existing.oi_change_7d_pct  = item.get("oi_change_7d_pct", 0)
                        existing.oi_acceleration   = item.get("oi_acceleration",   0)
                        existing.oi_flat_days      = item.get("oi_flat_days",      0)
                        existing.oi_trend_grade    = item.get("oi_trend_grade",    "")
                        existing.oi_consistency_score = item.get("oi_consistency_score", 0)
                        existing.ema_deviation_pct = item.get("ema_deviation_pct", 0)
                        existing.volume_ratio      = item.get("volume_ratio",      1)
                        existing.whale_long_ratio  = item.get("whale_long_ratio",  0.5)
                        existing.short_crowd_pct   = item.get("short_crowd_pct",  50)
                        existing.category          = item.get("category",          "")
                        existing.has_futures       = True
                        existing.funding_rate_pct  = item.get("funding_rate_pct",  0.0)
                        existing.fr_extreme_short  = item.get("fr_extreme_short",  False)
                        existing.retail_short_pct  = item.get("retail_short_pct",  50.0)
                        if "binance_futures" not in existing.sources:
                            existing.sources.append("binance_futures")
                        fut_enrich += 1
                    else:
                        c = Candidate(
                            symbol=sym,
                            name=sym,
                            price_usd=item.get("price_usd", 0),
                            price_change_24h=item.get("price_change_24h", 0),
                            has_futures=True,
                            sources=["binance_futures"],
                            oi_change_24h_pct=item.get("oi_change_24h_pct", 0),
                            oi_change_3d_pct=item.get("oi_change_3d_pct", 0),
                            oi_change_7d_pct=item.get("oi_change_7d_pct", 0),
                            oi_acceleration=item.get("oi_acceleration",   0),
                            oi_flat_days=item.get("oi_flat_days",      0),
                            oi_trend_grade=item.get("oi_trend_grade", ""),
                            oi_consistency_score=item.get("oi_consistency_score", 0),
                            ema_deviation_pct=item.get("ema_deviation_pct", 0),
                            volume_ratio=item.get("volume_ratio",      1),
                            whale_long_ratio=item.get("whale_long_ratio",  0.5),
                            short_crowd_pct=item.get("short_crowd_pct",  50),
                            category=item.get("category", ""),
                            signals=item.get("signals", []),
                            funding_rate_pct=item.get("funding_rate_pct", 0.0),
                            fr_extreme_short=item.get("fr_extreme_short", False),
                            retail_short_pct=item.get("retail_short_pct", 50.0),
                        )
                        candidates[sym] = c
                        fut_new += 1
                scan_status["sources"]["binance_futures"] = {
                    "count": len(futures_items), "last_run": start.strftime("%H:%M"),
                }
                logger.info("🔍 合约OI: %d 个信号 (新增%d 补充%d)", len(futures_items), fut_new, fut_enrich)
            except Exception as e:
                logger.warning("合约OI扫描失败: %s", e)

            # ── 12. OKX 多链验证 + 大单/风险分析 (限额内挑高价值合约品种) ─────
            okx_top_n = int(config_manager.settings.get("YAOBI_OKX_HEAVY_TOP_N", 40))
            futures_candidates = sorted(
                [c for c in candidates.values() if c.has_futures],
                key=lambda x: (
                    x.oi_change_24h_pct,
                    x.volume_ratio,
                    x.square_mentions,
                    x.price_change_24h,
                ),
                reverse=True,
            )[:okx_top_n]
            if futures_candidates:
                sem = asyncio.Semaphore(5)
                async def _enrich_okx_one(c: Candidate) -> None:
                    async with sem:
                        await self._enrich_okx(session, c)

                await asyncio.gather(*[_enrich_okx_one(c) for c in futures_candidates],
                                     return_exceptions=True)
                logger.info("🔍 OKX链验证: %d 个合约品种已分析", len(futures_candidates))

        # ── 13. 首轮打分 ─────────────────────────────────────────────────────
        min_score = int(config_manager.settings.get("YAOBI_MIN_SCORE", 30))
        all_candidates = list(candidates.values())
        scored: list[Candidate] = []
        for c in all_candidates:
            c = score_candidate(c)
            if c.score >= min_score:
                scored.append(c)
        scored.sort(key=lambda x: x.score, reverse=True)

        # ── 14. Surf AI 终审 (Top-N) ─────────────────────────────────────────
        surf_top_n = int(config_manager.settings.get("YAOBI_SURF_TOP_N", 5))
        if _surf_enabled() and config_manager.settings.get("YAOBI_SURF_ENABLED", True) and scored:
            top_candidates = [c for c in scored[:surf_top_n] if c.score >= 55]
            if top_candidates:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    await self._surf_ai_review(session, top_candidates)
                # Re-score after AI review (apply -30 if HIGH)
                for c in top_candidates:
                    c = score_candidate(c)
                scored.sort(key=lambda x: x.score, reverse=True)
                logger.info("🔍 Surf AI终审: %d 个候选已审查", len(top_candidates))

        # ── 15. 异常币/情绪雷达 (OKX market-filter/sentiment-tracker 风格) ───
        self._apply_anomaly_radar(all_candidates)
        self._apply_decision_cards(all_candidates)
        min_anomaly = int(config_manager.settings.get("YAOBI_MIN_ANOMALY_SCORE", 35))
        anomaly_count = sum(1 for c in all_candidates if c.anomaly_score >= min_anomaly)
        scored = [
            c for c in all_candidates
            if c.score >= min_score or c.anomaly_score >= min_anomaly
        ]
        scored.sort(key=lambda x: (x.score, x.anomaly_score), reverse=True)

        # ── 16. 写入候选库 ───────────────────────────────────────────────────
        for c in scored:
            upsert_candidate(c)

        elapsed = (datetime.now() - start).total_seconds()
        scan_status.update({
            "scanning":      False,
            "last_scan":     start.strftime("%Y-%m-%d %H:%M:%S"),
            "total_scanned": len(candidates),
            "scored":        len(scored),
            "anomalies":     anomaly_count,
            "min_anomaly":   min_anomaly,
        })
        logger.info(
            "🔍 扫描完成: %d 个候选, %d 个入库 (评分≥%d 或异动≥%d), 异常%d个, 耗时 %.1fs",
            len(candidates), len(scored), min_score, min_anomaly, anomaly_count, elapsed,
        )

        # ── 17. Obsidian 日报 (每天一次) ────────────────────────────────────
        today = date.today()
        if self._last_obsidian_date != today and scored:
            try:
                dicts = [c.to_dict() for c in scored[:30]]
                write_daily_candidates(dicts)
                write_daily_digest(dicts, square_posts[:10])
                if square_posts:
                    ticker_map_arch = extract_ticker_mentions(square_posts)
                    write_square_archive(square_posts, ticker_map_arch)
                for c in scored[:10]:
                    write_token_doc(c.to_dict())
                self._last_obsidian_date = today
                logger.info("📝 Obsidian 日报写入完成")
            except Exception as e:
                logger.error("Obsidian 写入失败: %s", e)

    # ── Surf 新闻预过滤 ────────────────────────────────────────────────────────

    async def _surf_news_filter(
        self,
        session: aiohttp.ClientSession,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        """批量查询 Surf 新闻，标记情绪，过滤掉负面新闻的候选。"""
        def _classify(text: str) -> str:
            lower = text.lower()
            for kw in _NEG_KW:
                idx = lower.find(kw)
                while idx != -1:
                    window = lower[max(0, idx - 60): idx]
                    if not any(n in window for n in _NEG_GATES):
                        return "negative"
                    idx = lower.find(kw, idx + len(kw))
            if any(k in lower for k in _POS_KW):
                return "positive"
            return "neutral"

        def _chunks(rows: list[str], size: int) -> list[list[str]]:
            return [rows[i:i + size] for i in range(0, len(rows), size)]

        lookup_terms: list[str] = []
        for c in candidates:
            lookup_terms.extend(surf_project_terms(c.symbol, c.name)[:2])
        lookup_terms = list(dict.fromkeys(lookup_terms))

        items: list[dict] = []
        for chunk in _chunks(lookup_terms, 25):
            items.extend(await surf_fetch_news_feed(session, projects=chunk, limit=50))
            await asyncio.sleep(0.05)

        fallback_searches = 0
        for c in candidates:
            matched = [
                item for item in items
                if surf_news_matches_symbol(item, c.symbol, c.name)
            ][:5]
            if (not matched
                    and fallback_searches < 20
                    and (abs(c.price_change_24h or 0.0) >= 30 or c.square_mentions >= 8)):
                query = " ".join(surf_project_terms(c.symbol, c.name)[:2])
                matched = await surf_search_news(session, query)
                fallback_searches += 1
            if not matched:
                continue
            c.surf_news_titles = [i.get("title", "") for i in matched[:3]]
            combined = " ".join((i.get("text") or "").lower() for i in matched)
            c.surf_news_sentiment = _classify(combined)
        return [c for c in candidates if c.surf_news_sentiment != "negative"]

    # ── OKX 多链验证 + 大单分析 ────────────────────────────────────────────────

    async def _enrich_okx(self, session: aiohttp.ClientSession, c: Candidate) -> None:
        """通过 OKX API 验证代币在多链的部署情况和机构大单占比。"""
        if c.chain_id:
            c.okx_chain_count = max(c.okx_chain_count, 1)
            if not c.okx_chains_found:
                c.okx_chains_found = [CHAIN_NAMES.get(c.chain_id, c.chain or c.chain_id)]

        if not (c.address and c.chain_id):
            try:
                tokens = await search_tokens(session, c.symbol, limit=10)
                if tokens:
                    if "okx_search" not in c.sources:
                        c.sources.append("okx_search")
                    chains = list({t.get("chain_id", "") for t in tokens if t.get("chain_id")})
                    c.okx_chain_count  = len(chains)
                    c.okx_chains_found = [CHAIN_NAMES.get(ch, ch) for ch in chains]
                    primary = max(
                        tokens,
                        key=lambda t: (t.get("liquidity", 0), t.get("market_cap", 0), t.get("volume_24h", 0)),
                    )
                    c.chain_id = primary.get("chain_id", c.chain_id)
                    c.chain = primary.get("chain", c.chain)
                    c.address = primary.get("address", c.address)
                    c.liquidity = primary.get("liquidity", 0) or c.liquidity
                    c.market_cap = primary.get("market_cap", 0) or c.market_cap
                    c.holder_count = primary.get("holder_count", 0) or c.holder_count
            except Exception:
                pass

        if c.address and c.chain_id:
            try:
                trade_info = await get_token_trades(session, c.chain_id, c.address)
                c.okx_large_trade_pct = trade_info.get("large_trade_pct", 0.0)
                c.okx_buy_ratio = trade_info.get("buy_ratio", 0.0)
            except Exception:
                pass
            try:
                adv = await get_token_advanced_info(session, c.chain_id, c.address)
                if adv:
                    c.okx_risk_level = adv.get("risk_control_level", 0)
                    c.okx_token_tags = adv.get("token_tags", [])
                    c.okx_top10_hold_pct = adv.get("top10_hold_pct", 0.0)
                    c.okx_dev_hold_pct = adv.get("dev_hold_pct", 0.0)
                    c.okx_lp_burned_pct = adv.get("lp_burned_pct", 0.0)
            except Exception:
                pass
            try:
                smart_holders = await get_token_holders(session, c.chain_id, c.address, tag_filter="3", limit=20)
                c.okx_smart_money_holders = smart_holders.get("smart_money_count", 0)
            except Exception:
                pass

    # ── Surf AI 终审 ───────────────────────────────────────────────────────────

    async def _surf_ai_review(
        self,
        session: aiohttp.ClientSession,
        candidates: list[Candidate],
    ) -> None:
        """对高分候选进行 Surf AI 风险审查，设置 surf_ai_risk_level。"""
        async def _review(c: Candidate) -> None:
            if not _surf_enabled():
                record_provider_skip("surf", "chat/completions", "missing_surf_api_key")
                return
            prompt = (
                f"You are a crypto token risk analyst. Evaluate {c.symbol} as a speculative trade candidate.\n"
                f"Data: price_change_24h={c.price_change_24h:+.1f}%, "
                f"oi_change_24h={c.oi_change_24h_pct:+.1f}%, "
                f"volume_ratio={c.volume_ratio:.1f}x, "
                f"funding_rate={c.funding_rate_pct:.4f}%, "
                f"retail_short={c.retail_short_pct:.0f}%, "
                f"score={c.score}\n"
                f"Recent news: {'; '.join(c.surf_news_titles[:2]) or 'none'}\n"
                f"Answer ONLY with JSON: "
                f'{{\"score\":0-100,\"risk\":\"LOW|MEDIUM|HIGH\",\"reason\":\"max 12 words\"}}'
            )
            try:
                ok_resp, text, status = await surf_chat_completion(
                    session,
                    prompt,
                    timeout_sec=18,
                    reasoning_effort="low",
                )
                if not ok_resp:
                    logger.debug("Surf AI 审查失败 [%s]: HTTP %s", c.symbol, status)
                    return
                if "```" in text:
                    text = text.split("```")[1].lstrip("json").strip()
                result = json.loads(text)
                c.surf_ai_score      = max(0, min(100, int(result.get("score", 50))))
                c.surf_ai_risk_level = result.get("risk", "")
                c.surf_ai_reason     = result.get("reason", "")
                if c.surf_ai_risk_level == "HIGH":
                    logger.info("🔍 [%s] Surf AI 高风险: %s", c.symbol, c.surf_ai_reason)
            except Exception as e:
                record_provider_call("surf", "chat/completions", False, status="exception", error=f"{type(e).__name__}: {e}")
                logger.debug("Surf AI 审查异常 [%s]: %s", c.symbol, e)

        sem = asyncio.Semaphore(3)
        async def bounded(c: Candidate) -> None:
            async with sem:
                await _review(c)

        await asyncio.gather(*[bounded(c) for c in candidates], return_exceptions=True)

    # ── 异常币/情绪雷达 ───────────────────────────────────────────────────────

    def _apply_anomaly_radar(self, candidates: list[Candidate]) -> None:
        """把 OKX/Binance/Surf/广场数据聚合成一张“今天哪些币不寻常”的雷达表。"""
        for c in candidates:
            score = 0
            tags: list[str] = []
            reasons: list[str] = []

            def add(points: int, tag: str, reason: str = "") -> None:
                nonlocal score
                score += points
                if tag and tag not in tags:
                    tags.append(tag)
                if reason:
                    reasons.append(reason)

            oi = float(c.oi_change_24h_pct or 0)
            oi_abs = abs(oi)
            if oi >= 150:
                add(24, "OI爆增", f"OI {oi:+.1f}%")
            elif oi >= 80:
                add(18, "OI大增", f"OI {oi:+.1f}%")
            elif oi >= 30:
                add(10, "OI异动", f"OI {oi:+.1f}%")
            elif oi <= -25:
                add(9, "OI骤降", f"OI {oi:+.1f}%")

            if c.oi_acceleration >= 30:
                add(10, "OI加速", f"加速 {c.oi_acceleration:+.1f}%")
            elif c.oi_acceleration >= 15:
                add(5, "OI加速")

            if c.oi_trend_grade == "S":
                add(14, "S级OI趋势", f"7D {c.oi_change_7d_pct:+.1f}% / 3D {c.oi_change_3d_pct:+.1f}%")
            elif c.oi_trend_grade == "A":
                add(9, "A级OI趋势", f"7D {c.oi_change_7d_pct:+.1f}%")
            elif c.oi_trend_grade == "B":
                add(5, "B级OI趋势")
            elif c.oi_trend_grade == "RISK":
                add(8, "OI趋势风险", f"7D {c.oi_change_7d_pct:+.1f}%")

            if c.volume_ratio >= 20:
                add(12, "极端放量", f"成交量 {c.volume_ratio:.1f}x")
            elif c.volume_ratio >= 10:
                add(9, "明显放量", f"成交量 {c.volume_ratio:.1f}x")
            elif c.volume_ratio >= 5:
                add(5, "放量")

            price_24h = float(c.price_change_24h or 0)
            if price_24h >= 80:
                add(16, "价格暴涨", f"价格 {price_24h:+.1f}%")
            elif price_24h >= 30:
                add(10, "价格强势", f"价格 {price_24h:+.1f}%")
            elif price_24h <= -30:
                add(10, "价格暴跌", f"价格 {price_24h:+.1f}%")
            elif abs(price_24h) >= 12:
                add(5, "价格异动")

            if abs(c.price_change_1h or 0) >= 10:
                add(8, "1H急动", f"1H {c.price_change_1h:+.1f}%")
            elif abs(c.price_change_4h or 0) >= 20:
                add(6, "4H急动", f"4H {c.price_change_4h:+.1f}%")

            fr = float(c.funding_rate_pct or c.funding_rate or 0)
            if fr <= -0.05 or c.fr_extreme_short:
                add(12, "极端负FR", f"FR {fr:+.4f}%")
            elif fr >= 0.08:
                add(8, "高正FR", f"FR {fr:+.4f}%")
            elif abs(fr) >= 0.03:
                add(4, "资金费率异动")

            if c.retail_short_pct >= 70:
                add(10, "散户极空", f"散户空 {c.retail_short_pct:.0f}%")
            elif c.retail_short_pct >= 62:
                add(6, "散户偏空")

            whale_long = float(c.whale_long_ratio or 0.5)
            if c.has_futures:
                if whale_long >= 0.65:
                    add(7, "大户偏多", f"大户多 {whale_long * 100:.0f}%")
                elif whale_long <= 0.40:
                    add(7, "大户偏空", f"大户空 {(1 - whale_long) * 100:.0f}%")

            if c.okx_large_trade_pct >= 0.30:
                add(12, "OKX大单", f"大单 {c.okx_large_trade_pct * 100:.0f}%")
            elif c.okx_large_trade_pct >= 0.15:
                add(6, "OKX大单")

            if c.okx_buy_ratio >= 0.70:
                add(8, "OKX买盘", f"买盘 {c.okx_buy_ratio * 100:.0f}%")
            elif c.okx_buy_ratio <= 0.35 and c.okx_buy_ratio > 0:
                add(6, "OKX卖压", f"买盘 {c.okx_buy_ratio * 100:.0f}%")

            if c.okx_smart_money_holders >= 3:
                add(8, "聪明钱持仓", f"聪明钱 {c.okx_smart_money_holders}")
            elif c.okx_smart_money_holders >= 1:
                add(4, "聪明钱")

            if c.okx_top10_hold_pct >= 75:
                add(7, "筹码集中", f"Top10 {c.okx_top10_hold_pct:.0f}%")
            elif c.okx_top10_hold_pct >= 60:
                add(4, "筹码集中")

            if c.txs_5m_accel >= 2.0:
                add(8, "链上Tx加速", f"Tx {c.txs_5m_accel:.1f}x")
            elif c.txs_5m_accel >= 1.5:
                add(4, "链上活跃")

            square_heat = int(c.square_mentions or 0)
            news_heat = len(c.surf_news_titles or [])
            if square_heat >= 20:
                add(12, "情绪热", f"广场 {square_heat}条")
            elif square_heat >= 8:
                add(8, "情绪升温", f"广场 {square_heat}条")
            elif square_heat >= 3:
                add(4, "情绪升温")

            if news_heat >= 3:
                add(6, "新闻密集", f"新闻 {news_heat}条")
            elif news_heat:
                add(2, "新闻出现")

            sentiment_score = 0
            if c.surf_news_sentiment == "positive":
                add(6, "Surf正面")
                sentiment_score += 30
            elif c.surf_news_sentiment == "negative":
                add(6, "Surf负面")
                sentiment_score -= 35
            if c.surf_ai_risk_level == "HIGH":
                add(6, "Surf高风险")
                sentiment_score -= 25
            elif c.surf_ai_score >= 70:
                sentiment_score += 15

            sentiment_score += min(square_heat * 2, 20)
            if c.retail_short_pct >= 62:
                sentiment_score -= min(int(c.retail_short_pct - 55), 20)
            if whale_long >= 0.60:
                sentiment_score += 12
            elif whale_long <= 0.40:
                sentiment_score -= 12
            if fr <= -0.05:
                sentiment_score -= 12
            elif fr >= 0.05:
                sentiment_score += 8

            c.sentiment_heat = square_heat + news_heat + len(c.square_posts or [])
            c.sentiment_score = max(-100, min(100, sentiment_score))
            if c.sentiment_score >= 20:
                c.sentiment_label = "bullish"
            elif c.sentiment_score <= -20:
                c.sentiment_label = "bearish"
            else:
                c.sentiment_label = "neutral"

            if c.has_futures:
                long_pct = whale_long * 100
                if abs(long_pct - 50) < 0.1 and c.retail_short_pct:
                    long_pct = max(0.0, min(100.0, 100.0 - c.retail_short_pct))
                c.long_short_text = f"多{long_pct:.0f}%/空{100.0 - long_pct:.0f}%"
            else:
                c.long_short_text = "—/—"

            holder_parts: list[str] = []
            if c.okx_top10_hold_pct:
                holder_parts.append(f"Top10 {c.okx_top10_hold_pct:.0f}%")
            if c.okx_dev_hold_pct:
                holder_parts.append(f"Dev {c.okx_dev_hold_pct:.1f}%")
            if c.okx_smart_money_holders:
                holder_parts.append(f"聪明钱 {c.okx_smart_money_holders}")
            c.holder_signal = " / ".join(holder_parts[:3])

            if c.okx_risk_level >= 4 and "OKX高风险" not in tags:
                tags.append("OKX高风险")
            if c.okx_token_tags:
                for tag in c.okx_token_tags[:2]:
                    label = f"OKX:{tag}"
                    if label not in tags:
                        tags.append(label)

            # OI和价格同时剧烈变化时再加一点综合分，避免单一维度误报。
            if oi_abs >= 30 and abs(price_24h) >= 10:
                score += 6
            if c.has_futures and (square_heat or news_heat) and oi_abs >= 20:
                score += 4

            c.anomaly_score = max(0, min(100, int(round(score))))
            c.anomaly_tags = tags[:8]
            c.market_filter_note = " | ".join(reasons[:4]) or (
                " / ".join(tags[:3]) if tags else ""
            )

            for tag in c.anomaly_tags[:4]:
                label = f"异动:{tag}"
                if label not in c.signals:
                    c.signals.append(label)

    def _apply_decision_cards(self, candidates: list[Candidate]) -> None:
        """生成一张可解释的人工决策卡；只作筛选/复盘，不替代交易策略。"""
        for c in candidates:
            reasons: list[str] = []
            risks: list[str] = []
            missing: list[str] = []
            confidence = 0

            if c.score >= 70:
                reasons.append(f"综合评分{c.score}")
                confidence += 18
            elif c.score >= 50:
                reasons.append(f"评分{c.score}，可继续观察")
                confidence += 10
            else:
                missing.append("综合评分不足50")

            if c.anomaly_score >= 60:
                reasons.append(f"异动指数{c.anomaly_score}")
                confidence += 18
            elif c.anomaly_score >= 35:
                reasons.append(f"异动指数{c.anomaly_score}")
                confidence += 10
            else:
                missing.append("异动指数不足")

            if c.oi_trend_grade in {"S", "A"}:
                reasons.append(f"{c.oi_trend_grade}级OI趋势")
                confidence += 18 if c.oi_trend_grade == "S" else 12
            elif c.oi_trend_grade == "B":
                reasons.append("B级OI趋势")
                confidence += 6
            elif c.has_futures:
                missing.append("OI趋势未形成S/A")

            if c.has_futures:
                if c.retail_short_pct >= 65 and c.funding_rate_pct <= -0.03:
                    reasons.append("空头拥挤+负资金费率")
                    confidence += 10
                if c.whale_long_ratio >= 0.60:
                    reasons.append("大户偏多")
                    confidence += 6
                elif c.whale_long_ratio <= 0.40:
                    risks.append("大户偏空")

            if c.sentiment_label == "bullish":
                reasons.append("情绪偏多")
                confidence += 8
            elif c.sentiment_label == "bearish":
                risks.append("情绪偏空")
            else:
                missing.append("情绪未确认")

            if c.okx_large_trade_pct >= 0.15:
                reasons.append(f"OKX大单{c.okx_large_trade_pct * 100:.0f}%")
                confidence += 8
            elif c.has_futures and not c.address:
                missing.append("链上大单未确认")

            if c.category == "风险":
                risks.append("候选分类为风险")
            if c.surf_ai_risk_level == "HIGH":
                risks.append("Surf AI高风险")
            if c.surf_news_sentiment == "negative":
                risks.append("Surf负面新闻")
            if c.okx_risk_level >= 4:
                risks.append("OKX高风险")
            if c.okx_top10_hold_pct >= 75:
                risks.append(f"Top10持仓{c.okx_top10_hold_pct:.0f}%")
            if c.price_change_24h >= 80 and c.oi_trend_grade not in {"S", "A"}:
                risks.append("24h涨幅过快但OI趋势不够强")
            if c.price_change_24h <= -30:
                risks.append("24h大跌")

            if risks and any(x in " ".join(risks) for x in ("高风险", "负面", "风险")):
                action = "禁止交易"
            elif c.category == "风险":
                action = "禁止交易"
            elif confidence >= 55 and not risks:
                action = "允许交易"
            elif confidence >= 30:
                action = "等待确认"
            else:
                action = "观察"

            c.decision_action = action
            c.decision_confidence = max(0, min(100, confidence - len(risks) * 8))
            c.decision_reasons = reasons[:5]
            c.decision_risks = risks[:5]
            c.decision_missing = missing[:5]
            c.decision_note = (
                "；".join((reasons or missing or ["暂无确认"])[0:2])
                + (f"；风险: {risks[0]}" if risks else "")
            )

    # ── 内部辅助 ─────────────────────────────────────────────────────────────

    def _ingest_items(
        self,
        candidates: dict[str, Candidate],
        items: list[dict],
        trend_start_rank: int = 0,
    ) -> None:
        for rank, item in enumerate(items, 1):
            try:
                sym = (item.get("symbol") or "").upper().strip()
                if not sym or len(sym) < 2 or len(sym) > 12:
                    continue
                chain_id = item.get("chain_id", "")
                raw_address = str(item.get("address") or "").strip()
                address  = raw_address if str(chain_id) in ("501", "784", "607") else raw_address.lower()
                src      = item.get("source", "unknown")

                key = f"{chain_id}:{address}" if (chain_id and address) else sym

                if key in candidates:
                    c = candidates[key]
                    if src and src not in c.sources:
                        c.sources.append(src)
                    if trend_start_rank and "gecko_trend" in src and c.gecko_trend_rank == 0:
                        c.gecko_trend_rank = rank
                    if "dex_boost" in src and c.dex_boost_rank == 0:
                        c.dex_boost_rank = item.get("boost_rank", rank)
                    if c.price_usd == 0:
                        c.price_usd = float(item.get("price_usd", 0))
                    if c.volume_24h == 0:
                        c.volume_24h = float(item.get("volume_24h", 0))
                    if c.liquidity == 0:
                        c.liquidity = float(item.get("liquidity", 0))
                    c.links.update(item.get("links", {}))
                else:
                    c = Candidate(
                        symbol=sym,
                        name=item.get("name", sym),
                        chain=item.get("chain", ""),
                        chain_id=chain_id,
                        address=address,
                        price_usd=float(item.get("price_usd", 0)),
                        price_change_24h=float(item.get("price_change_24h", 0)),
                        volume_24h=float(item.get("volume_24h", 0)),
                        liquidity=float(item.get("liquidity", 0)),
                        market_cap=float(item.get("market_cap", 0)),
                        holder_count=int(item.get("holder_count", 0)),
                        sources=[src] if src else [],
                        logo_url=item.get("logo_url", ""),
                        tags=item.get("tags", []),
                        links=item.get("links", {}),
                    )
                    if trend_start_rank and "gecko_trend" in src:
                        c.gecko_trend_rank = rank
                    if "dex_boost" in src:
                        c.dex_boost_rank = item.get("boost_rank", rank)
                    candidates[key] = c
            except Exception as e:
                logger.debug("候选合并异常: %s", e)

    def _apply_square_mentions(
        self,
        candidates: dict[str, Candidate],
        ticker_map: dict,
    ) -> None:
        for sym, data in ticker_map.items():
            found = False
            for c in candidates.values():
                if c.symbol.upper() == sym:
                    c.square_mentions += data["count"]
                    c.square_posts.extend(data.get("posts", []))
                    if "binance_square" not in c.sources:
                        c.sources.append("binance_square")
                    found = True
                    break
            if not found and data["count"] >= 3:
                c = Candidate(
                    symbol=sym,
                    name=sym,
                    sources=["binance_square"],
                    square_mentions=data["count"],
                    square_posts=data.get("posts", []),
                )
                candidates[sym] = c

    async def _refresh_futures(self, session: aiohttp.ClientSession) -> None:
        now = datetime.now()
        if (self._futures_cached_at
                and (now - self._futures_cached_at).total_seconds() < 21600):
            return
        try:
            async with session.get(
                _FUTURES_URL, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    syms = {
                        s["symbol"]
                        for s in data.get("symbols", [])
                        if s.get("contractType") == "PERPETUAL"
                        and s.get("quoteAsset") == "USDT"
                    }
                    self._futures_symbols = syms
                    self._futures_cached_at = now
                    logger.debug("期货符号缓存: %d 个", len(syms))
        except Exception as e:
            logger.warning("期货符号刷新失败: %s", e)
