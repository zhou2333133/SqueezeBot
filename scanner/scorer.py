"""
多因子打分引擎 — 0~100 分制
参考图片中 Surf.ai 的评分拆分逻辑:
  基础策略分 + KOL/广场加分 + 成交量加分 + 广场加分 - 风险扣分
"""
from __future__ import annotations
from .candidates import Candidate


def score(c: Candidate) -> Candidate:
    bd: dict[str, int] = {}
    sigs: list[str]    = []

    # ── 1. 基础链上活跃分 (最高 35) ─────────────────────────────────────────
    base = 0
    if c.price_change_24h > 50:
        base += 15; sigs.append(f"24H涨幅 +{c.price_change_24h:.0f}%")
    elif c.price_change_24h > 20:
        base += 8; sigs.append(f"24H涨幅 +{c.price_change_24h:.0f}%")
    elif c.price_change_24h > 10:
        base += 4

    if c.price_change_1h > 10:
        base += 10; sigs.append(f"1H急涨 +{c.price_change_1h:.1f}%")
    elif c.price_change_1h > 5:
        base += 5; sigs.append(f"1H涨幅 +{c.price_change_1h:.1f}%")
    elif c.price_change_1h > 2:
        base += 2

    if c.price_change_4h > 20:
        base += 10; sigs.append(f"4H涨幅 +{c.price_change_4h:.1f}%")
    elif c.price_change_4h > 10:
        base += 5
    bd["基础活跃"] = min(base, 35)

    # ── 2. 成交量加分 (最高 20) ──────────────────────────────────────────────
    vol_pts = 0
    if c.volume_24h > 10_000_000:
        vol_pts = 20; sigs.append(f"24H成交 ${c.volume_24h/1e6:.1f}M")
    elif c.volume_24h > 1_000_000:
        vol_pts = 12; sigs.append(f"24H成交 ${c.volume_24h/1e6:.1f}M")
    elif c.volume_24h > 100_000:
        vol_pts = 6
    elif c.volume_24h > 10_000:
        vol_pts = 2
    bd["成交量"] = vol_pts

    # ── 3. 持仓人数加分 (最高 10) ────────────────────────────────────────────
    holder_pts = 0
    if c.holder_count > 10000:
        holder_pts = 10
    elif c.holder_count > 1000:
        holder_pts = 6
    elif c.holder_count > 100:
        holder_pts = 3
    bd["持仓人数"] = holder_pts

    # ── 4. 聪明钱/大户信号 (最高 20) ────────────────────────────────────────
    smart_pts = 0
    if c.smart_money_signal:
        smart_pts = 20; sigs.append(f"聪明钱信号: {c.smart_money_detail}")
    bd["聪明钱"] = smart_pts

    # ── 5. 币安广场社交热度 (最高 15) ────────────────────────────────────────
    square_pts = 0
    if c.square_mentions >= 20:
        square_pts = 15; sigs.append(f"广场提及 {c.square_mentions} 次")
    elif c.square_mentions >= 10:
        square_pts = 10; sigs.append(f"广场提及 {c.square_mentions} 次")
    elif c.square_mentions >= 5:
        square_pts = 6; sigs.append(f"广场提及 {c.square_mentions} 次")
    elif c.square_mentions >= 2:
        square_pts = 3
    bd["广场热度"] = square_pts

    # ── 6. 合约联动加分 (最高 10) ────────────────────────────────────────────
    futures_pts = 0
    if c.has_futures:
        futures_pts += 5; sigs.append("有币安合约")
    if c.futures_oi > 10_000_000:
        futures_pts += 5; sigs.append(f"OI ${c.futures_oi/1e6:.1f}M")
    elif c.futures_oi > 1_000_000:
        futures_pts += 2
    bd["合约联动"] = futures_pts

    # ── 7. 多源发现加分 (最高 10) ────────────────────────────────────────────
    src_pts = min(len(set(c.sources)) * 3, 10)
    if src_pts >= 6:
        sigs.append(f"多平台同步热 ({len(set(c.sources))} 个来源)")
    bd["多源发现"] = src_pts

    # ── 8. 风险扣分 ──────────────────────────────────────────────────────────
    deduct = 0
    if c.liquidity < 10_000:
        deduct += 20; sigs.append("⚠ 流动性极低 (<$10K)")
    elif c.liquidity < 50_000:
        deduct += 10; sigs.append("⚠ 流动性偏低 (<$50K)")

    if c.market_cap > 0 and c.market_cap < 50_000:
        deduct += 5; sigs.append("⚠ 市值极小 (<$50K)")

    if c.price_change_24h < -30:
        deduct += 15; sigs.append(f"⚠ 已大幅下跌 {c.price_change_24h:.0f}%")
    bd["风险扣分"] = -deduct

    # ── 汇总 ─────────────────────────────────────────────────────────────────
    raw = sum(v for v in bd.values())
    c.score = max(0, min(100, raw))
    c.score_breakdown = bd
    c.signals = sigs
    return c
