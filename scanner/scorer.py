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

    # ── 8. OI & 合约信号 (最高 55) ──────────────────────────────────────────
    oi_pts = 0
    if c.oi_change_24h_pct >= 150:
        oi_pts += 25; sigs.append(f"OI日增+{c.oi_change_24h_pct:.0f}%")
    elif c.oi_change_24h_pct >= 80:
        oi_pts += 15; sigs.append(f"OI日增+{c.oi_change_24h_pct:.0f}%")
    elif c.oi_change_24h_pct >= 40:
        oi_pts += 8;  sigs.append(f"OI日增+{c.oi_change_24h_pct:.0f}%")

    if c.volume_ratio >= 20:
        oi_pts += 20; sigs.append(f"成交量{c.volume_ratio:.0f}x放量")
    elif c.volume_ratio >= 10:
        oi_pts += 12; sigs.append(f"成交量{c.volume_ratio:.0f}x放量")
    elif c.volume_ratio >= 5:
        oi_pts += 6;  sigs.append(f"成交量{c.volume_ratio:.0f}x放量")

    if c.oi_flat_days >= 14 and c.volume_ratio >= 5:
        oi_pts += 20; sigs.append(f"OI死平{c.oi_flat_days}天+首次放量{c.volume_ratio:.0f}x")
    elif c.oi_flat_days >= 7 and c.volume_ratio >= 3:
        oi_pts += 12; sigs.append(f"OI死平{c.oi_flat_days}天+首次放量{c.volume_ratio:.0f}x")

    if c.oi_acceleration >= 30:
        oi_pts += 10; sigs.append(f"OI加速+{c.oi_acceleration:.0f}%+放量{c.volume_ratio:.0f}x")
    elif c.oi_acceleration >= 15:
        oi_pts += 5
    bd["OI信号"] = min(oi_pts, 55)

    # ── 9. API 增强加分 (最高 58) ────────────────────────────────────────────
    api_pts = 0

    # Surf 正面新闻
    if c.surf_news_sentiment == "positive":
        api_pts += 10; sigs.append("Surf正面新闻")

    # OKX 多链验证
    if c.okx_chain_count >= 2:
        api_pts += 8; sigs.append(f"OKX多链验证({c.okx_chain_count}链)")

    # OKX 机构大单流入
    if c.okx_large_trade_pct >= 0.30:
        api_pts += 15; sigs.append(f"机构大单{c.okx_large_trade_pct*100:.0f}%")

    # 资金费率极端做空 (轧空前兆)
    if c.fr_extreme_short:
        api_pts += 12; sigs.append(f"FR极端做空{c.funding_rate_pct:.3f}%")

    # 散户拥挤做空
    if c.retail_short_pct >= 65.0:
        api_pts += 8; sigs.append(f"散户空头拥挤{c.retail_short_pct:.0f}%")

    # 链上交易加速
    if c.txs_5m_accel >= 1.5:
        api_pts += 5; sigs.append(f"链上Tx加速{c.txs_5m_accel:.1f}x")

    bd["API增强"] = min(api_pts, 58)

    # ── 10. 风险扣分 ─────────────────────────────────────────────────────────
    deduct = 0
    # 流动性扣分只针对链上代币，合约品种(无链)不适用
    is_onchain = bool(c.chain or c.address)
    if is_onchain:
        if c.liquidity < 10_000:
            deduct += 20; sigs.append("⚠ 流动性极低 (<$10K)")
        elif c.liquidity < 50_000:
            deduct += 10; sigs.append("⚠ 流动性偏低 (<$50K)")

    if c.market_cap > 0 and c.market_cap < 50_000:
        deduct += 5; sigs.append("⚠ 市值极小 (<$50K)")

    if c.price_change_24h < -30:
        deduct += 15; sigs.append(f"⚠ 已大幅下跌 {c.price_change_24h:.0f}%")

    # 大户翻空
    if c.whale_long_ratio < 0.5:  # Has real futures data (default is 0.5)
        whale_short = (1 - c.whale_long_ratio) * 100
        if whale_short >= 60:
            deduct += 15; sigs.append(f"⚠ 大户翻空{whale_short:.0f}%")
        elif whale_short >= 55:
            deduct += 8;  sigs.append(f"大户偏空{whale_short:.0f}%")

    # 空头拥挤
    if c.short_crowd_pct >= 68:
        deduct += 12; sigs.append(f"⚠ 空头拥挤{c.short_crowd_pct:.0f}%")
    elif c.short_crowd_pct >= 62:
        deduct += 6

    # 庄家出货 (OI↓ 价格↑)
    if c.oi_change_24h_pct < -15 and c.price_change_24h > 10:
        deduct += 20; sigs.append("⚠ OI↓价格↑(庄家出货)")

    # Surf AI 高风险裁决
    if c.surf_ai_risk_level == "HIGH":
        deduct += 30; sigs.append(f"⚠ SurfAI高风险: {c.surf_ai_reason}")

    bd["风险扣分"] = -deduct

    # ── 汇总 ─────────────────────────────────────────────────────────────────
    raw = sum(v for v in bd.values())
    c.score = max(0, min(100, raw))
    c.score_breakdown = bd
    c.signals = sigs

    # ── 分类 (只对有OI数据的合约品种) ─────────────────────────────────────────
    if not c.category and c.has_futures:
        whale_s = (1 - c.whale_long_ratio) * 100
        risk_flag = (
            whale_s >= 60
            or c.short_crowd_pct >= 68
            or (c.oi_change_24h_pct < -15 and c.price_change_24h > 10)
        )
        if risk_flag:
            c.category = "风险"
        elif c.oi_change_24h_pct >= 80 and c.volume_ratio >= 10:
            c.category = "启动预警"
        elif (
            (c.oi_flat_days >= 7 and c.volume_ratio >= 3)
            or c.oi_acceleration >= 20
            or (c.oi_change_24h_pct >= 40 and c.volume_ratio >= 5)
        ):
            c.category = "蓄势观察"

    return c
