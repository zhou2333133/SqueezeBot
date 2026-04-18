"""
共享候选币种状态 —— 类似 signals.py 的设计模式
所有 source 写入，web.py / obsidian 读取
"""
from __future__ import annotations
import json
import queue as std_queue
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

MAX_CANDIDATES = 200

# ── 共享状态 ───────────────────────────────────────────────────────────────────
candidates_map:   dict[str, dict] = {}   # key = chain:address or symbol
candidates_queue: std_queue.Queue = std_queue.Queue(maxsize=200)
scan_status: dict = {
    "last_scan":      None,
    "scanning":       False,
    "total_scanned":  0,
    "errors":         [],
    "sources":        {},   # source_name -> {"count": int, "last_run": str}
}


@dataclass
class Candidate:
    # ── 身份 ──────────────────────────────────────────────────────────────────
    symbol:   str
    name:     str       = ""
    chain:    str       = ""          # eth / bsc / sol / arb / base ...
    address:  str       = ""          # contract address
    chain_id: str       = "1"         # OKX chainIndex

    # ── 价格 & 市场 ───────────────────────────────────────────────────────────
    price_usd:          float = 0.0
    price_change_1h:    float = 0.0
    price_change_4h:    float = 0.0
    price_change_24h:   float = 0.0
    volume_24h:         float = 0.0
    liquidity:          float = 0.0
    market_cap:         float = 0.0
    holder_count:       int   = 0

    # ── 合约联动 ──────────────────────────────────────────────────────────────
    has_futures:   bool  = False
    futures_oi:    float = 0.0
    funding_rate:  float = 0.0

    # ── 信号来源 ──────────────────────────────────────────────────────────────
    sources:              list[str] = field(default_factory=list)
    square_mentions:      int   = 0
    square_posts:         list  = field(default_factory=list)   # [{title,url,heat}]
    smart_money_signal:   bool  = False
    smart_money_detail:   str   = ""
    gecko_trend_rank:     int   = 0
    dex_boost_rank:       int   = 0

    # ── 打分 ──────────────────────────────────────────────────────────────────
    score:           int  = 0
    score_breakdown: dict = field(default_factory=dict)
    signals:         list = field(default_factory=list)   # human-readable

    # ── 元数据 ────────────────────────────────────────────────────────────────
    found_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    links: dict = field(default_factory=dict)   # {dexscreener, gecko, binance, ...}
    logo_url: str = ""
    tags: list = field(default_factory=list)

    def key(self) -> str:
        if self.address and self.chain_id:
            return f"{self.chain_id}:{self.address.lower()}"
        return self.symbol.upper()

    def to_dict(self) -> dict:
        return {
            "key":                self.key(),
            "symbol":             self.symbol,
            "name":               self.name,
            "chain":              self.chain,
            "chain_id":           self.chain_id,
            "address":            self.address,
            "price_usd":          self.price_usd,
            "price_change_1h":    round(self.price_change_1h, 2),
            "price_change_4h":    round(self.price_change_4h, 2),
            "price_change_24h":   round(self.price_change_24h, 2),
            "volume_24h":         self.volume_24h,
            "liquidity":          self.liquidity,
            "market_cap":         self.market_cap,
            "holder_count":       self.holder_count,
            "has_futures":        self.has_futures,
            "futures_oi":         self.futures_oi,
            "funding_rate":       self.funding_rate,
            "sources":            self.sources,
            "square_mentions":    self.square_mentions,
            "square_posts":       self.square_posts[:3],
            "smart_money_signal": self.smart_money_signal,
            "smart_money_detail": self.smart_money_detail,
            "gecko_trend_rank":   self.gecko_trend_rank,
            "dex_boost_rank":     self.dex_boost_rank,
            "score":              self.score,
            "score_breakdown":    self.score_breakdown,
            "signals":            self.signals,
            "found_at":           self.found_at,
            "updated_at":         self.updated_at,
            "links":              self.links,
            "logo_url":           self.logo_url,
            "tags":               self.tags,
        }


def upsert_candidate(c: Candidate) -> None:
    k = c.key()
    c.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    candidates_map[k] = c.to_dict()
    # Push update to WebSocket queue
    msg = json.dumps({"type": "update", "key": k, "data": c.to_dict()}, ensure_ascii=False, default=str)
    if candidates_queue.full():
        try: candidates_queue.get_nowait()
        except std_queue.Empty: pass
    try: candidates_queue.put_nowait(msg)
    except std_queue.Full: pass


def get_sorted_candidates(min_score: int = 0, chain: str = "", source: str = "") -> list[dict]:
    items = list(candidates_map.values())
    if min_score:
        items = [x for x in items if x.get("score", 0) >= min_score]
    if chain:
        items = [x for x in items if x.get("chain", "").lower() == chain.lower()]
    if source:
        items = [x for x in items if source in x.get("sources", [])]
    return sorted(items, key=lambda x: x.get("score", 0), reverse=True)


def clear_candidates() -> None:
    candidates_map.clear()
