"""
币安广场热帖抓取 — best-effort 读取源

说明:
- Binance Square 公开读取端点没有稳定官方文档，网页端经常受 AWS WAF / 403 影响。
- 本模块先尝试公开 bapi 端点；如果环境变量提供浏览器 Cookie，则自动带上 Cookie/CSRF 再试。
- 失败时不阻塞妖币扫描器，诊断接口会暴露端点状态，方便判断是否需要更新 Cookie。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from config import (
    BINANCE_SQUARE_BNC_UUID,
    BINANCE_SQUARE_COOKIE,
    BINANCE_SQUARE_CSRF_TOKEN,
    BINANCE_SQUARE_OPENAPI_KEY,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Endpoint:
    name: str
    method: str
    url: str
    variants: tuple[dict[str, Any], ...]


def _rows(n: int) -> int:
    return max(1, min(int(n), 100))


def _endpoint_specs(rows: int) -> list[_Endpoint]:
    r = _rows(rows)
    get_variants = (
        {"page": 1, "rows": r, "type": "recommend"},
        {"page": 1, "size": r, "type": "HOT"},
        {"page": 1, "rows": r, "type": "hot"},
        {"page": 1, "rows": r},
        {"cursor": "", "limit": r},
    )
    pgc_variants = (
        {"page": 1, "rows": r, "type": "recommend"},
        {"pageIndex": 1, "pageSize": r, "scene": "WEB"},
        {"pageNo": 1, "pageSize": r, "scene": "WEB"},
        {"cursor": "", "limit": r, "scene": "WEB"},
        {"size": r, "scene": "WEB"},
    )
    return [
        _Endpoint("square_friendly_list", "GET",
                  "https://www.binance.com/bapi/square/v1/friendly/square/post/list", get_variants),
        _Endpoint("feed_friendly_posts", "GET",
                  "https://www.binance.com/bapi/feed/v1/friendly/feed/posts", get_variants),
        _Endpoint("social_square_list", "GET",
                  "https://www.binance.com/bapi/social/v2/public/square/post/list", get_variants),
        _Endpoint("social_community_list", "GET",
                  "https://www.binance.com/bapi/social/v1/public/feed/community/post/list", get_variants),
        _Endpoint("topic_home_posts", "GET",
                  "https://www.binance.com/bapi/social/v1/public/topic/home/posts", get_variants),
        _Endpoint("square_homepage_stream", "GET",
                  "https://www.binance.com/bapi/square/v1/public/square/homepage-stream", get_variants),
        _Endpoint("pgc_content_list_get", "GET",
                  "https://www.binance.com/bapi/composite/v1/public/pgc/content/list", pgc_variants),
        _Endpoint("pgc_contents_get", "GET",
                  "https://www.binance.com/bapi/composite/v1/public/pgc/contents", pgc_variants),
        _Endpoint("pgc_content_list_post", "POST",
                  "https://www.binance.com/bapi/composite/v1/public/pgc/content/list", pgc_variants),
        _Endpoint("pgc_contents_post", "POST",
                  "https://www.binance.com/bapi/composite/v1/public/pgc/contents", pgc_variants),
    ]


def _headers() -> dict[str, str]:
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer":         "https://www.binance.com/en/square",
        "Origin":          "https://www.binance.com",
        "clienttype":      "web",
        "lang":            "zh-CN",
    }
    if BINANCE_SQUARE_COOKIE:
        h["Cookie"] = BINANCE_SQUARE_COOKIE
    if BINANCE_SQUARE_CSRF_TOKEN:
        h["csrftoken"] = BINANCE_SQUARE_CSRF_TOKEN
        h["x-csrf-token"] = BINANCE_SQUARE_CSRF_TOKEN
    if BINANCE_SQUARE_BNC_UUID:
        h["bnc-uuid"] = BINANCE_SQUARE_BNC_UUID
    if BINANCE_SQUARE_OPENAPI_KEY:
        h["X-Square-OpenAPI-Key"] = BINANCE_SQUARE_OPENAPI_KEY
    return h


def auth_status() -> dict:
    return {
        "cookie_set":      bool(BINANCE_SQUARE_COOKIE),
        "csrf_set":        bool(BINANCE_SQUARE_CSRF_TOKEN),
        "bnc_uuid_set":    bool(BINANCE_SQUARE_BNC_UUID),
        "openapi_key_set": bool(BINANCE_SQUARE_OPENAPI_KEY),
    }


_TICKER_RE = re.compile(r"\$([A-Z]{2,10})\b|\b([A-Z]{2,10})USDT\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


async def fetch_hot_posts(
    session: aiohttp.ClientSession,
    rows: int = 50,
) -> list[dict]:
    """尝试所有端点，返回热帖列表。"""
    diagnostics = []
    for spec in _endpoint_specs(rows):
        posts, meta = await _try_endpoint(session, spec)
        diagnostics.append(meta)
        if posts:
            logger.info("币安广场: 抓取到 %d 条热帖 (%s)", len(posts), spec.name)
            return posts[:_rows(rows)]
    status = ", ".join(f"{d['name']}:{d.get('status')}" for d in diagnostics[:4])
    logger.warning("币安广场: 所有端点均失败，跳过 (%s)", status)
    return []


async def _try_endpoint(
    session: aiohttp.ClientSession,
    spec: _Endpoint,
) -> tuple[list[dict], dict]:
    last_meta = {"name": spec.name, "method": spec.method, "status": None, "posts": 0}
    for variant in spec.variants:
        try:
            kwargs = {"headers": _headers(), "timeout": aiohttp.ClientTimeout(total=15)}
            if spec.method == "GET":
                kwargs["params"] = variant
            else:
                kwargs["json"] = variant
            async with session.request(spec.method, spec.url, **kwargs) as resp:
                text = await resp.text()
                last_meta = {
                    "name": spec.name,
                    "method": spec.method,
                    "status": resp.status,
                    "content_type": resp.headers.get("content-type", ""),
                    "posts": 0,
                }
                if resp.status != 200:
                    continue
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    logger.debug("币安广场 %s 返回非JSON: %s", spec.name, text[:160])
                    continue
                posts = _extract_posts(data)
                last_meta["posts"] = len(posts)
                code = data.get("code") if isinstance(data, dict) else None
                if code:
                    last_meta["code"] = code
                if posts:
                    return posts, last_meta
        except Exception as e:
            last_meta = {
                "name": spec.name,
                "method": spec.method,
                "status": "exception",
                "error": f"{type(e).__name__}: {str(e)[:120]}",
                "posts": 0,
            }
    return [], last_meta


def _extract_posts(data: dict | list) -> list[dict]:
    """从各种响应结构中递归提取帖子。"""
    results: list[dict] = []
    seen: set[str] = set()
    for item in _iter_dict_items(data):
        text = _extract_text(item)
        if not text:
            continue
        pid = _extract_id(item) or text[:80]
        if pid in seen:
            continue
        seen.add(pid)
        likes = _safe_int(item, "likeCount", "like_count", "likes", "likes_count", "likeNum")
        reposts = _safe_int(item, "repostCount", "repost_count", "shares", "shareCount")
        views = _safe_int(item, "viewCount", "view_count", "views", "viewNum", "readCount")
        heat = likes * 3 + reposts * 5 + views // 100
        results.append({
            "text":    text[:300],
            "likes":   likes,
            "reposts": reposts,
            "views":   views,
            "heat":    heat,
            "url":     _extract_url(item),
            "ts":      datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        if len(results) >= 100:
            break
    return results


def _iter_dict_items(root: Any):
    stack = [root]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            for item in reversed(cur):
                stack.append(item)
        elif isinstance(cur, dict):
            if _looks_like_post(cur):
                yield cur
            for val in cur.values():
                if isinstance(val, (dict, list)):
                    stack.append(val)


def _looks_like_post(d: dict) -> bool:
    post_markers = (
        "id", "postId", "contentId", "articleId", "likeCount", "viewCount",
        "likeNum", "viewNum", "author", "user", "publisher",
    )
    return bool(_extract_text(d) and any(k in d for k in post_markers))


def _extract_text(d: dict) -> str:
    parts: list[str] = []
    for key in (
        "bodyTextOnly", "content", "body", "text", "title", "summary",
        "shareSummary", "description", "postContent", "shortContent",
    ):
        val = d.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, dict):
            nested = _extract_text(val)
            if nested:
                parts.append(nested)
    text = " ".join(parts).strip()
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_id(d: dict) -> str:
    for key in ("id", "postId", "contentId", "articleId"):
        val = d.get(key)
        if val:
            return str(val)
    return ""


def _safe_int(d: dict, *keys: str) -> int:
    for key in keys:
        val = _nested_value(d, key)
        if val is not None:
            try:
                return int(float(val))
            except (TypeError, ValueError):
                pass
    return 0


def _nested_value(root: Any, key: str):
    stack = [root]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


def _extract_url(post: dict) -> str:
    for key in ("postUrl", "url", "shareUrl", "link"):
        val = post.get(key, "")
        if isinstance(val, str) and val.startswith("http"):
            return val
    pid = _extract_id(post)
    return f"https://www.binance.com/en/square/post/{pid}" if pid else ""


def extract_ticker_mentions(posts: list[dict]) -> dict[str, dict]:
    """
    从帖子列表中提取 $TICKER 提及统计。
    返回: {SYMBOL: {count, total_heat, posts: [...]}}
    """
    ignore = {
        "THE", "FOR", "AND", "NOT", "BUT", "ARE", "YOU", "CAN",
        "ALL", "NEW", "BUY", "TOP", "GET", "USD", "ETH", "BTC",
        "THIS", "FROM", "WITH", "WILL", "HAVE", "BEEN", "JUST",
        "THAT", "YOUR", "INTO", "THEY", "WHEN", "WHAT", "USDT",
    }
    mention_map: dict[str, dict] = {}
    for post in posts:
        text = post.get("text", "")
        heat = post.get("heat", 0)
        tickers = set()
        for match in _TICKER_RE.finditer(text):
            sym = (match.group(1) or match.group(2) or "").upper()
            if sym and len(sym) >= 2 and sym not in ignore:
                tickers.add(sym)
        for sym in tickers:
            if sym not in mention_map:
                mention_map[sym] = {"count": 0, "total_heat": 0, "posts": []}
            mention_map[sym]["count"] += 1
            mention_map[sym]["total_heat"] += heat
            if len(mention_map[sym]["posts"]) < 3:
                mention_map[sym]["posts"].append({
                    "text": text[:100],
                    "heat": heat,
                    "url": post.get("url", ""),
                })
    return mention_map


async def diagnose(session: aiohttp.ClientSession, rows: int = 5) -> dict:
    endpoints = []
    ok = False
    for spec in _endpoint_specs(rows):
        posts, meta = await _try_endpoint(session, spec)
        endpoints.append(meta)
        ok = ok or bool(posts)
        if ok:
            break
    return {
        **auth_status(),
        "ok": ok,
        "endpoints": endpoints,
    }
