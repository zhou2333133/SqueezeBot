"""
币安广场热帖抓取 — 浏览器公开页面读取源

说明:
- 固定 HTTP 端点已多次失效；主路径改为 Playwright 打开公开 Square 页面。
- 本模块只监听页面自身加载的 JSON 响应，不提取、不打印、不上传 cookie/token。
- 如页面要求登录，只允许用户在本机浏览器窗口手动登录自己的账号。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from config import (
    BINANCE_SQUARE_BNC_UUID,
    BINANCE_SQUARE_COOKIE,
    BINANCE_SQUARE_CSRF_TOKEN,
    BINANCE_SQUARE_OPENAPI_KEY,
    config_manager,
)

logger = logging.getLogger(__name__)

SQUARE_URL = "https://www.binance.com/en/square"
USER_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "binance_square_browser_profile"
_RESPONSE_KEYWORDS = ("pgc/feed", "bapi", "square", "feed")
_LIST_KEYS = ("vos", "list", "items", "feedList", "posts", "content", "contents")
_TICKER_RE = re.compile(r"\$([A-Z]{2,10})\b|\b([A-Z]{2,10})USDT\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_LAST_BROWSER_DIAG: dict[str, Any] = {}


def _rows(n: int) -> int:
    return max(1, min(int(n), 100))


def _setting(name: str, default: Any) -> Any:
    return config_manager.settings.get(name, default)


def _playwright_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


def auth_status() -> dict:
    return {
        "cookie_set":      bool(BINANCE_SQUARE_COOKIE),
        "csrf_set":        bool(BINANCE_SQUARE_CSRF_TOKEN),
        "bnc_uuid_set":    bool(BINANCE_SQUARE_BNC_UUID),
        "openapi_key_set": bool(BINANCE_SQUARE_OPENAPI_KEY),
    }


def last_diagnostics() -> dict:
    return dict(_LAST_BROWSER_DIAG or _browser_meta())


def _browser_meta(**overrides: Any) -> dict[str, Any]:
    meta = {
        **auth_status(),
        "http_ok": False,
        "endpoints": [],
        "playwright_installed": _playwright_installed(),
        "browser_enabled": bool(_setting("YAOBI_SQUARE_BROWSER_ENABLED", True)),
        "browser_ok": False,
        "browser_posts": 0,
        "last_error": None,
        "requires_manual_login": False,
        "last_success_method": "none",
        "install_hint": None,
    }
    meta.update(overrides)
    if meta.get("browser_ok"):
        meta["last_success_method"] = "browser"
    return meta


async def fetch_hot_posts(
    session: aiohttp.ClientSession,
    rows: int = 50,
) -> list[dict]:
    """使用浏览器公开页面采集热帖。session 保留为调用方兼容参数。"""
    _ = session
    global _LAST_BROWSER_DIAG
    if not bool(_setting("YAOBI_SQUARE_BROWSER_ENABLED", True)):
        _LAST_BROWSER_DIAG = _browser_meta(
            browser_ok=False,
            browser_posts=0,
            last_error="browser_disabled",
        )
        logger.warning("币安广场: 浏览器采集已关闭")
        return []

    posts, meta = await _fetch_browser_posts(rows=rows)
    _LAST_BROWSER_DIAG = meta
    if posts:
        logger.info("币安广场: 浏览器采集到 %d 条热帖", len(posts))
    else:
        logger.warning("币安广场: 浏览器采集无结果 (%s)", meta.get("last_error") or "empty")
    return posts[:_rows(rows)]


async def _fetch_browser_posts(
    rows: int = 50,
    duration_seconds: int | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    if not _playwright_installed():
        return [], _browser_meta(
            browser_ok=False,
            browser_posts=0,
            last_error="playwright_not_installed",
            install_hint="pip install playwright && python -m playwright install chromium",
        )

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        return [], _browser_meta(
            browser_ok=False,
            browser_posts=0,
            last_error=f"playwright_import_failed:{type(e).__name__}",
            install_hint="pip install playwright && python -m playwright install chromium",
        )

    limit = _rows(rows)
    duration = int(duration_seconds if duration_seconds is not None else _setting("YAOBI_SQUARE_BROWSER_SECONDS", 45))
    duration = max(5, min(duration, 300))
    pause = float(_setting("YAOBI_SQUARE_SCROLL_PAUSE_SECONDS", 2))
    pause = max(0.5, min(pause, 10.0))
    reset_every = int(_setting("YAOBI_SQUARE_SCROLL_RESET_EVERY", 20))
    reset_every = max(5, min(reset_every, 200))
    headless = bool(_setting("YAOBI_SQUARE_BROWSER_HEADLESS", True))

    captured: list[dict] = []
    seen: set[str] = set()
    requires_manual_login = False
    last_error: str | None = None

    def merge_posts(posts: list[dict]) -> None:
        for post in posts:
            key = str(post.get("id") or post.get("url") or post.get("text", "")[:120])
            if not key or key in seen:
                continue
            seen.add(key)
            captured.append(post)

    async def handle_response(response) -> None:
        if not _is_square_json_response(response):
            return
        try:
            data = await response.json()
        except Exception:
            return
        merge_posts(_extract_posts(data, limit=limit * 3))

    try:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=headless,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("response", handle_response)

            try:
                await page.goto(SQUARE_URL, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(3_000)
                requires_manual_login = await _page_requires_manual_login(page)
                start = datetime.now().timestamp()
                scrolls = 0
                while datetime.now().timestamp() - start < duration and len(captured) < limit:
                    await page.mouse.wheel(0, 4000)
                    scrolls += 1
                    await page.wait_for_timeout(int(pause * 1000))
                    if scrolls % reset_every == 0 and len(captured) < limit:
                        try:
                            await page.goto(SQUARE_URL, wait_until="domcontentloaded", timeout=60_000)
                            await page.wait_for_timeout(2_500)
                        except Exception as e:
                            last_error = f"page_refresh_failed:{type(e).__name__}"
            finally:
                await context.close()
    except Exception as e:
        msg = str(e).replace(os.linesep, " ")[:180]
        err_type = type(e).__name__
        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
            last_error = "chromium_not_installed"
        else:
            last_error = f"{err_type}:{msg}"

    posts = captured[:limit]
    browser_ok = bool(posts)
    if not browser_ok and not last_error:
        last_error = "no_posts_captured"
    return posts, _browser_meta(
        browser_ok=browser_ok,
        browser_posts=len(posts),
        last_error=last_error,
        requires_manual_login=requires_manual_login and not browser_ok,
        install_hint=(
            "python -m playwright install chromium"
            if last_error == "chromium_not_installed"
            else None
        ),
    )


def _is_square_json_response(response) -> bool:
    url = (getattr(response, "url", "") or "").lower()
    if not any(k in url for k in _RESPONSE_KEYWORDS):
        return False
    headers = getattr(response, "headers", {}) or {}
    content_type = (headers.get("content-type") or "").lower()
    return "json" in content_type or "/bapi/" in url


async def _page_requires_manual_login(page) -> bool:
    try:
        url = (page.url or "").lower()
        if "login" in url:
            return True
        text = (await page.locator("body").inner_text(timeout=2_000)).lower()
        markers = (
            "log in",
            "sign in",
            "login",
            "verify",
            "captcha",
            "please enable cookies",
            "登录",
            "验证",
            "验证码",
        )
        return any(marker in text for marker in markers)
    except Exception:
        return False


def _extract_posts(data: dict | list, limit: int = 100) -> list[dict]:
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
        comments = _safe_int(item, "commentCount", "comment_count", "comments", "commentNum")
        reposts = _safe_int(item, "repostCount", "repost_count", "shares", "shareCount")
        views = _safe_int(item, "viewCount", "view_count", "views", "viewNum", "readCount")
        heat = likes * 3 + comments * 4 + reposts * 5 + views // 100
        results.append({
            "id":       pid,
            "text":     text[:300],
            "likes":    likes,
            "comments": comments,
            "reposts":  reposts,
            "views":    views,
            "heat":     heat,
            "url":      _extract_url(item),
            "ts":       datetime.now().strftime("%Y-%m-%d %H:%M"),
            "tokens":   sorted(_extract_embedded_tokens(item)),
        })
        if len(results) >= limit:
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
            for key in _LIST_KEYS:
                val = cur.get(key)
                if isinstance(val, list) and val:
                    for item in reversed(val):
                        stack.append(item)
            for val in cur.values():
                if isinstance(val, (dict, list)):
                    stack.append(val)


def _looks_like_post(d: dict) -> bool:
    post_markers = (
        "id", "postId", "contentId", "articleId", "handWork", "likeCount",
        "viewCount", "commentCount", "quoteCount", "shareCount", "likeNum",
        "viewNum", "author", "authorName", "squareAuthorId", "user",
        "publisher", "tradingPairs", "tradingPairsV2", "coinPairList",
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
    for key in ("id", "postId", "contentId", "articleId", "handWork"):
        val = d.get(key)
        if val:
            return str(val)
    return ""


def _extract_embedded_tokens(d: dict) -> set[str]:
    tokens: set[str] = set()
    for key in ("tradingPairs", "tradingPairsV2"):
        for item in d.get(key) or []:
            if isinstance(item, dict):
                for code_key in ("code", "symbol", "baseAsset", "coin"):
                    code = item.get(code_key)
                    if isinstance(code, str) and code.strip():
                        tokens.add(_normalize_symbol(code))
            elif isinstance(item, str):
                tokens.add(_normalize_symbol(item))
    for item in d.get("coinPairList") or []:
        if isinstance(item, str):
            tokens.add(_normalize_symbol(item))
        elif isinstance(item, dict):
            for code_key in ("code", "symbol", "baseAsset", "coin"):
                code = item.get(code_key)
                if isinstance(code, str) and code.strip():
                    tokens.add(_normalize_symbol(code))
    return {t for t in tokens if t}


def _normalize_symbol(value: str) -> str:
    sym = value.strip().upper().lstrip("$#")
    if sym.endswith("USDT") and len(sym) > 4:
        sym = sym[:-4]
    return re.sub(r"[^A-Z0-9]", "", sym)


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
        tickers = set(post.get("tokens") or [])
        for match in _TICKER_RE.finditer(text):
            sym = (match.group(1) or match.group(2) or "").upper()
            if sym and len(sym) >= 2 and sym not in ignore:
                tickers.add(sym)
        for sym_raw in tickers:
            sym = _normalize_symbol(str(sym_raw))
            if not sym or len(sym) < 2 or sym in ignore:
                continue
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
    _ = session
    if not bool(_setting("YAOBI_SQUARE_BROWSER_ENABLED", True)):
        return _browser_meta(
            ok=False,
            browser_ok=False,
            browser_posts=0,
            last_error="browser_disabled",
        )
    posts, meta = await _fetch_browser_posts(
        rows=rows,
        duration_seconds=min(int(_setting("YAOBI_SQUARE_BROWSER_SECONDS", 45)), 15),
    )
    meta["ok"] = bool(posts)
    return meta
