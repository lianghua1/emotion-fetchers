"""OKX Orbit / Planet: coin search + topic detail (JSON API, no browser)."""

from __future__ import annotations

import time
from typing import Any

from .http_client import HybridClient, default_client
from .lang import LangFilter, prefer_lang_then_fill
from .models import SocialPost, SortMode, dedupe_posts, sort_posts
from .okx_siteinfo import site_info_from_env


def _past_deadline(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline

SEARCH_URL = "https://www.okx.com/priapi/v5/rubik/public/content/search/1"
TOPIC_DETAIL_URL = "https://www.okx.com/priapi/v5/rubik/content/topic/detail/1"
TOPIC_LIST_URL = "https://www.okx.com/priapi/v5/rubik/content/topic/list/1"


def _response_object(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and "_raw" in data:
        status = data.get("_status")
        raw = str(data.get("_raw") or "").lower()
        if "cloudflare" in raw or "just a moment" in raw or "challenge" in raw:
            raise RuntimeError("OKX Orbit endpoint was blocked by Cloudflare; change the egress or retry later.")
        raise RuntimeError(f"OKX Orbit endpoint returned non-JSON data (HTTP {status or 'unknown'}).")
    return data if isinstance(data, dict) else {}


def _headers(site_info: str | None = None) -> dict[str, str]:
    return {
        "accept": "application/json",
        "origin": "https://www.okx.com",
        "referer": "https://www.okx.com/",
        "x-site-info": site_info or site_info_from_env(),
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }


def _images_from(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("imageList", "images", "coverPhoto"):
        val = item.get(key)
        if not val:
            continue
        if isinstance(val, str):
            out.append(val)
        elif isinstance(val, list):
            for x in val:
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict):
                    u = x.get("url") or x.get("imageUrl")
                    if u:
                        out.append(str(u))
    return out


def _unwrap_content(item: dict[str, Any]) -> dict[str, Any]:
    if isinstance(item.get("contentData"), dict):
        return item["contentData"]
    return item


def _post_from(item: dict[str, Any], coin: str) -> SocialPost | None:
    body = _unwrap_content(item)
    pid = str(body.get("contentId") or body.get("id") or "")
    if not pid:
        return None
    text = str(body.get("content") or body.get("title") or body.get("excerpt") or "")
    author = ""
    u = body.get("author") or body.get("userInfo") or body.get("creator")
    if isinstance(u, dict):
        author = str(u.get("nickName") or u.get("nickname") or u.get("userName") or "")

    like = int(body.get("likeCount") or 0)
    comment = int(body.get("commentNum") or body.get("commentCount") or 0)
    view = int(body.get("viewCount") or body.get("twitterViewCount") or 0)
    share = int(body.get("quoteCount") or body.get("shareCount") or body.get("retweetTotal") or 0)
    ts = int(body.get("publishTime") or body.get("createTime") or body.get("timestamp") or 0)
    if ts and ts < 1_000_000_000_000:
        ts *= 1000

    url = str(body.get("shareUrl") or f"https://www.okx.com/zh-hans/orbit/post/{pid}")
    detected = str(
        body.get("detectedLanguage")
        or body.get("language")
        or body.get("lang")
        or body.get("contentLanguage")
        or ""
    )

    return SocialPost(
        source="okx",
        id=pid,
        text=text,
        author=author,
        like_count=like,
        comment_count=comment,
        view_count=view,
        share_count=share,
        publish_time_ms=ts,
        images=_images_from(body),
        url=url,
        coin=coin.upper(),
        detected_language=detected,
        raw=item,
    )


def search_by_coin(
    coin: str,
    *,
    mode: SortMode = "latest",
    size: int = 50,
    cursor: int | str = 0,
    max_pages: int | None = None,
    client: HybridClient | None = None,
    site_info: str | None = None,
    lang: LangFilter = "all",
    deadline: float | None = None,
) -> list[SocialPost]:
    """
    coinName 搜索。接口本身偏时间序；热度靠多页后本地重排。
    需要非港区出口（JP 住宅代理更稳）。
    lang：all|zh|en —— 无可靠服务端语种参，文案启发式过滤。
    deadline：monotonic 截止；超时前返回已收集部分。
    """
    c = client or default_client()
    tag = coin.strip().upper()
    page_size = min(max(size, 20), 50)
    lang_boost = 4 if lang != "all" else 1
    pages = max_pages if max_pages is not None else (
        (10 if mode == "hot" else max(2, (size + 19) // 20)) * lang_boost
    )
    # hot 排序靠本地重排，10 页足够；翻太多会在多源预算内超时
    pages = min(pages, 10 if mode == "hot" else 30)

    collected: list[SocialPost] = []
    cur: int | str = cursor
    for _ in range(pages):
        if _past_deadline(deadline):
            break
        data = _response_object(c.get_json(
            SEARCH_URL,
            headers=_headers(site_info),
            params={"coinName": tag, "size": page_size, "cursor": cur},
        ))
        payload = (data or {}).get("data") or {}
        rows = (
            payload.get("contentDataList")
            or payload.get("dataList")
            or payload.get("list")
            or payload.get("contents")
            or (payload if isinstance(payload, list) else [])
        )
        if not rows:
            break
        for item in rows:
            if isinstance(item, dict):
                p = _post_from(item, tag)
                if p:
                    collected.append(p)
        nxt = payload.get("nextCursor") if isinstance(payload, dict) else None
        if nxt in (None, "", cur):
            break
        cur = nxt
        filtered_n = len(prefer_lang_then_fill(dedupe_posts(collected), lang, min_keep=size))
        if filtered_n >= size:
            break

    return sort_posts(prefer_lang_then_fill(dedupe_posts(collected), lang), mode)[:size]


def search_by_coin_chunk(
    coin: str,
    *,
    size: int = 30,
    cursor: int | str = 0,
    max_scan: int = 6,
    client: HybridClient | None = None,
    site_info: str | None = None,
    lang: LangFilter = "all",
) -> tuple[list[SocialPost], str | int | None, bool]:
    """
    时间序增量一截。返回 (posts, next_cursor, has_more)。
    """
    c = client or default_client()
    tag = coin.strip().upper()
    page_size = min(max(size, 20), 50)
    collected: list[SocialPost] = []
    cur: int | str = cursor
    next_cur: str | int | None = None
    exhausted = False

    for _ in range(max(1, max_scan)):
        data = _response_object(c.get_json(
            SEARCH_URL,
            headers=_headers(site_info),
            params={"coinName": tag, "size": page_size, "cursor": cur},
        ))
        payload = (data or {}).get("data") or {}
        rows = (
            payload.get("contentDataList")
            or payload.get("dataList")
            or payload.get("list")
            or payload.get("contents")
            or (payload if isinstance(payload, list) else [])
        )
        if not rows:
            exhausted = True
            next_cur = None
            break
        for item in rows:
            if isinstance(item, dict):
                p = _post_from(item, tag)
                if p:
                    collected.append(p)
        nxt = payload.get("nextCursor") if isinstance(payload, dict) else None
        if nxt in (None, "", cur):
            exhausted = True
            next_cur = None
            break
        next_cur = nxt
        cur = nxt
        if len(prefer_lang_then_fill(dedupe_posts(collected), lang, min_keep=size)) >= size:
            break

    posts = sort_posts(prefer_lang_then_fill(dedupe_posts(collected), lang), "latest")[:size]
    has_more = (not exhausted) and next_cur is not None and bool(posts)
    return posts, next_cur, has_more


def topic_detail(
    topic_id: str,
    *,
    mode: SortMode = "hot",
    group: str = "USDT",
    size: int = 20,
    client: HybridClient | None = None,
    site_info: str | None = None,
) -> list[SocialPost]:
    """type=0 latest, type=1 hot — server-side."""
    c = client or default_client()
    typ = 1 if mode == "hot" else 0
    data = _response_object(c.get_json(
        TOPIC_DETAIL_URL,
        headers=_headers(site_info),
        params={"id": topic_id, "group": group, "size": size, "type": typ},
    ))
    payload = (data or {}).get("data") or {}
    rows = (
        payload.get("contentDataList")
        or payload.get("dataList")
        or payload.get("list")
        or []
    )
    posts: list[SocialPost] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        p = _post_from(item, group)
        if p:
            posts.append(p)
    return posts


def list_topics(
    *,
    size: int = 30,
    client: HybridClient | None = None,
    site_info: str | None = None,
) -> list[dict[str, Any]]:
    c = client or default_client()
    data = _response_object(c.get_json(
        TOPIC_LIST_URL,
        headers=_headers(site_info),
        params={"size": size},
    ))
    payload = (data or {}).get("data") or {}
    rows = payload.get("dataList") or payload.get("list") or payload.get("contentDataList") or []
    return [r for r in rows if isinstance(r, dict)]
