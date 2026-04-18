"""
妖币扫描器 — 主调度器
数据链路: GeckoTerminal → DEX Screener → Binance Square → OKX Market 补全 → 评分 → Obsidian
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, date
from typing import Optional

import aiohttp

from config import config_manager
from scanner.candidates import (
    Candidate, upsert_candidate, scan_status, candidates_map, clear_candidates,
)
from scanner.scorer import score as score_candidate
from scanner.sources.geckoterminal import fetch_trending_pools, fetch_new_pools
from scanner.sources.dexscreener import (
    fetch_token_boosts, fetch_latest_profiles, fetch_token_detail,
)
from scanner.sources.okx_market import get_token_price_info
from scanner.sources.binance_square import fetch_hot_posts, extract_ticker_mentions
from scanner.obsidian import (
    init_rulebook, write_daily_candidates, write_daily_digest,
    write_square_archive, write_token_doc,
)

logger = logging.getLogger(__name__)

_FUTURES_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
_SCAN_NETWORKS = ["eth", "bsc", "solana", "base", "arbitrum"]


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
            try:
                square_posts = await fetch_hot_posts(session, rows=50)
                ticker_map = extract_ticker_mentions(square_posts)
                self._apply_square_mentions(candidates, ticker_map)
                scan_status["sources"]["binance_square"] = {
                    "count": len(square_posts), "last_run": start.strftime("%H:%M"),
                }
                logger.info("🔍 广场: %d 条热帖, 提及 %d 个币种", len(square_posts), len(ticker_map))
            except Exception as e:
                logger.warning("广场抓取失败: %s", e)

            # ── 5. OKX Market 价格补全 (有 address + chain_id 的候选) ────────
            enriched = 0
            enrich_targets = [
                c for c in candidates.values()
                if c.address and c.chain_id and c.chain_id not in ("501",)
            ]
            for c in enrich_targets[:60]:
                try:
                    info = await get_token_price_info(session, c.chain_id, c.address)
                    if info:
                        c.price_usd = info["price_usd"] or c.price_usd
                        c.market_cap = info["market_cap"] or c.market_cap
                        c.liquidity = info["liquidity"] or c.liquidity
                        c.holder_count = info["holder_count"] or c.holder_count
                        c.price_change_1h = info["price_change_1h"]
                        c.price_change_4h = info["price_change_4h"]
                        c.price_change_24h = info["price_change_24h"] or c.price_change_24h
                        c.volume_24h = info["volume_24h"] or c.volume_24h
                        enriched += 1
                    await asyncio.sleep(0.08)
                except Exception:
                    pass
            logger.info("🔍 OKX 补充 %d 个链上数据", enriched)

            # ── 6. DEX Screener 价格兜底 (仍然缺价格的) ──────────────────────
            needs_detail = [
                c for c in candidates.values()
                if c.price_usd == 0 and c.address
            ]
            for c in needs_detail[:20]:
                try:
                    detail = await fetch_token_detail(session, c.chain, c.address)
                    if detail:
                        c.price_usd = detail.get("price_usd") or c.price_usd
                        c.price_change_1h = detail.get("price_change_1h") or c.price_change_1h
                        c.price_change_24h = detail.get("price_change_24h") or c.price_change_24h
                        c.volume_24h = detail.get("volume_24h") or c.volume_24h
                        c.liquidity = detail.get("liquidity") or c.liquidity
                        c.market_cap = detail.get("market_cap") or c.market_cap
                        c.links.update(detail.get("links", {}))
                    await asyncio.sleep(0.05)
                except Exception:
                    pass

        # ── 7. Binance 期货标记 ──────────────────────────────────────────────
        for c in candidates.values():
            if (c.symbol.upper() + "USDT") in self._futures_symbols:
                c.has_futures = True

        # ── 8. 聪明钱启发式检测 ──────────────────────────────────────────────
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

        # ── 9. 打分 & 写入候选库 ─────────────────────────────────────────────
        min_score = int(config_manager.settings.get("YAOBI_MIN_SCORE", 30))
        scored: list[Candidate] = []
        for c in candidates.values():
            c = score_candidate(c)
            if c.score >= min_score:
                upsert_candidate(c)
                scored.append(c)
        scored.sort(key=lambda x: x.score, reverse=True)

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

        # ── 10. Obsidian 日报 (每天一次) ────────────────────────────────────
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
                    # Update price/volume if currently zero
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
