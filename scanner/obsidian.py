"""
Obsidian Vault 写入器
Vault 路径: C:\\BOT\\yaobi  (config.OBSIDIAN_VAULT_PATH)
结构:
  daily/YYYY-MM-DD.md     每日摘要
  candidates/YYYY-MM-DD.md 当日候选币
  tokens/SYMBOL.md         单币深度文档
  scans/YYYY-MM-DD-square.md 广场热帖存档
  rules/rulebook.md         规则库（首次初始化）
"""
from __future__ import annotations
import logging
import os
from datetime import datetime

from config import config_manager

logger = logging.getLogger(__name__)


def vault_path() -> str:
    return config_manager.settings.get("OBSIDIAN_VAULT_PATH", r"C:\BOT\yaobi")


def _write(rel_path: str, content: str) -> None:
    full = os.path.join(vault_path(), rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    logger.debug("Obsidian 写入: %s", rel_path)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── 初始化规则库（只写一次）────────────────────────────────────────────────────

def init_rulebook() -> None:
    path = os.path.join(vault_path(), "rules", "rulebook.md")
    if os.path.exists(path):
        return
    content = """# 妖币规则库
更新时间: """ + _today() + """

> 这个文件存放稳定规则。每日 digest 可提出新规则候选，但只有经过人工确认或多次样本验证后才写入这里。

## R001: 不因"涨多了"就摸顶做空妖币
- **触发**: 小币/妖币已经明显上涨，但庄家仍在、空仓仍拥挤、资金费率仍给燃料。
- **确认**: OI、资金费率、现仓、现货/合约价差、CEX 充值和池子流动性。
- **动作偏好**: No trade 或小仓合适的摸顶做多。
- **失效**: OI 突然下降、CEX 充值/现货供给显著化、做惨退、社交买盘耗尽。

## R002: Alpha + 合约优先于 Alpha Only
- **触发**: Binance Alpha/Wallet/Booster 项目同时具备合约市场。
- **确认**: 合约 OI、资金费率、现仓、现货链上承接、社交扩散。
- **动作偏好**: 研究对手盘和庄动力；合约数据为观察，不用于杠杆参与。
- **失效**: 合约数据缺乏自洽、没有真实成交承接、官方路径落空。

## R003: 聪明钱要还原路径，不是看见买就买
- **触发**: KOL 提到内幕地址、聪明钱包、子钱包分发或静默持仓。
- **确认**: 地址历史胜率、底部填坑、高位出货、跨标的一致性、CEX 转入。
- **动作偏好**: 建立追卡、等地址路径和市场数据同步。
- **失效**: 单次地址动作无法解释、地址标签不可验证、已被全网扩散。

---
*由 SqueezeBot YaoBI Scanner 自动初始化*
"""
    _write("rules/rulebook.md", content)
    logger.info("Obsidian 规则库初始化完成")


# ── 写入每日候选报告 ─────────────────────────────────────────────────────────

def write_daily_candidates(candidates: list[dict]) -> None:
    if not candidates:
        return
    today = _today()
    lines = [
        f"# 妖币候选 {today}",
        f"*生成时间: {_now()} | 候选数: {len(candidates)}*",
        "",
        "---",
        "",
    ]
    for c in candidates[:30]:
        score     = c.get("score", 0)
        symbol    = c.get("symbol", "?")
        chain     = c.get("chain", "")
        price     = c.get("price_usd", 0)
        ch24      = c.get("price_change_24h", 0)
        vol       = c.get("volume_24h", 0)
        mentions  = c.get("square_mentions", 0)
        sources   = ", ".join(c.get("sources", []))
        sigs      = c.get("signals", [])

        score_bar = "🟢" if score >= 70 else ("🟡" if score >= 50 else "🔴")
        lines += [
            f"## {score_bar} [{symbol}](tokens/{symbol}.md)  Score: {score}/100",
            f"- **链**: {chain}  **价格**: ${price:.6g}  **24H**: {ch24:+.1f}%",
            f"- **成交量**: ${vol/1e6:.2f}M  **广场提及**: {mentions} 次",
            f"- **来源**: {sources}",
        ]
        if sigs:
            lines.append("- **信号**:")
            for s in sigs[:5]:
                lines.append(f"  - {s}")
        bd = c.get("score_breakdown", {})
        if bd:
            breakdown = " | ".join(f"{k} {v:+d}" for k, v in bd.items() if v != 0)
            lines.append(f"- **评分拆分**: {breakdown}")
        lines += ["", "---", ""]

    _write(f"candidates/{today}.md", "\n".join(lines))
    logger.info("Obsidian 候选报告已写入 candidates/%s.md (%d 个候选)", today, len(candidates))


# ── 写入每日摘要 ─────────────────────────────────────────────────────────────

def write_daily_digest(candidates: list[dict], square_posts: list[dict]) -> None:
    today = _today()
    top5  = candidates[:5]
    lines = [
        f"# 每日摘要 {today}",
        f"*{_now()} 自动生成*",
        "",
        "## 今日 Top 5 候选",
        "",
    ]
    for i, c in enumerate(top5, 1):
        sym   = c.get("symbol", "?")
        score = c.get("score", 0)
        ch24  = c.get("price_change_24h", 0)
        lines.append(f"{i}. **{sym}** — 评分 {score} | 24H {ch24:+.1f}%")

    lines += [
        "",
        "## 广场热帖摘要",
        "",
    ]
    for p in square_posts[:5]:
        lines.append(f"- {p.get('text', '')[:120]}  *(热度 {p.get('heat', 0)})*")

    lines += [
        "",
        "## 待人工确认",
        "",
        "- [ ] 检查 Top 候选的现货/合约价差",
        "- [ ] 验证聪明钱地址路径",
        "- [ ] 评估是否更新规则库",
        "",
        "---",
        f"*[[candidates/{today}|查看完整候选列表]]*",
    ]
    _write(f"daily/{today}.md", "\n".join(lines))
    logger.info("Obsidian 每日摘要已写入 daily/%s.md", today)


# ── 写入广场热帖存档 ─────────────────────────────────────────────────────────

def write_square_archive(posts: list[dict], ticker_map: dict) -> None:
    today = _today()
    lines = [
        f"# 币安广场热帖 {_now()}",
        f"*共 {len(posts)} 条 | 提及币种 {len(ticker_map)} 个*",
        "",
        "## 提及频次排行",
        "",
    ]
    sorted_tickers = sorted(ticker_map.items(), key=lambda x: x[1]["count"], reverse=True)
    for sym, data in sorted_tickers[:20]:
        lines.append(f"- **${sym}** — 提及 {data['count']} 次 | 热度 {data['total_heat']}")
    lines += ["", "---", ""]
    for p in posts[:30]:
        lines += [
            f"> {p.get('text', '')[:200]}",
            f"> 👍{p.get('likes',0)} 🔁{p.get('reposts',0)} 👀{p.get('views',0)}",
            "",
        ]
    _write(f"scans/{today}-binance-square.md", "\n".join(lines))


# ── 写入单币文档 ─────────────────────────────────────────────────────────────

def write_token_doc(c: dict) -> None:
    sym   = c.get("symbol", "UNKNOWN")
    today = _today()
    lines = [
        f"# {sym}",
        f"*最后更新: {_now()}*",
        "",
        "## 基本信息",
        f"- **链**: {c.get('chain', '')}  **合约**: `{c.get('address', '')}`",
        f"- **价格**: ${c.get('price_usd', 0):.6g}",
        f"- **市值**: ${c.get('market_cap', 0)/1e6:.2f}M  **流动性**: ${c.get('liquidity', 0)/1e6:.2f}M",
        f"- **持仓人数**: {c.get('holder_count', 0):,}",
        "",
        "## 评分",
        f"- **总分**: {c.get('score', 0)}/100",
    ]
    bd = c.get("score_breakdown", {})
    for k, v in bd.items():
        lines.append(f"  - {k}: {v:+d}")
    lines += [
        "",
        "## 信号",
    ]
    for s in c.get("signals", []):
        lines.append(f"- {s}")
    links = c.get("links", {})
    if links:
        lines += ["", "## 链接"]
        for k, v in links.items():
            if v:
                lines.append(f"- [{k}]({v})")
    lines += [
        "",
        "## 观察记录",
        f"| 日期 | 价格 | 涨跌 | 备注 |",
        f"|------|------|------|------|",
        f"| {today} | ${c.get('price_usd', 0):.6g} | {c.get('price_change_24h', 0):+.1f}% | 首次记录 |",
    ]
    _write(f"tokens/{sym}.md", "\n".join(lines))
