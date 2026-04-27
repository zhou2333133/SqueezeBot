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
from datetime import datetime, date, timedelta
from typing import Optional

import aiohttp

from config import config_manager, surf_credentials_status
from scanner.candidates import (
    Candidate, upsert_candidate, scan_status, candidates_map, clear_candidates,
    set_opportunity_queue,
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
from scanner.sources.binance_futures import scan_futures_oi, scan_short_term_intel
from scanner.sources.binance_liquidations import collect_liquidations, liquidation_stats
from scanner.ai_gateway import analyze_opportunities, provider_status as ai_provider_status
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
        self._liquidation_task: asyncio.Task | None = None

    async def run(self) -> None:
        self.running = True
        if config_manager.settings.get("YAOBI_BINANCE_LIQUIDATION_WS_ENABLED", True):
            self._liquidation_task = asyncio.create_task(
                collect_liquidations(lambda: self.running and bool(config_manager.settings.get("YAOBI_BINANCE_LIQUIDATION_WS_ENABLED", True)))
            )
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
        if self._liquidation_task and not self._liquidation_task.done():
            self._liquidation_task.cancel()
            try:
                await self._liquidation_task
            except asyncio.CancelledError:
                pass
        self._liquidation_task = None
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
            if (_surf_enabled()
                    and config_manager.settings.get("YAOBI_SURF_ENABLED", True)
                    and config_manager.settings.get("YAOBI_SURF_NEWS_ENABLED", False)):
                all_cand_list = list(candidates.values())
                news_top_n = int(config_manager.settings.get("YAOBI_SURF_NEWS_TOP_N", 20))
                news_targets = sorted(
                    all_cand_list,
                    key=lambda c: (
                        abs(c.price_change_24h or 0.0),
                        c.square_mentions,
                        c.gecko_trend_rank > 0,
                        c.dex_boost_rank > 0,
                    ),
                    reverse=True,
                )[:max(1, news_top_n)]
                kept_targets = await self._surf_news_filter(session, news_targets)
                removed = {c.key() for c in news_targets} - {c.key() for c in kept_targets}
                for k in removed:
                    candidates.pop(k, None)
                scan_status["sources"]["surf_news"] = {
                    "count": len(news_targets), "last_run": start.strftime("%H:%M"),
                }
                logger.info("🔍 Surf新闻过滤: 审查%d个 剩余%d个候选 (淘汰%d个负面)",
                            len(news_targets), len(candidates), len(removed))
            else:
                scan_status["sources"]["surf_news"] = {
                    "count": 0, "last_run": start.strftime("%H:%M"), "disabled": True,
                }

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

            # ── 11b. Binance 原生短线情报：5m/15m OI、Taker、多空比、强平 ──
            if config_manager.settings.get("YAOBI_BINANCE_SHORT_INTEL_ENABLED", True):
                try:
                    top_n = int(config_manager.settings.get("YAOBI_FUTURES_TOP_N", 120))
                    short_items = await scan_short_term_intel(session, top_n=top_n)
                    short_new, short_enrich = self._apply_short_term_intel(candidates, short_items)
                    self._apply_liquidation_stats(candidates)
                    scan_status["sources"]["binance_short_intel"] = {
                        "count": len(short_items), "last_run": start.strftime("%H:%M"),
                    }
                    logger.info(
                        "🔍 Binance短线情报: %d 个合约 (新增%d 补充%d)",
                        len(short_items), short_new, short_enrich,
                    )
                except Exception as e:
                    logger.warning("Binance短线情报失败: %s", e)
            else:
                scan_status["sources"]["binance_short_intel"] = {
                    "count": 0, "last_run": start.strftime("%H:%M"), "disabled": True,
                }

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
        surf_top_n = int(config_manager.settings.get("YAOBI_SURF_TOP_N", 1))
        if (config_manager.settings.get("YAOBI_AI_ENABLED", False)
                and config_manager.settings.get("YAOBI_DUAL_AI_CONSENSUS_REQUIRED", False)):
            surf_top_n = max(
                surf_top_n,
                int(config_manager.settings.get("YAOBI_AI_MAX_SYMBOLS_PER_RUN", 6) or 6),
            )
        if (_surf_enabled()
                and config_manager.settings.get("YAOBI_SURF_ENABLED", True)
                and config_manager.settings.get("YAOBI_SURF_AI_ENABLED", False)
                and scored):
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
        await self._apply_opportunity_queue(all_candidates)
        min_anomaly = int(config_manager.settings.get("YAOBI_MIN_ANOMALY_SCORE", 35))
        anomaly_count = sum(1 for c in all_candidates if c.anomaly_score >= min_anomaly)
        scored = [
            c for c in all_candidates
            if c.score >= min_score or c.anomaly_score >= min_anomaly
        ]
        sticky = [
            c for c in all_candidates
            if self._is_watch_action(c.opportunity_action)
            or c.opportunity_permission in {"ALLOW_IF_1M_SIGNAL", "BLOCK"}
            or c.opportunity_setup_state in {"ARMED", "HOT", "BLOCK"}
        ]
        merged_scored = {c.key(): c for c in scored}
        for c in sticky:
            merged_scored[c.key()] = c
        scored = list(merged_scored.values())
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

        # ── 16b. 妖币雷达 Telegram/Discord 推送（best-effort, 不阻塞）─────────
        try:
            from .notifier import push_yaobi_scan_summary
            await push_yaobi_scan_summary([c.to_dict() for c in scored])
        except Exception as e:
            logger.debug("📡 妖币雷达推送异常（已忽略）: %r", e)

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

    # ── Binance 短线情报 + 机会队列 ───────────────────────────────────────────

    @staticmethod
    def _find_candidate_by_symbol(candidates: dict[str, Candidate], symbol: str) -> Candidate | None:
        base = str(symbol or "").upper().replace("USDT", "")
        if not base:
            return None
        if base in candidates:
            return candidates[base]
        for c in candidates.values():
            if c.symbol.upper().replace("USDT", "") == base:
                return c
        return None

    def _apply_short_term_intel(
        self,
        candidates: dict[str, Candidate],
        items: list[dict],
    ) -> tuple[int, int]:
        new_count = 0
        enrich_count = 0
        for item in items:
            sym = str(item.get("symbol", "")).upper().replace("USDT", "")
            if not sym:
                continue
            c = self._find_candidate_by_symbol(candidates, sym)
            if c is None:
                c = Candidate(
                    symbol=sym,
                    name=sym,
                    has_futures=True,
                    sources=["binance_short_intel"],
                    price_usd=item.get("price_usd", 0.0),
                    price_change_24h=item.get("price_change_24h", 0.0),
                    volume_24h=item.get("volume_24h", 0.0),
                )
                candidates[sym] = c
                new_count += 1
            else:
                enrich_count += 1
                if "binance_short_intel" not in c.sources:
                    c.sources.append("binance_short_intel")

            c.has_futures = True
            for key in (
                "price_usd", "price_change_24h", "volume_24h", "futures_oi",
                "oi_change_5m_pct", "oi_change_15m_pct", "volume_5m_ratio",
                "taker_buy_ratio_5m", "taker_sell_ratio_5m", "funding_rate_pct",
                "retail_short_pct", "long_account_pct", "top_trader_long_pct",
                "oi_volume_ratio", "contract_activity_score",
            ):
                if key in item:
                    setattr(c, key, item[key])
        return new_count, enrich_count

    def _apply_liquidation_stats(self, candidates: dict[str, Candidate]) -> None:
        symbols = {
            f"{c.symbol.upper().replace('USDT', '')}USDT"
            for c in candidates.values()
            if c.has_futures
        }
        if not symbols:
            return
        stats = liquidation_stats(symbols)
        for c in candidates.values():
            full = f"{c.symbol.upper().replace('USDT', '')}USDT"
            row = stats.get(full)
            if not row:
                continue
            c.liquidation_5m_usd = row.get("liquidation_5m_usd", 0.0)
            c.liquidation_15m_usd = row.get("liquidation_15m_usd", 0.0)

    def _base_opportunity(self, c: Candidate) -> tuple[int, str, str, int, list[str], list[str], list[str]]:
        reasons: list[str] = []
        risks: list[str] = []
        required = ["本地1m动能突破或轧空/轧多信号仍需触发", "入场瞬间Taker与OI方向不反转"]

        score = int(round(
            c.score * 0.24 +
            c.anomaly_score * 0.26 +
            c.contract_activity_score * 0.36
        ))
        if abs(c.oi_change_15m_pct) >= 4:
            score += 10
            reasons.append(f"15m OI {c.oi_change_15m_pct:+.2f}%")
        elif abs(c.oi_change_5m_pct) >= 1:
            score += 5
            reasons.append(f"5m OI {c.oi_change_5m_pct:+.2f}%")
        if c.volume_5m_ratio >= 2:
            score += 8
            reasons.append(f"5m放量 {c.volume_5m_ratio:.1f}x")
        if c.liquidation_5m_usd >= 50_000:
            score += 8
            reasons.append(f"5m强平 {c.liquidation_5m_usd/1000:.0f}K")
        elif c.liquidation_15m_usd >= 100_000:
            score += 5
            reasons.append(f"15m强平 {c.liquidation_15m_usd/1000:.0f}K")
        if c.okx_large_trade_pct >= 0.15:
            score += 6
            reasons.append(f"OKX大单 {c.okx_large_trade_pct*100:.0f}%")

        long_score = 0
        short_score = 0
        taker = float(c.taker_buy_ratio_5m or 0.5)
        if taker >= 0.58:
            long_score += 22
            reasons.append(f"Taker买 {taker:.0%}")
        elif taker >= 0.52 and c.price_change_24h > 5:
            long_score += 10
            reasons.append(f"Taker买盘不弱 {taker:.0%}")
        elif taker <= 0.42:
            short_score += 22
            reasons.append(f"Taker卖 {1-taker:.0%}")
        elif taker <= 0.48 and c.price_change_24h < -5:
            short_score += 10
            reasons.append(f"Taker卖盘不弱 {1-taker:.0%}")

        if c.oi_change_15m_pct > 1 and c.price_change_24h > 0:
            long_score += 8
        if c.oi_change_15m_pct > 1 and c.price_change_24h < 0:
            short_score += 8
        if c.price_change_24h >= 12 and (
            c.oi_change_15m_pct >= 0.5 or
            c.oi_change_24h_pct >= 20 or
            c.contract_activity_score >= 45
        ):
            long_score += 18
            reasons.append("24h强趋势，按顺势接力候选")
        if c.price_change_24h <= -12 and (
            c.oi_change_15m_pct >= 0.5 or
            c.oi_change_24h_pct >= 20 or
            c.contract_activity_score >= 45
        ):
            short_score += 18
            reasons.append("24h弱趋势，按顺势接力候选")
        if c.price_change_1h >= -0.8 and c.price_change_24h >= 8:
            long_score += 5
        if c.price_change_1h <= 0.8 and c.price_change_24h <= -8:
            short_score += 5
        if c.funding_rate_pct <= -0.05 and c.retail_short_pct >= 60:
            long_score += 16
            reasons.append("负FR+空头拥挤")
        if c.funding_rate_pct >= 0.05 and c.long_account_pct >= 60:
            short_score += 12
            risks.append("正FR+多头拥挤")
        if c.okx_buy_ratio >= 0.62:
            long_score += 8
        elif 0 < c.okx_buy_ratio <= 0.38:
            short_score += 8
        if c.sentiment_label == "bullish":
            long_score += 5
        elif c.sentiment_label == "bearish":
            short_score += 5

        hard_block = self._is_hard_block_candidate(c)
        min_score = int(config_manager.settings.get("YAOBI_OPPORTUNITY_MIN_SCORE", 45))
        if hard_block:
            action = "BLOCK"
            permission = "BLOCK"
            risks.append(c.surf_ai_reason or c.decision_note or "硬风险信号")
        elif score < min_score:
            action = "OBSERVE"
            permission = "OBSERVE"
            risks.append(f"机会分{score}低于阈值{min_score}")
        elif long_score >= short_score + 8 and long_score >= 20:
            action = "WATCH_LONG_CONTINUATION"
            permission = "ALLOW_IF_1M_SIGNAL"
            required.append("只接受多头1m延续/回踩确认，禁止反向追空")
        elif short_score >= long_score + 8 and short_score >= 20:
            action = "WATCH_SHORT_CONTINUATION"
            permission = "ALLOW_IF_1M_SIGNAL"
            required.append("只接受空头1m延续/回踩确认，禁止反向追多")
        else:
            action = "OBSERVE"
            permission = "OBSERVE"
            required.append("方向分歧，等待Taker/OI重新同向")

        confidence = max(0, min(100, score + abs(long_score - short_score) - len(risks) * 6))
        return max(0, min(100, score)), action, permission, confidence, reasons[:6], risks[:6], required[:6]

    @staticmethod
    def _action_direction(action: str) -> str:
        raw = str(action or "").upper()
        if raw in {"WATCH_LONG", "WATCH_LONG_CONTINUATION", "WATCH_LONG_FADE"}:
            return "LONG"
        if raw in {"WATCH_SHORT", "WATCH_SHORT_CONTINUATION", "WATCH_SHORT_FADE"}:
            return "SHORT"
        return ""

    @staticmethod
    def _is_watch_action(action: str) -> bool:
        return YaobiScanner._action_direction(action) in {"LONG", "SHORT"}

    @staticmethod
    def _is_fade_action(action: str) -> bool:
        return str(action or "").upper().endswith("_FADE")

    @staticmethod
    def _parse_dt(text: str) -> Optional[datetime]:
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _setup_rank(state: str) -> int:
        mapping = {"WAIT": 0, "ARMED": 1, "HOT": 2, "BLOCK": -1}
        return mapping.get(str(state or "").upper(), 0)

    @staticmethod
    def _surf_hard_block_reason(reason: str) -> bool:
        lower = str(reason or "").lower()
        if not lower:
            return False
        hard_terms = (
            "hack", "exploit", "rug", "scam", "fraud", "breach", "stolen",
            "vulnerability", "delist", "suspend", "lawsuit", "no data",
            "no activity", "unclear direction", "unclear market", "illiquid",
            "liquidity too low", "liquidity risk",
        )
        return any(term in lower for term in hard_terms)

    def _is_hard_block_candidate(self, c: Candidate) -> bool:
        return (
            c.surf_news_sentiment == "negative"
            or c.okx_risk_level >= 4
            or bool(c.surf_ai_hard_block)
        )

    def _prev_candidate_state(self, c: Candidate) -> dict:
        row = candidates_map.get(c.key())
        return row if isinstance(row, dict) else {}

    def _prev_playbook_active(self, prev: dict) -> bool:
        if not prev:
            return False
        action = str(prev.get("opportunity_action", "") or "")
        perm = str(prev.get("opportunity_permission", "") or "")
        expires = self._parse_dt(prev.get("opportunity_expires_at", ""))
        if not self._is_watch_action(action):
            return False
        if perm != "ALLOW_IF_1M_SIGNAL":
            return False
        return bool(expires and expires > datetime.now())

    def _carry_forward_playbook(self, c: Candidate) -> bool:
        prev = self._prev_candidate_state(c)
        if not self._prev_playbook_active(prev):
            return False
        if self._is_hard_block_candidate(c):
            return False
        if self._is_watch_action(c.opportunity_action):
            return False
        prev_action = str(prev.get("opportunity_action", "") or "")
        if not self._is_watch_action(prev_action):
            return False
        c.opportunity_action = prev_action
        c.opportunity_confidence = max(c.opportunity_confidence, int(prev.get("opportunity_confidence", 0) or 0))
        c.ai_provider = c.ai_provider or str(prev.get("ai_provider", "") or "")
        c.ai_cached = True
        c.ai_updated_at = c.ai_updated_at or str(prev.get("ai_updated_at", "") or "")
        c.intelligence_summary = c.intelligence_summary or str(prev.get("intelligence_summary", "") or "")
        reasons = list(c.opportunity_reasons or [])
        reasons.insert(0, "沿用上一轮AI剧本窗口")
        c.opportunity_reasons = reasons[:5]
        return True

    def _evaluate_playbook_setup(self, c: Candidate) -> tuple[str, str, str]:
        action = str(c.opportunity_action or "").upper()
        if action == "BLOCK" or self._is_hard_block_candidate(c):
            note = c.surf_ai_reason or c.decision_note or "硬风险拦截"
            return "BLOCK", "", note
        if not self._is_watch_action(action):
            return "WAIT", "", "等待AI/规则生成明确剧本"

        direction = self._action_direction(action)
        is_fade = self._is_fade_action(action)
        taker = float(c.taker_buy_ratio_5m or 0.5)
        vol5 = float(c.volume_5m_ratio or 0.0)
        oi5 = float(c.oi_change_5m_pct or 0.0)
        oi15 = float(c.oi_change_15m_pct or 0.0)
        oi24 = float(c.oi_change_24h_pct or 0.0)
        price1h = float(c.price_change_1h or 0.0)
        price24h = float(c.price_change_24h or 0.0)
        funding = float(c.funding_rate_pct or 0.0)
        long_pct = float(c.long_account_pct or 0.0)
        retail_short = float(c.retail_short_pct or 0.0)
        activity = int(c.contract_activity_score or 0)

        if not is_fade:
            trigger = "BREAKOUT"
            trend_score = 0
            local_score = 0
            if direction == "LONG":
                if price24h >= 5:
                    trend_score += 2
                if price1h >= -0.8:
                    trend_score += 1
                if oi15 >= 0.5:
                    trend_score += 2
                if oi24 >= 10:
                    trend_score += 1
                if activity >= 30:
                    trend_score += 1
                if vol5 >= 0.5:
                    local_score += 1
                if taker >= 0.46:
                    local_score += 1
                if oi5 >= -0.5:
                    local_score += 1
                hot = trend_score >= 4 and local_score >= 2 and taker >= 0.50 and vol5 >= 0.6
                armed = trend_score >= 3
                note = (
                    "15m趋势仍强，等待1m回踩后再突破"
                    if armed and not hot
                    else "5m/15m共振，允许1m顺势接力"
                )
            else:
                if price24h <= -5:
                    trend_score += 2
                if price1h <= 0.8:
                    trend_score += 1
                if oi15 >= 0.5:
                    trend_score += 2
                if oi24 >= 10:
                    trend_score += 1
                if activity >= 30:
                    trend_score += 1
                if vol5 >= 0.5:
                    local_score += 1
                if taker <= 0.54:
                    local_score += 1
                if oi5 >= -0.5:
                    local_score += 1
                hot = trend_score >= 4 and local_score >= 2 and taker <= 0.50 and vol5 >= 0.6
                armed = trend_score >= 3
                note = (
                    "15m空头趋势仍强，等待1m反抽后再破位"
                    if armed and not hot
                    else "5m/15m共振，允许1m顺势接力"
                )
            if hot:
                return "HOT", trigger, note
            if armed:
                return "ARMED", trigger, note
            return "WAIT", trigger, "趋势尚未形成稳定延续，先观察"

        trigger = "SQUEEZE"
        if direction == "SHORT":
            extension = 0
            if price24h >= 12:
                extension += 2
            if price1h >= 1.5:
                extension += 1
            if oi15 >= 1.0:
                extension += 1
            if funding >= 0.02:
                extension += 1
            if long_pct >= 55:
                extension += 1
            if activity >= 35:
                extension += 1
            armed = extension >= 3
            hot = armed and (taker <= 0.46 or vol5 <= 1.0 or oi5 <= 0.2)
            note = (
                "趋势已拉伸，等待1m顶部衰竭后短空"
                if armed and not hot
                else "5m已有衰竭迹象，只做1m反打空"
            )
        else:
            extension = 0
            if price24h <= -12:
                extension += 2
            if price1h <= -1.5:
                extension += 1
            if abs(oi15) >= 1.0:
                extension += 1
            if funding <= -0.02:
                extension += 1
            if retail_short >= 58:
                extension += 1
            if activity >= 35:
                extension += 1
            armed = extension >= 3
            hot = armed and (taker >= 0.54 or vol5 <= 1.0 or oi5 <= 0.2)
            note = (
                "跌势已拉伸，等待1m底部衰竭后抢反弹"
                if armed and not hot
                else "5m已有衰竭迹象，只做1m反打多"
            )
        if hot:
            return "HOT", trigger, note
        if armed:
            return "ARMED", trigger, note
        return "WAIT", trigger, "局部衰竭条件不足，先观察"

    def _apply_playbook_state(self, candidates: list[Candidate]) -> None:
        ttl_min = int(config_manager.settings.get("YAOBI_PLAYBOOK_TTL_MINUTES", 45) or 45)
        now = datetime.now()
        for c in candidates:
            self._carry_forward_playbook(c)
            prev = self._prev_candidate_state(c)
            prev_active = self._prev_playbook_active(prev)
            prev_action = str(prev.get("opportunity_action", "") or "")
            state, family, note = self._evaluate_playbook_setup(c)
            if (
                state == "WAIT"
                and prev_active
                and prev_action == c.opportunity_action
                and not self._is_hard_block_candidate(c)
            ):
                state = "ARMED"
                note = (note + "；沿用上一轮剧本窗口").strip("；")
            c.opportunity_trigger_family = family
            c.opportunity_setup_state = state
            c.opportunity_setup_note = note[:160]
            if state == "BLOCK":
                c.opportunity_action = "BLOCK"
                c.opportunity_permission = "BLOCK"
                c.opportunity_expires_at = ""
                continue
            if not self._is_watch_action(c.opportunity_action):
                c.opportunity_permission = "OBSERVE"
                c.opportunity_expires_at = ""
                continue
            c.opportunity_permission = "ALLOW_IF_1M_SIGNAL" if state in {"ARMED", "HOT"} else "OBSERVE"
            if c.opportunity_permission == "ALLOW_IF_1M_SIGNAL":
                c.opportunity_expires_at = (now + timedelta(minutes=ttl_min)).strftime("%Y-%m-%d %H:%M:%S")
            elif prev_active and prev_action == c.opportunity_action:
                c.opportunity_expires_at = str(prev.get("opportunity_expires_at", "") or "")
            else:
                c.opportunity_expires_at = ""
            required = list(c.opportunity_required_confirmation or [])
            if family == "BREAKOUT":
                required.insert(0, "5m/15m剧本已就绪，仅等待1m回踩/破位触发")
            elif family == "SQUEEZE":
                required.insert(0, "5m/15m剧本已就绪，仅等待1m顶部/底部衰竭反打")
            c.opportunity_required_confirmation = list(dict.fromkeys(required))[:5]

    def _apply_dual_ai_consensus(self, candidates: list[Candidate]) -> None:
        if not bool(config_manager.settings.get("YAOBI_AI_ENABLED", False)):
            return

        strict_consensus = bool(config_manager.settings.get("YAOBI_DUAL_AI_CONSENSUS_REQUIRED", False))
        min_conf = int(config_manager.settings.get("YAOBI_SURF_DIRECTION_MIN_CONFIDENCE", 55) or 55)
        surf_enabled = (
            _surf_enabled()
            and config_manager.settings.get("YAOBI_SURF_ENABLED", True)
            and config_manager.settings.get("YAOBI_SURF_AI_ENABLED", False)
        )
        for c in candidates:
            if c.opportunity_permission == "BLOCK":
                continue
            if not self._is_watch_action(c.opportunity_action):
                continue
            if self._is_hard_block_candidate(c):
                c.opportunity_action = "BLOCK"
                c.opportunity_permission = "BLOCK"
                risks = list(c.opportunity_risks or [])
                risks.insert(0, f"Surf硬风险: {c.surf_ai_reason or 'risk_high'}")
                c.opportunity_risks = risks[:5]
                continue

            if not surf_enabled:
                if strict_consensus:
                    c.opportunity_action = "OBSERVE"
                    c.opportunity_permission = "OBSERVE"
                    risks = list(c.opportunity_risks or [])
                    risks.insert(0, "双AI同向已开启，但Surf方向终审未启用")
                    c.opportunity_risks = risks[:5]
                continue

            expected = self._action_direction(c.opportunity_action)
            bias = str(c.surf_ai_bias or "").upper()
            confidence = int(c.surf_ai_confidence or 0)
            if not strict_consensus:
                reasons = list(c.opportunity_reasons or [])
                if bias and bias not in {"NEUTRAL", expected} and confidence >= min_conf:
                    reasons.insert(0, f"Surf逆向提示: {bias}({confidence})")
                elif bias == expected and confidence >= min_conf:
                    reasons.insert(0, f"Surf同向确认: {bias}({confidence})")
                c.opportunity_reasons = reasons[:5]
                continue

            if bias != expected or confidence < min_conf:
                c.opportunity_action = "OBSERVE"
                c.opportunity_permission = "OBSERVE"
                risks = list(c.opportunity_risks or [])
                if not bias or bias == "NEUTRAL":
                    risks.insert(0, f"Surf方向未达成共识(置信度{confidence})")
                else:
                    risks.insert(0, f"Surf偏{bias}与Gemini偏{expected}不一致")
                c.opportunity_risks = risks[:5]
                continue

            reasons = list(c.opportunity_reasons or [])
            surf_reason = c.surf_ai_reason or f"Surf同向{expected}"
            if surf_reason and surf_reason not in reasons:
                reasons.insert(0, f"Surf同向: {surf_reason}")
            c.opportunity_reasons = reasons[:5]

    async def _apply_opportunity_queue(self, candidates: list[Candidate]) -> None:
        futures = [c for c in candidates if c.has_futures]
        if not futures:
            set_opportunity_queue([])
            scan_status["ai_status"] = ai_provider_status()
            return

        queue_candidates: list[Candidate] = []
        for c in futures:
            (
                op_score, action, permission, confidence, reasons, risks, required,
            ) = self._base_opportunity(c)
            c.opportunity_score = op_score
            c.opportunity_action = action
            c.opportunity_permission = permission
            c.opportunity_confidence = confidence
            c.opportunity_reasons = reasons
            c.opportunity_risks = risks
            c.opportunity_required_confirmation = required
            c.intelligence_summary = "；".join((reasons or risks or [c.market_filter_note or c.decision_note])[:2])
            if (
                c.opportunity_score >= int(config_manager.settings.get("YAOBI_OPPORTUNITY_MIN_SCORE", 45))
                or action != "OBSERVE"
                or self._prev_playbook_active(self._prev_candidate_state(c))
            ):
                queue_candidates.append(c)

        queue_candidates.sort(
            key=lambda c: (
                c.opportunity_permission == "ALLOW_IF_1M_SIGNAL",
                c.opportunity_score,
                c.anomaly_score,
                c.contract_activity_score,
            ),
            reverse=True,
        )

        ai_status = ai_provider_status()
        ai_required = bool(config_manager.settings.get("YAOBI_AI_REQUIRED_FOR_PERMISSION", True))
        if ai_required:
            for c in queue_candidates:
                if self._is_watch_action(c.opportunity_action) and c.opportunity_permission != "BLOCK":
                    c.opportunity_permission = "OBSERVE"
                    required = list(c.opportunity_required_confirmation or [])
                    required.insert(0, "等待Gemini方向终审通过后，才允许1m执行")
                    c.opportunity_required_confirmation = required[:5]

        if config_manager.settings.get("YAOBI_AI_ENABLED", False) and queue_candidates:
            ai_top = int(config_manager.settings.get("YAOBI_AI_MAX_SYMBOLS_PER_RUN", 12) or 12)
            async with aiohttp.ClientSession(trust_env=True) as session:
                ai_result = await analyze_opportunities(session, queue_candidates[:ai_top])
            ai_status = ai_result.get("status", ai_status)
            logger.info(
                "🔍 Gemini终审: top=%d | reason=%s | provider=%s | calls=%s | budget_left=%s",
                ai_top,
                ai_status.get("last_reason", ""),
                ai_status.get("last_provider", ""),
                (ai_status.get("usage") or {}).get("calls", 0),
                ai_status.get("budget_left_usd", ""),
            )
            if ai_status.get("last_error"):
                logger.warning("🔍 Gemini终审失败细节: %s", ai_status.get("last_error"))
            ai_failed = ai_status.get("last_reason") == "all_failed"
            failure_fallback = (
                ai_failed
                and bool(config_manager.settings.get("YAOBI_AI_FAILURE_FALLBACK_ENABLED", True))
            )
            fallback_min_score = int(config_manager.settings.get("YAOBI_AI_FAILURE_FALLBACK_MIN_SCORE", 45) or 45)
            ai_by_symbol = {
                str(item.get("symbol", "")).upper().replace("USDT", ""): item
                for item in ai_result.get("items", [])
                if item.get("symbol")
            }
            for c in queue_candidates:
                ai = ai_by_symbol.get(c.symbol.upper().replace("USDT", ""))
                if not ai:
                    continue
                c.ai_provider = ai.get("provider", "")
                c.ai_cached = bool(ai.get("cached"))
                c.ai_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                c.intelligence_summary = ai.get("summary") or c.intelligence_summary
                if ai.get("reasons"):
                    c.opportunity_reasons = (ai.get("reasons") or [])[:5]
                if ai.get("risks"):
                    c.opportunity_risks = (ai.get("risks") or [])[:5]
                if ai.get("required_confirmation"):
                    c.opportunity_required_confirmation = (ai.get("required_confirmation") or [])[:5]
                c.opportunity_confidence = max(c.opportunity_confidence, int(ai.get("confidence", 0) or 0))
                ai_action = ai.get("action")
                ai_permission = ai.get("permission")
                if ai_action == "BLOCK" or ai_permission == "BLOCK":
                    c.opportunity_action = "BLOCK"
                    c.opportunity_permission = "BLOCK"
                elif self._is_watch_action(ai_action):
                    c.opportunity_action = ai_action
                    c.opportunity_permission = "OBSERVE"
                elif ai_required:
                    c.opportunity_action = "OBSERVE"
                    c.opportunity_permission = "OBSERVE"
                c.opportunity_score = max(c.opportunity_score, min(100, c.opportunity_confidence))

            if ai_required:
                reviewed = set(ai_by_symbol)
                for c in queue_candidates:
                    key = c.symbol.upper().replace("USDT", "")
                    if key in reviewed or c.opportunity_permission == "BLOCK":
                        continue
                    if failure_fallback and self._is_watch_action(c.opportunity_action) and c.opportunity_score >= fallback_min_score:
                        c.ai_provider = c.ai_provider or "rule_fallback"
                        c.ai_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        reasons = list(c.opportunity_reasons or [])
                        reasons.insert(0, "AI终审连接失败，使用规则+Surf风险兜底")
                        c.opportunity_reasons = reasons[:5]
                        required = list(c.opportunity_required_confirmation or [])
                        required.insert(0, "AI恢复前只允许高分剧本进入1m执行层")
                        c.opportunity_required_confirmation = required[:5]
                        continue
                    c.opportunity_action = "OBSERVE"
                    c.opportunity_permission = "OBSERVE"
                    risks = list(c.opportunity_risks or [])
                    risks.insert(0, "未进入Gemini终审TopN，先观察")
                    c.opportunity_risks = risks[:5]
                    summary = c.intelligence_summary or ""
                    if "Gemini终审" not in summary:
                        c.intelligence_summary = ("未进入Gemini终审TopN；" + summary).strip("；")

        self._apply_dual_ai_consensus(queue_candidates)
        self._apply_playbook_state(queue_candidates)

        allow_cnt = sum(1 for c in queue_candidates if c.opportunity_permission == "ALLOW_IF_1M_SIGNAL")
        block_cnt = sum(1 for c in queue_candidates if c.opportunity_permission == "BLOCK")
        observe_cnt = max(0, len(queue_candidates) - allow_cnt - block_cnt)
        hot_cnt = sum(1 for c in queue_candidates if c.opportunity_setup_state == "HOT")
        logger.info(
            "🔍 机会队列结果: 候选%d | AI许可%d | 观察%d | Block%d | HOT%d | 双AI=%s",
            len(queue_candidates),
            allow_cnt,
            observe_cnt,
            block_cnt,
            hot_cnt,
            "ON" if config_manager.settings.get("YAOBI_DUAL_AI_CONSENSUS_REQUIRED", False) else "OFF",
        )

        queue_candidates.sort(
            key=lambda c: (
                c.opportunity_permission == "ALLOW_IF_1M_SIGNAL",
                self._setup_rank(c.opportunity_setup_state),
                c.opportunity_score,
                c.anomaly_score,
                c.contract_activity_score,
            ),
            reverse=True,
        )
        top_n = int(config_manager.settings.get("YAOBI_OPPORTUNITY_TOP_N", 6) or 6)
        for idx, c in enumerate(queue_candidates[:top_n], 1):
            c.opportunity_rank = idx
        for c in queue_candidates[top_n:]:
            c.opportunity_rank = 0
        set_opportunity_queue([c.to_dict() for c in queue_candidates[:max(top_n, 1)]])
        scan_status["ai_status"] = ai_status

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

        fallback_limit = int(config_manager.settings.get("YAOBI_SURF_FALLBACK_SEARCH_LIMIT", 3))
        fallback_searches = 0
        for c in candidates:
            matched = [
                item for item in items
                if surf_news_matches_symbol(item, c.symbol, c.name)
            ][:5]
            if (not matched
                    and fallback_searches < fallback_limit
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
        """对高分候选进行 Surf AI 方向+风险终审，单次批量调用降低 credits 消耗。"""
        if not _surf_enabled():
            record_provider_skip("surf", "chat/completions", "missing_surf_api_key", items=len(candidates))
            return
        if not candidates:
            return

        compact_rows = []
        for c in candidates:
            compact_rows.append({
                "symbol": c.symbol,
                "score": c.score,
                "price_1h": round(float(c.price_change_1h or 0), 2),
                "price_4h": round(float(c.price_change_4h or 0), 2),
                "price_24h": round(float(c.price_change_24h or 0), 2),
                "oi_5m": round(float(c.oi_change_5m_pct or 0), 3),
                "oi_15m": round(float(c.oi_change_15m_pct or 0), 3),
                "oi_24h": round(float(c.oi_change_24h_pct or 0), 2),
                "volume_5m_ratio": round(float(c.volume_5m_ratio or 0), 2),
                "volume_ratio": round(float(c.volume_ratio or 0), 2),
                "taker_buy_5m": round(float(c.taker_buy_ratio_5m or 0.5), 3),
                "funding_pct": round(float(c.funding_rate_pct or 0), 5),
                "retail_short_pct": round(float(c.retail_short_pct or 0), 2),
                "long_account_pct": round(float(c.long_account_pct or 0), 2),
                "top_trader_long_pct": round(float(c.top_trader_long_pct or 0), 2),
                "liq_5m_usd": round(float(c.liquidation_5m_usd or 0), 2),
                "liq_15m_usd": round(float(c.liquidation_15m_usd or 0), 2),
                "okx_buy_ratio": round(float(c.okx_buy_ratio or 0), 3),
                "contract_activity": int(c.contract_activity_score or 0),
                "rule_action": c.opportunity_action or "OBSERVE",
                "news_sentiment": c.surf_news_sentiment or "neutral",
                "news_titles": c.surf_news_titles[:2],
            })

        payload = json.dumps({"candidates": compact_rows}, ensure_ascii=False, separators=(",", ":"))
        prompt = (
            "You are a crypto short-term market analyst. Review the provided candidates for the next 15-60 minutes only. "
            "Use only the supplied compact data and recent news titles. "
            "Return strict JSON only in this schema: "
            "{\"items\":[{\"symbol\":\"BASE\",\"bias\":\"LONG|SHORT|NEUTRAL\",\"risk\":\"LOW|MEDIUM|HIGH\","
            "\"confidence\":0,\"reason\":\"max 18 words\"}]}. "
            "Do not include markdown.\n"
            f"{payload}"
        )
        try:
            ok_resp, text, status = await surf_chat_completion(
                session,
                prompt,
                model=str(config_manager.settings.get("YAOBI_SURF_AI_MODEL", "surf-ask") or "surf-ask"),
                timeout_sec=22,
                reasoning_effort="low",
            )
            if not ok_resp:
                logger.debug("Surf AI 批量终审失败: %s", status)
                return
            raw = str(text or "").strip()
            if "```" in raw:
                parts = raw.split("```")
                for part in parts:
                    candidate = part.strip()
                    if candidate.lower().startswith("json"):
                        candidate = candidate[4:].strip()
                    if candidate.startswith("{") or candidate.startswith("["):
                        raw = candidate
                        break
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                start = raw.find("{")
                end = raw.rfind("}")
                if start < 0 or end <= start:
                    raise
                result = json.loads(raw[start:end + 1])
            items = result.get("items", []) if isinstance(result, dict) else result
            if not isinstance(items, list):
                items = []
            items_by_symbol = {
                str(item.get("symbol", "")).upper().replace("USDT", ""): item
                for item in items
                if isinstance(item, dict) and item.get("symbol")
            }
            for c in candidates:
                item = items_by_symbol.get(c.symbol.upper().replace("USDT", ""))
                if not item:
                    continue
                c.surf_ai_bias = str(item.get("bias", "") or "").upper()
                c.surf_ai_confidence = max(0, min(100, int(item.get("confidence", 0) or 0)))
                c.surf_ai_score = c.surf_ai_confidence
                c.surf_ai_risk_level = str(item.get("risk", "") or "").upper()
                c.surf_ai_reason = str(item.get("reason", "") or "")[:120]
                c.surf_ai_hard_block = self._surf_hard_block_reason(c.surf_ai_reason)
                if c.surf_ai_risk_level == "HIGH":
                    if c.surf_ai_hard_block:
                        logger.info("🔍 [%s] Surf AI 硬风险: %s", c.symbol, c.surf_ai_reason)
                    else:
                        logger.info("🔍 [%s] Surf AI 扩展风险: %s", c.symbol, c.surf_ai_reason)
        except Exception as e:
            record_provider_call("surf", "chat/completions", False, status="exception", error=f"{type(e).__name__}: {e}")
            logger.debug("Surf AI 批量终审异常: %s", e)

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

            hard_risk = self._is_hard_block_candidate(c)

            if c.category == "风险":
                risks.append("候选分类为风险")
            if c.surf_ai_hard_block:
                risks.append("Surf AI硬风险")
            elif c.surf_ai_risk_level == "HIGH":
                risks.append("Surf AI扩展风险")
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

            if hard_risk:
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
