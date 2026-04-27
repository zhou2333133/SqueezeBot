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
opportunity_queue: list[dict] = []
opportunity_update_queue: std_queue.Queue = std_queue.Queue(maxsize=100)
scan_status: dict = {
    "last_scan":      None,
    "scanning":       False,
    "total_scanned":  0,
    "errors":         [],
    "sources":        {},   # source_name -> {"count": int, "last_run": str}
    "opportunity_count": 0,
    "ai_status": {},
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

    # ── OI & 合约异动 ─────────────────────────────────────────────────────────
    oi_change_24h_pct: float = 0.0   # OI日增%
    oi_change_3d_pct:  float = 0.0   # OI 3日变化%
    oi_change_7d_pct:  float = 0.0   # OI 7日变化%
    oi_acceleration:   float = 0.0   # OI加速% (近4h vs 前4h)
    oi_flat_days:      int   = 0     # OI连续死平天数
    oi_trend_grade:    str   = ""    # S/A/B/C/RISK，用于关注层分层
    oi_consistency_score: int = 0     # 最近3日OI上涨一致性 0-100
    ema_deviation_pct: float = 0.0    # 当前价相对短EMA偏离%
    volume_ratio:      float = 0.0   # 成交量放量倍数 (今日 / 7日均量)
    whale_long_ratio:  float = 0.5   # 大户多头比例 (0~1)
    short_crowd_pct:   float = 50.0  # 空头拥挤度%

    # ── Surf 新闻 & AI 审查 ──────────────────────────────────────────────────
    surf_news_sentiment: str  = ""    # positive / negative / neutral
    surf_news_titles:    list = field(default_factory=list)
    surf_ai_risk_level:  str  = ""    # HIGH / MEDIUM / LOW
    surf_ai_bias:        str  = ""    # LONG / SHORT / NEUTRAL
    surf_ai_confidence:  int  = 0     # 0-100
    surf_ai_reason:      str  = ""
    surf_ai_score:       int  = 0     # 0-100
    surf_ai_hard_block:  bool = False

    # ── OKX 增强 ──────────────────────────────────────────────────────────────
    okx_chain_count:     int   = 0
    okx_chains_found:    list  = field(default_factory=list)
    okx_large_trade_pct: float = 0.0  # 大单(>$5K)成交占比
    okx_buy_ratio:       float = 0.0
    okx_risk_level:      int   = 0
    okx_token_tags:      list  = field(default_factory=list)
    okx_top10_hold_pct:  float = 0.0
    okx_dev_hold_pct:    float = 0.0
    okx_lp_burned_pct:   float = 0.0
    okx_smart_money_holders: int = 0

    # ── 币安合约增强 ──────────────────────────────────────────────────────────
    funding_rate_pct:  float = 0.0    # 当前资金费率%
    fr_extreme_short:  bool  = False  # 资金费率 < -0.05%
    retail_short_pct:  float = 0.0    # 全局账户多空比空头侧%
    oi_change_5m_pct:  float = 0.0
    oi_change_15m_pct: float = 0.0
    volume_5m_ratio:   float = 0.0
    taker_buy_ratio_5m: float = 0.5
    taker_sell_ratio_5m: float = 0.5
    long_account_pct:  float = 50.0
    top_trader_long_pct: float = 50.0
    liquidation_5m_usd: float = 0.0
    liquidation_15m_usd: float = 0.0
    oi_volume_ratio:   float = 0.0
    contract_activity_score: int = 0

    # ── 链上交易加速 ──────────────────────────────────────────────────────────
    txs_5m:       int   = 0
    txs_5m_accel: float = 0.0         # 近5分钟 vs 前5分钟交易数倍数

    # ── 异常币/情绪雷达 ──────────────────────────────────────────────────────
    anomaly_score:      int   = 0     # 0-100, 类 OKX market-filter 异动指数
    anomaly_tags:       list  = field(default_factory=list)
    sentiment_heat:     int   = 0     # 新闻/广场/Surf 综合热度
    sentiment_score:    int   = 0     # -100~100, 偏多为正、偏空为负
    sentiment_label:    str   = ""    # bullish / bearish / neutral
    long_short_text:    str   = "—/—" # UI 显示: 多x%/空y%
    holder_signal:      str   = ""    # OKX top10/dev/smart-money 摘要
    market_filter_note: str   = ""    # 核心异动解释

    # ── 决策解释卡（只作筛选/复盘，不直接生成交易）──────────────────────────
    decision_action:     str  = "观察"  # 允许交易 / 等待确认 / 禁止交易 / 观察
    decision_confidence: int  = 0
    decision_reasons:    list = field(default_factory=list)
    decision_risks:      list = field(default_factory=list)
    decision_missing:    list = field(default_factory=list)
    decision_note:       str  = ""

    # ── AI/机会队列（只作上游筛选，不直接下单）──────────────────────────────
    opportunity_rank:       int  = 0
    opportunity_score:      int  = 0
    opportunity_action:     str  = "OBSERVE"  # WATCH_*_CONTINUATION / WATCH_*_FADE / OBSERVE / BLOCK
    opportunity_permission: str  = "OBSERVE"  # ALLOW_IF_1M_SIGNAL / OBSERVE / BLOCK
    opportunity_confidence: int  = 0
    opportunity_reasons:    list = field(default_factory=list)
    opportunity_risks:      list = field(default_factory=list)
    opportunity_required_confirmation: list = field(default_factory=list)
    opportunity_trigger_family: str = ""   # BREAKOUT / SQUEEZE
    opportunity_setup_state: str = "WAIT"  # WAIT / ARMED / HOT / BLOCK
    opportunity_setup_note: str = ""
    opportunity_expires_at: str = ""
    ai_provider:            str  = ""
    ai_cached:              bool = False
    ai_updated_at:          str  = ""
    knowledge_refs:         list = field(default_factory=list)
    intelligence_summary:   str  = ""
    alpha_status:           str  = ""

    # ── 分类 ──────────────────────────────────────────────────────────────────
    category: str = ""   # 启动预警 / 蓄势观察 / 风险

    # ── 打分 ──────────────────────────────────────────────────────────────────
    score:           int  = 0
    score_raw:       int  = 0     # 未截断的原始评分（可>100），看含金量
    score_breakdown: dict = field(default_factory=dict)
    signals:         list = field(default_factory=list)   # human-readable

    # ── 三层决策面板（v2.8 风格）───────────────────────────────────────────────
    # decision_tier: L1_MAIN（追涨第一仓） / L2_AMBUSH（埋伏等启动） / RISK_AVOID（避险）/ ""（中性）
    # decision_subtype: L1 子类（OI爆发/加速中/妖币启动） / L2 子类（突破前夜/静默建仓/早期启动） / 风险子类（出货家族/FR警告/链上风险）
    decision_tier:    str = ""
    decision_subtype: str = ""

    # ── 元数据 ────────────────────────────────────────────────────────────────
    found_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    links: dict = field(default_factory=dict)   # {dexscreener, gecko, binance, ...}
    logo_url: str = ""
    tags: list = field(default_factory=list)

    # ── V4AF 选币所需（由 token_supply 启发式填充）──────────────────────────
    listing_age_days:    int   = 0      # 距币安合约首次上线的天数
    circulating_supply:  float = 0.0
    total_supply:        float = 0.0
    circulation_pct:     float = 0.0    # circulating_supply / total_supply (0-1)
    fdv_ratio:           float = 0.0    # market_cap / FDV (0-1)，越高越接近全流通
    vesting_phase:       str   = ""     # pre_unlock / unlock_active / unlock_late / post_unlock / near_full_meme / unknown
    flash_eligible:      bool  = False  # 启发式判定是否纳入 V4AF 目标池
    flash_ban_reason:    str   = ""     # 被 ban 的原因（low_volume / near_full_circ / unknown_supply 等）

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
            "oi_change_24h_pct":  round(self.oi_change_24h_pct, 1),
            "oi_change_3d_pct":   round(self.oi_change_3d_pct, 1),
            "oi_change_7d_pct":   round(self.oi_change_7d_pct, 1),
            "oi_acceleration":    round(self.oi_acceleration, 1),
            "oi_flat_days":       self.oi_flat_days,
            "oi_trend_grade":     self.oi_trend_grade,
            "oi_consistency_score": self.oi_consistency_score,
            "ema_deviation_pct":  round(self.ema_deviation_pct, 2),
            "volume_ratio":       round(self.volume_ratio, 1),
            "whale_long_ratio":   round(self.whale_long_ratio, 3),
            "short_crowd_pct":    round(self.short_crowd_pct, 1),
            "surf_news_sentiment": self.surf_news_sentiment,
            "surf_news_titles":   self.surf_news_titles[:3],
            "surf_ai_risk_level": self.surf_ai_risk_level,
            "surf_ai_bias":       self.surf_ai_bias,
            "surf_ai_confidence": self.surf_ai_confidence,
            "surf_ai_reason":     self.surf_ai_reason,
            "surf_ai_score":      self.surf_ai_score,
            "surf_ai_hard_block": self.surf_ai_hard_block,
            "okx_chain_count":    self.okx_chain_count,
            "okx_chains_found":   self.okx_chains_found,
            "okx_large_trade_pct": round(self.okx_large_trade_pct, 3),
            "okx_buy_ratio":      round(self.okx_buy_ratio, 3),
            "okx_risk_level":     self.okx_risk_level,
            "okx_token_tags":     self.okx_token_tags,
            "okx_top10_hold_pct": round(self.okx_top10_hold_pct, 2),
            "okx_dev_hold_pct":   round(self.okx_dev_hold_pct, 2),
            "okx_lp_burned_pct":  round(self.okx_lp_burned_pct, 2),
            "okx_smart_money_holders": self.okx_smart_money_holders,
            "funding_rate_pct":   round(self.funding_rate_pct, 4),
            "fr_extreme_short":   self.fr_extreme_short,
            "retail_short_pct":   round(self.retail_short_pct, 1),
            "oi_change_5m_pct":   round(self.oi_change_5m_pct, 3),
            "oi_change_15m_pct":  round(self.oi_change_15m_pct, 3),
            "volume_5m_ratio":    round(self.volume_5m_ratio, 2),
            "taker_buy_ratio_5m": round(self.taker_buy_ratio_5m, 3),
            "taker_sell_ratio_5m": round(self.taker_sell_ratio_5m, 3),
            "long_account_pct":   round(self.long_account_pct, 1),
            "top_trader_long_pct": round(self.top_trader_long_pct, 1),
            "liquidation_5m_usd": round(self.liquidation_5m_usd, 2),
            "liquidation_15m_usd": round(self.liquidation_15m_usd, 2),
            "oi_volume_ratio":    round(self.oi_volume_ratio, 4),
            "contract_activity_score": self.contract_activity_score,
            "txs_5m":             self.txs_5m,
            "txs_5m_accel":       round(self.txs_5m_accel, 2),
            "anomaly_score":       self.anomaly_score,
            "anomaly_tags":        self.anomaly_tags,
            "sentiment_heat":      self.sentiment_heat,
            "sentiment_score":     self.sentiment_score,
            "sentiment_label":     self.sentiment_label,
            "long_short_text":     self.long_short_text,
            "holder_signal":       self.holder_signal,
            "market_filter_note":  self.market_filter_note,
            "decision_action":     self.decision_action,
            "decision_confidence": self.decision_confidence,
            "decision_reasons":    self.decision_reasons[:5],
            "decision_risks":      self.decision_risks[:5],
            "decision_missing":    self.decision_missing[:5],
            "decision_note":       self.decision_note,
            "opportunity_rank":       self.opportunity_rank,
            "opportunity_score":      self.opportunity_score,
            "opportunity_action":     self.opportunity_action,
            "opportunity_permission": self.opportunity_permission,
            "opportunity_confidence": self.opportunity_confidence,
            "opportunity_reasons":    self.opportunity_reasons[:6],
            "opportunity_risks":      self.opportunity_risks[:6],
            "opportunity_required_confirmation": self.opportunity_required_confirmation[:6],
            "opportunity_trigger_family": self.opportunity_trigger_family,
            "opportunity_setup_state": self.opportunity_setup_state,
            "opportunity_setup_note": self.opportunity_setup_note,
            "opportunity_expires_at": self.opportunity_expires_at,
            "ai_provider":            self.ai_provider,
            "ai_cached":              self.ai_cached,
            "ai_updated_at":          self.ai_updated_at,
            "knowledge_refs":         self.knowledge_refs[:5],
            "intelligence_summary":   self.intelligence_summary,
            "alpha_status":           self.alpha_status,
            "category":           self.category,
            "score":              self.score,
            "score_raw":          self.score_raw,
            "score_breakdown":    self.score_breakdown,
            "signals":            self.signals,
            "decision_tier":      self.decision_tier,
            "decision_subtype":   self.decision_subtype,
            "found_at":           self.found_at,
            "updated_at":         self.updated_at,
            "links":              self.links,
            "logo_url":           self.logo_url,
            "tags":               self.tags,
            "listing_age_days":   self.listing_age_days,
            "circulating_supply": self.circulating_supply,
            "total_supply":       self.total_supply,
            "circulation_pct":    round(self.circulation_pct, 4),
            "fdv_ratio":          round(self.fdv_ratio, 4),
            "vesting_phase":      self.vesting_phase,
            "flash_eligible":     self.flash_eligible,
            "flash_ban_reason":   self.flash_ban_reason,
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


def get_anomaly_candidates(min_anomaly: int = 1, limit: int = 100) -> list[dict]:
    items = [
        x for x in candidates_map.values()
        if x.get("anomaly_score", 0) >= min_anomaly
    ]
    items = sorted(
        items,
        key=lambda x: (
            x.get("anomaly_score", 0),
            abs(x.get("oi_change_24h_pct", 0) or 0),
            abs(x.get("price_change_24h", 0) or 0),
            x.get("sentiment_heat", 0),
            x.get("score", 0),
        ),
        reverse=True,
    )
    return items[:limit]


def clear_candidates() -> None:
    candidates_map.clear()
    set_opportunity_queue([])


def set_opportunity_queue(items: list[dict]) -> None:
    global opportunity_queue
    opportunity_queue = list(items)
    scan_status["opportunity_count"] = len(opportunity_queue)
    msg = json.dumps({"type": "opportunity", "items": opportunity_queue}, ensure_ascii=False, default=str)
    if opportunity_update_queue.full():
        try:
            opportunity_update_queue.get_nowait()
        except std_queue.Empty:
            pass
    try:
        opportunity_update_queue.put_nowait(msg)
    except std_queue.Full:
        pass


def get_opportunity_queue(limit: int = 20) -> list[dict]:
    return opportunity_queue[:limit]
