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

from config import config_manager, SURF_API_KEY
from scanner.candidates import (
    Candidate, upsert_candidate, scan_status, candidates_map, clear_candidates,
)
from scanner.scorer import score as score_candidate
from scanner.sources.geckoterminal import fetch_trending_pools, fetch_new_pools
from scanner.sources.dexscreener import (
    fetch_token_boosts, fetch_latest_profiles, fetch_token_detail,
)
from scanner.sources.okx_market import get_token_price_info, search_tokens, get_token_trades, CHAIN_NAMES
from scanner.sources.binance_square import fetch_hot_posts, extract_ticker_mentions
from scanner.sources.binance_futures import scan_futures_oi
from scanner.obsidian import (
    init_rulebook, write_daily_candidates, write_daily_digest,
    write_square_archive, write_token_doc,
)

logger = logging.getLogger(__name__)

_FUTURES_URL   = "https://fapi.binance.com/fapi/v1/exchangeInfo"
_SCAN_NETWORKS = ["eth", "bsc", "solana", "base", "arbitrum"]

_SURF_ENABLED = bool(SURF_API_KEY and SURF_API_KEY != "YOUR_SURF_API_KEY")

_NEG_KW = {"hack", "exploit", "rug", "scam", "delist", "suspend", "crash",
           "fraud", "lawsuit", "ponzi", "breach", "stolen", "vulnerability"}
_POS_KW = {"partnership", "listing", "upgrade", "adoption", "mainnet", "launch",
           "integration", "backed", "grant", "milestone", "record"}
_NEG_GATES = {"not", "no", "never", "successfully", "patches", "fixed",
              "resolved", "mitigated", "prevents", "blocked"}


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

            # ── 4. Binance Square ────────────────────────────────────────────
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

            # ── 5. Surf 新闻预过滤 ───────────────────────────────────────────
            if _SURF_ENABLED and config_manager.settings.get("YAOBI_SURF_ENABLED", True):
                cand_list = list(candidates.values())
                cand_list = await self._surf_news_filter(session, cand_list)
                # Rebuild dict from filtered list
                removed = set(candidates.keys()) - {c.key() for c in cand_list}
                for k in removed:
                    candidates.pop(k, None)
                logger.info("🔍 Surf新闻过滤: 剩余 %d 个候选 (淘汰 %d 个负面)", len(cand_list), len(removed))

            # ── 6. OKX Market 价格补全 ───────────────────────────────────────
            enriched = 0
            enrich_targets = [
                c for c in candidates.values()
                if c.address and c.chain_id and c.chain_id not in ("501",)
            ]
            for c in enrich_targets[:60]:
                try:
                    info = await get_token_price_info(session, c.chain_id, c.address)
                    if info:
                        c.price_usd      = info["price_usd"]      or c.price_usd
                        c.market_cap     = info["market_cap"]      or c.market_cap
                        c.liquidity      = info["liquidity"]       or c.liquidity
                        c.holder_count   = info["holder_count"]    or c.holder_count
                        c.price_change_1h  = info["price_change_1h"]
                        c.price_change_4h  = info["price_change_4h"]
                        c.price_change_24h = info["price_change_24h"] or c.price_change_24h
                        c.volume_24h       = info["volume_24h"]    or c.volume_24h
                        # 5分钟交易笔数加速
                        txs_now = info.get("tx_count_5m", 0)
                        if c.txs_5m > 0 and txs_now > 0:
                            c.txs_5m_accel = txs_now / c.txs_5m
                        c.txs_5m = txs_now
                        enriched += 1
                    await asyncio.sleep(0.08)
                except Exception:
                    pass
            logger.info("🔍 OKX 补充 %d 个链上数据", enriched)

            # ── 7. DEX Screener 价格兜底 ─────────────────────────────────────
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

        # ── 8. Binance 期货标记 ──────────────────────────────────────────────
        for c in candidates.values():
            if (c.symbol.upper() + "USDT") in self._futures_symbols:
                c.has_futures = True

        # ── 9. 聪明钱启发式检测 ──────────────────────────────────────────────
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

        # ── 10. 合约OI扫描 + 资金费率 + 散户多空 ────────────────────────────
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
                        existing.oi_acceleration   = item.get("oi_acceleration",   0)
                        existing.oi_flat_days      = item.get("oi_flat_days",      0)
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
                            oi_acceleration=item.get("oi_acceleration",   0),
                            oi_flat_days=item.get("oi_flat_days",      0),
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

            # ── 11. OKX 多链验证 + 大单分析 (有合约的品种) ──────────────────
            futures_candidates = [c for c in candidates.values() if c.has_futures]
            if futures_candidates:
                sem = asyncio.Semaphore(5)
                async def _enrich_okx_one(c: Candidate) -> None:
                    async with sem:
                        await self._enrich_okx(session, c)

                await asyncio.gather(*[_enrich_okx_one(c) for c in futures_candidates],
                                     return_exceptions=True)
                logger.info("🔍 OKX链验证: %d 个合约品种已分析", len(futures_candidates))

        # ── 12. 首轮打分 ─────────────────────────────────────────────────────
        min_score = int(config_manager.settings.get("YAOBI_MIN_SCORE", 30))
        scored: list[Candidate] = []
        for c in candidates.values():
            c = score_candidate(c)
            if c.score >= min_score:
                scored.append(c)
        scored.sort(key=lambda x: x.score, reverse=True)

        # ── 13. Surf AI 终审 (Top-N) ─────────────────────────────────────────
        surf_top_n = int(config_manager.settings.get("YAOBI_SURF_TOP_N", 5))
        if _SURF_ENABLED and config_manager.settings.get("YAOBI_SURF_ENABLED", True) and scored:
            top_candidates = [c for c in scored[:surf_top_n] if c.score >= 55]
            if top_candidates:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    await self._surf_ai_review(session, top_candidates)
                # Re-score after AI review (apply -30 if HIGH)
                for c in top_candidates:
                    c = score_candidate(c)
                scored.sort(key=lambda x: x.score, reverse=True)
                logger.info("🔍 Surf AI终审: %d 个候选已审查", len(top_candidates))

        # ── 14. 写入候选库 ───────────────────────────────────────────────────
        for c in scored:
            upsert_candidate(c)

        elapsed = (datetime.now() - start).total_seconds()
        scan_status.update({
            "scanning":      False,
            "last_scan":     start.strftime("%Y-%m-%d %H:%M:%S"),
            "total_scanned": len(candidates),
            "scored":        len(scored),
        })
        logger.info(
            "🔍 扫描完成: %d 个候选, %d 个达标 (≥%d分), 耗时 %.1fs",
            len(candidates), len(scored), min_score, elapsed,
        )

        # ── 15. Obsidian 日报 (每天一次) ────────────────────────────────────
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
        headers = {"Authorization": f"Bearer {SURF_API_KEY}"}

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

        async def _check(c: Candidate) -> None:
            base = c.symbol.replace("USDT", "")
            try:
                async with session.get(
                    "https://api.asksurf.ai/v1/news/curated",
                    params={"q": base, "limit": 3},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as resp:
                    if resp.status != 200:
                        return
                    items = (await resp.json()).get("data", [])
                    if not items:
                        return
                    c.surf_news_titles = [i.get("title", "") for i in items]
                    combined = " ".join(t.lower() for t in c.surf_news_titles)
                    c.surf_news_sentiment = _classify(combined)
            except Exception:
                pass

        sem = asyncio.Semaphore(8)
        async def bounded(c: Candidate) -> None:
            async with sem:
                await _check(c)

        await asyncio.gather(*[bounded(c) for c in candidates], return_exceptions=True)
        return [c for c in candidates if c.surf_news_sentiment != "negative"]

    # ── OKX 多链验证 + 大单分析 ────────────────────────────────────────────────

    async def _enrich_okx(self, session: aiohttp.ClientSession, c: Candidate) -> None:
        """通过 OKX API 验证代币在多链的部署情况和机构大单占比。"""
        try:
            tokens = await search_tokens(session, c.symbol, limit=10)
            if tokens:
                chains = list({t.get("chain_id", "") for t in tokens if t.get("chain_id")})
                c.okx_chain_count  = len(chains)
                c.okx_chains_found = [CHAIN_NAMES.get(ch, ch) for ch in chains]
        except Exception:
            pass

        if c.address and c.chain_id:
            try:
                trade_info = await get_token_trades(session, c.chain_id, c.address)
                c.okx_large_trade_pct = trade_info.get("large_trade_pct", 0.0)
            except Exception:
                pass

    # ── Surf AI 终审 ───────────────────────────────────────────────────────────

    async def _surf_ai_review(
        self,
        session: aiohttp.ClientSession,
        candidates: list[Candidate],
    ) -> None:
        """对高分候选进行 Surf AI 风险审查，设置 surf_ai_risk_level。"""
        headers = {"Authorization": f"Bearer {SURF_API_KEY}"}

        async def _review(c: Candidate) -> None:
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
                async with session.post(
                    "https://api.asksurf.ai/v1/chat/completions",
                    json={"model": "surf-1.5-turbo",
                          "messages": [{"role": "user", "content": prompt}]},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status != 200:
                        return
                    data   = await resp.json()
                    text   = data["choices"][0]["message"]["content"].strip()
                    if "```" in text:
                        text = text.split("```")[1].lstrip("json").strip()
                    result = json.loads(text)
                    c.surf_ai_score      = max(0, min(100, int(result.get("score", 50))))
                    c.surf_ai_risk_level = result.get("risk", "")
                    c.surf_ai_reason     = result.get("reason", "")
                    if c.surf_ai_risk_level == "HIGH":
                        logger.info("🔍 [%s] Surf AI 高风险: %s", c.symbol, c.surf_ai_reason)
            except Exception as e:
                logger.debug("Surf AI 审查异常 [%s]: %s", c.symbol, e)

        sem = asyncio.Semaphore(3)
        async def bounded(c: Candidate) -> None:
            async with sem:
                await _review(c)

        await asyncio.gather(*[bounded(c) for c in candidates], return_exceptions=True)

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
                address  = (item.get("address") or "").lower()
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
