"""
妖币雷达推送通知 — Telegram / Discord webhook

公开接口:
    summarize_scan(candidates: list[dict]) -> dict | None
        把候选列表汇总成 {"l1": [...], "l2": [...], "risk": [...], "header": "..."}
        如果没什么可推送的（L1+L2 都空），返回 None。

    format_telegram_text(summary: dict) -> str
        渲染成 Telegram 文本（Markdown 格式）。

    format_discord_text(summary: dict) -> str
        渲染成 Discord 文本（同样的 Markdown）。

    async push_yaobi_scan_summary(session, candidates) -> dict
        端到端: summarize + 渲染 + 推送 Telegram / Discord，返回各 channel 的
        发送结果 {"telegram": "ok"/"failed: ..."/"skipped: no_token", "discord": ...}.
        失败不抛异常（推送是 best-effort，主流程不应被阻塞）。

设计:
    - 受 cfg.YAOBI_NOTIFIER_ENABLED 控制
    - 受 cfg.YAOBI_NOTIFIER_MIN_TIER 控制要推哪几档（L1_MAIN / L2_AMBUSH / ALL）
    - 受 cfg.YAOBI_NOTIFIER_MAX_PER_BATCH 控制单条最多列几个币
    - 同一批扫描如果跟上次完全相同，不重复推送（去重指纹）
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

import aiohttp

from config import (
    DISCORD_WEBHOOK_URL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    config_manager,
)

logger = logging.getLogger(__name__)

# 去重指纹：上一次推送的 (l1_symbols + l2_symbols) 组合，避免同一批扫描结果反复推
_last_fingerprint: str = ""

# tier 子类对应的 emoji 标签，让消息更直观
_SUBTYPE_EMOJI = {
    # L1
    "OI爆发": "🌋",
    "加速中": "⚡",
    "妖币启动": "🚀",
    "L1常规": "🔴",
    # L2
    "突破前夜": "📦",
    "静默建仓": "🤫",
    "早期启动": "📈",
    "L2常规": "🟡",
    # RISK
    "出货家族": "🦘",
    "FR警告": "🔥",
    "AI高风险": "⚠",
    "链上风险": "⛔",
    "OI转弱": "📉",
    "已破位": "💀",
}

_TIER_PRIORITY = {"L1_MAIN": 3, "L2_AMBUSH": 2, "RISK_AVOID": 1}


def _tier_threshold(min_tier: str) -> int:
    """根据 min_tier 字符串返回最低优先级阈值。"""
    return _TIER_PRIORITY.get((min_tier or "L2_AMBUSH").upper(), 2)


def _filter_by_tier(candidates: list[dict], wanted_tiers: set[str]) -> list[dict]:
    return [c for c in candidates if (c.get("decision_tier") or "") in wanted_tiers]


def summarize_scan(candidates: list[dict]) -> dict | None:
    """
    把扫描结果按 tier 汇总。返回 None 表示没什么值得推送的。
    """
    cfg = config_manager.settings
    if not cfg.get("YAOBI_NOTIFIER_ENABLED", False):
        return None
    max_per = int(cfg.get("YAOBI_NOTIFIER_MAX_PER_BATCH", 8) or 8)

    l1 = sorted(
        _filter_by_tier(candidates, {"L1_MAIN"}),
        key=lambda x: int(x.get("score") or 0),
        reverse=True,
    )[:max_per]
    l2 = sorted(
        _filter_by_tier(candidates, {"L2_AMBUSH"}),
        key=lambda x: int(x.get("score") or 0),
        reverse=True,
    )[:max_per]

    min_tier = str(cfg.get("YAOBI_NOTIFIER_MIN_TIER", "L2_AMBUSH") or "L2_AMBUSH").upper()
    risk: list[dict] = []
    if min_tier == "ALL":
        risk = sorted(
            _filter_by_tier(candidates, {"RISK_AVOID"}),
            key=lambda x: int(x.get("score") or 0),
            reverse=True,
        )[:max_per]

    if not l1 and not l2 and not risk:
        return None

    now = datetime.now().strftime("%H:%M")
    return {
        "header": f"[妖币雷达 {now}] 新增 {len(l1) + len(l2)} 个信号 (L1×{len(l1)} / L2×{len(l2)})",
        "l1": l1,
        "l2": l2,
        "risk": risk,
    }


def _fmt_card(c: dict) -> str:
    sym = c.get("symbol") or "?"
    subtype = c.get("decision_subtype") or ""
    emoji = _SUBTYPE_EMOJI.get(subtype, "•")
    score = int(c.get("score") or 0)
    raw = int(c.get("score_raw") or 0)
    chg = float(c.get("price_change_24h") or 0.0)
    oi = float(c.get("oi_change_24h_pct") or 0.0)
    chg_str = f"{chg:+.1f}%" if chg else "—"
    oi_str = f"OI{oi:+.0f}%" if oi else ""
    raw_str = f"raw:{raw}" if raw and raw != score else ""
    bits = [b for b in (chg_str, oi_str, raw_str) if b]
    return f"{emoji} `{sym}` {score}/100 · {' · '.join(bits)}"


def format_telegram_text(summary: dict) -> str:
    """渲染成 Telegram MarkdownV2 友好的文本（这里用普通 Markdown 因为 V2 转义太烦）。"""
    lines: list[str] = [f"*{summary.get('header', '')}*"]
    if summary.get("l1"):
        lines.append("\n🔴 *L1 主战场*（追涨第一仓）")
        for c in summary["l1"]:
            lines.append(_fmt_card(c))
    if summary.get("l2"):
        lines.append("\n🟡 *L2 埋伏型*（等启动）")
        for c in summary["l2"]:
            lines.append(_fmt_card(c))
    if summary.get("risk"):
        lines.append("\n⚠ *风险预警*（避险）")
        for c in summary["risk"]:
            lines.append(_fmt_card(c))
    return "\n".join(lines)


def format_discord_text(summary: dict) -> str:
    # Discord 也支持基础 Markdown，复用同一个渲染
    return format_telegram_text(summary)


async def _send_telegram(session: aiohttp.ClientSession, text: str) -> str:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return "skipped: no_token"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with session.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                return f"failed: HTTP{resp.status} {body[:120]}"
            return "ok"
    except Exception as e:
        return f"failed: {type(e).__name__}: {e}"


async def _send_discord(session: aiohttp.ClientSession, text: str) -> str:
    if not DISCORD_WEBHOOK_URL:
        return "skipped: no_webhook"
    try:
        # Discord webhook 单条限 2000 字符
        truncated = text if len(text) <= 1900 else text[:1900] + "\n…(truncated)"
        async with session.post(
            DISCORD_WEBHOOK_URL,
            json={"content": truncated},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                return f"failed: HTTP{resp.status} {body[:120]}"
            return "ok"
    except Exception as e:
        return f"failed: {type(e).__name__}: {e}"


def _fingerprint(summary: dict) -> str:
    parts = []
    for tier_key in ("l1", "l2", "risk"):
        for c in summary.get(tier_key, []):
            parts.append(f"{tier_key}:{c.get('symbol', '?')}:{c.get('score', 0)}")
    return "|".join(parts)


async def push_yaobi_scan_summary(
    candidates: list[dict],
    session: aiohttp.ClientSession | None = None,
) -> dict:
    """
    入口：把候选汇总后推送到 Telegram + Discord。
    失败不抛异常。返回 {"telegram": "...", "discord": "...", "skipped": "原因"}.

    session 可选；不传时内部自建独立 session（用于 yaobi_scanner 等没有持久 session 的场景）。
    """
    global _last_fingerprint

    cfg = config_manager.settings
    if not cfg.get("YAOBI_NOTIFIER_ENABLED", False):
        return {"skipped": "notifier_disabled"}

    summary = summarize_scan(candidates)
    if summary is None:
        return {"skipped": "no_actionable_signals"}

    fp = _fingerprint(summary)
    if fp == _last_fingerprint:
        return {"skipped": "same_as_last_scan"}
    _last_fingerprint = fp

    text = format_telegram_text(summary)
    if session is None:
        async with aiohttp.ClientSession(trust_env=True) as own_session:
            tg_result = await _send_telegram(own_session, text)
            dc_result = await _send_discord(own_session, format_discord_text(summary))
    else:
        tg_result = await _send_telegram(session, text)
        dc_result = await _send_discord(session, format_discord_text(summary))

    if tg_result == "ok" or dc_result == "ok":
        logger.info(
            "📡 妖币雷达推送 | L1×%d L2×%d RISK×%d | TG=%s | Discord=%s",
            len(summary["l1"]), len(summary["l2"]), len(summary["risk"]),
            tg_result, dc_result,
        )
    return {"telegram": tg_result, "discord": dc_result, "summary_size": {
        "l1": len(summary["l1"]), "l2": len(summary["l2"]), "risk": len(summary["risk"]),
    }}
