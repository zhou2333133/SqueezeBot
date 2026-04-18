"""
币安广场热帖抓取 — 内部非官方 API（best-effort，可能随时变更）
功能: 获取热门帖子 → 提取$TICKER提及 → 统计社交热度
"""
from __future__ import annotations
import logging
import re
from datetime import datetime

import aiohttp

logger = logging.getLogger(__name__)

# 尝试多个可能的端点
_ENDPOINTS = [
    "https://www.binance.com/bapi/social/v1/public/feed/community/post/list",
    "https://www.binance.com/bapi/social/v1/public/topic/home/posts",
    "https://www.binance.com/bapi/square/v1/public/square/homepage-stream",
]

_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":          "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer":         "https://www.binance.com/en/square",
    "Origin":          "https://www.binance.com",
}

_TICKER_RE = re.compile(r'\$([A-Z]{2,10})\b|\b([A-Z]{2,10})USDT\b', re.IGNORECASE)


async def fetch_hot_posts(
    session: aiohttp.ClientSession,
    rows: int = 50,
) -> list[dict]:
    """尝试所有端点，返回热帖列表"""
    for url in _ENDPOINTS:
        posts = await _try_endpoint(session, url, rows)
        if posts:
            logger.info("币安广场: 抓取到 %d 条热帖 (%s)", len(posts), url.split("/")[-1])
            return posts
    logger.warning("币安广场: 所有端点均失败，跳过")
    return []


async def _try_endpoint(session: aiohttp.ClientSession, url: str, rows: int) -> list[dict]:
    for params in [
        {"page": 1, "rows": rows, "type": "hot"},
        {"page": 1, "rows": rows},
        {"cursor": "", "limit": rows},
    ]:
        try:
            async with session.get(
                url, params=params, headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    posts = _extract_posts(data)
                    if posts:
                        return posts
        except Exception as e:
            logger.debug("币安广场端点 %s 异常: %s", url, e)
    return []


def _extract_posts(data: dict | list) -> list[dict]:
    """从各种响应结构中提取帖子"""
    raw_posts: list = []

    if isinstance(data, list):
        raw_posts = data
    elif isinstance(data, dict):
        for key in ("data", "posts", "list", "items", "content", "result"):
            val = data.get(key)
            if isinstance(val, list):
                raw_posts = val
                break
            if isinstance(val, dict):
                for sub in ("posts", "list", "items", "content"):
                    if isinstance(val.get(sub), list):
                        raw_posts = val[sub]
                        break
                if raw_posts:
                    break

    results = []
    for post in raw_posts[:100]:
        if not isinstance(post, dict):
            continue
        try:
            # 提取文本内容（字段名因版本而异）
            text = ""
            for f in ("content", "body", "text", "title", "summary", "shareSummary"):
                v = post.get(f, "")
                if isinstance(v, str) and v:
                    text += v + " "

            if not text.strip():
                continue

            # 统计互动数据
            likes  = _safe_int(post, "likeCount", "like_count", "likes", "likes_count")
            reposts= _safe_int(post, "repostCount", "repost_count", "shares")
            views  = _safe_int(post, "viewCount",  "view_count",  "views")
            heat   = likes * 3 + reposts * 5 + views // 100

            results.append({
                "text":    text.strip()[:300],
                "likes":   likes,
                "reposts": reposts,
                "views":   views,
                "heat":    heat,
                "url":     _extract_url(post),
                "ts":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
        except Exception:
            continue
    return results


def _safe_int(d: dict, *keys: str) -> int:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return 0


def _extract_url(post: dict) -> str:
    for k in ("postUrl", "url", "shareUrl", "link"):
        v = post.get(k, "")
        if isinstance(v, str) and v.startswith("http"):
            return v
    pid = post.get("id") or post.get("postId") or ""
    return f"https://www.binance.com/en/square/post/{pid}" if pid else ""


def extract_ticker_mentions(posts: list[dict]) -> dict[str, dict]:
    """
    从帖子列表中提取 $TICKER 提及统计
    返回: {SYMBOL: {count, total_heat, posts: [...]}}
    """
    # 过滤常见非币 token 词汇
    IGNORE = {"THE", "FOR", "AND", "NOT", "BUT", "ARE", "YOU", "CAN",
               "ALL", "NEW", "BUY", "TOP", "GET", "USD", "ETH", "BTC",
               "THIS", "FROM", "WITH", "WILL", "HAVE", "BEEN", "JUST",
               "THAT", "YOUR", "INTO", "THEY", "WHEN", "WHAT"}

    mention_map: dict[str, dict] = {}
    for post in posts:
        text  = post.get("text", "")
        heat  = post.get("heat", 0)
        tickers = set()
        for m in _TICKER_RE.finditer(text):
            sym = (m.group(1) or m.group(2) or "").upper()
            if sym and len(sym) >= 2 and sym not in IGNORE:
                tickers.add(sym)
        for sym in tickers:
            if sym not in mention_map:
                mention_map[sym] = {"count": 0, "total_heat": 0, "posts": []}
            mention_map[sym]["count"]      += 1
            mention_map[sym]["total_heat"] += heat
            if len(mention_map[sym]["posts"]) < 3:
                mention_map[sym]["posts"].append({
                    "text": post["text"][:100],
                    "heat": heat,
                    "url":  post["url"],
                })
    return mention_map
