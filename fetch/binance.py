"""Binance Square: hashtag feed + search (JSON API, no browser)."""

from __future__ import annotations

import time
from typing import Any

from .http_client import HybridClient, default_client
from .lang import LangFilter, prefer_lang_then_fill
from .models import SocialPost, SortMode, dedupe_posts, sort_posts


def _past_deadline(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline

HASHTAG_URL = "https://www.binance.com/bapi/composite/v4/friendly/pgc/content/queryByHashtag"
SEARCH_URL = "https://www.binance.com/bapi/composite/v2/friendly/pgc/feed/search/list"

DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "clienttype": "web",
    "lang": "zh-CN",
    "versioncode": "2.61.0",
    "origin": "https://www.binance.com",
    "referer": "https://www.binance.com/zh-CN/square",
}


def _headers_for_lang(lang: LangFilter) -> dict[str, str]:
    if lang == "en":
        return {
            **DEFAULT_HEADERS,
            "lang": "en",
            "referer": "https://www.binance.com/en/square",
        }
    # zh / all → 中文站头（接口本身仍混语种，靠本地 filter）
    return dict(DEFAULT_HEADERS)


def _images_from_item(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("images", "imageList", "imageMetaList", "imageUrlList"):
        val = item.get(key)
        if not val:
            continue
        if isinstance(val, list):
            for x in val:
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict):
                    u = x.get("url") or x.get("imageUrl") or x.get("ori")
                    if u:
                        out.append(str(u))
    return out


def _post_from_feed(item: dict[str, Any], coin: str) -> SocialPost | None:
    body = item
    nested = item.get("body")
    if isinstance(nested, dict) and (
        "likeCount" in nested or "contentId" in nested or "authorName" in nested
    ):
        body = nested

    pid = str(body.get("id") or body.get("contentId") or "")
    if not pid:
        return None

    text = body.get("content") or body.get("title") or body.get("body") or ""
    if isinstance(text, dict):
        text = text.get("text") or text.get("title") or ""
    text = str(text)

    author = str(body.get("authorName") or body.get("username") or "")
    author_obj = body.get("author") or body.get("userInfo")
    if not author and isinstance(author_obj, dict):
        author = str(
            author_obj.get("nickname")
            or author_obj.get("displayName")
            or author_obj.get("nickName")
            or ""
        )

    like = int(body.get("likeCount") or body.get("likeCnt") or 0)
    comment = int(body.get("commentCount") or body.get("commentCnt") or 0)
    view = int(body.get("viewCount") or body.get("viewCnt") or 0)
    share = int(body.get("shareCount") or body.get("shareCnt") or 0)
    ts = int(
        body.get("createTime")
        or body.get("publishTime")
        or body.get("date")
        or body.get("time")
        or 0
    )
    if ts and ts < 1_000_000_000_000:
        ts *= 1000

    web = body.get("webLink") or item.get("webLink")
    slug = body.get("squareId") or pid
    url = str(web) if web else f"https://www.binance.com/zh-CN/square/post/{slug}"
    detected = str(
        body.get("detectedLanguage")
        or item.get("detectedLanguage")
        or body.get("language")
        or ""
    )

    return SocialPost(
        source="binance",
        id=pid,
        text=text,
        author=author,
        like_count=like,
        comment_count=comment,
        view_count=view,
        share_count=share,
        publish_time_ms=ts,
        images=_images_from_item(body) or _images_from_item(item),
        url=url,
        coin=coin.upper(),
        detected_language=detected,
        raw=item if isinstance(item, dict) else {},
    )


def _fetch_hashtag_page(
    c: HybridClient,
    tag: str,
    *,
    order: str,
    page: int,
    page_size: int,
    headers: dict[str, str] | None = None,
) -> list[SocialPost]:
    data = c.get_json(
        HASHTAG_URL,
        headers=headers or DEFAULT_HEADERS,
        params={
            "hashtag": f"#{tag.lower()}",
            "pageIndex": page,
            "pageSize": page_size,
            "orderBy": order,
        },
    )
    payload = (data or {}).get("data") or {}
    feeds = payload.get("feedData") or payload.get("list") or []
    posts: list[SocialPost] = []
    for item in feeds:
        if not isinstance(item, dict):
            continue
        p = _post_from_feed(item, tag)
        if p:
            posts.append(p)
    return posts


def fetch_hashtag(
    coin: str,
    *,
    mode: SortMode = "hot",
    page: int = 1,
    page_size: int = 50,
    limit: int | None = None,
    max_pages: int | None = None,
    client: HybridClient | None = None,
    local_rerank: bool | None = None,
    lang: LangFilter = "all",
    deadline: float | None = None,
) -> list[SocialPost]:
    """
    币安广场 hashtag 讨论：

      GET /bapi/composite/v4/friendly/pgc/content/queryByHashtag
        ?hashtag=%23sui&pageIndex=1&pageSize=50&orderBy=HOT|LATEST

    - latest：跟服务端 LATEST 顺序
    - hot：扩池（HOT + 多页 LATEST）后按浏览量本地倒序
    - lang：all|zh|en —— 服务端混语种，按 detectedLanguage / 文案本地过滤
    - deadline：monotonic 截止；超时前返回已收集部分，避免整源判死
    """
    c = client or default_client()
    tag = coin.strip().lstrip("#").upper()
    want = limit if limit is not None else page_size
    rerank = (mode == "hot") if local_rerank is None else local_rerank
    headers = _headers_for_lang(lang)
    # 语种过滤会丢掉大量帖：中文/英语需多翻页凑满 want
    lang_boost = 5 if lang != "all" else 1

    collected: list[SocialPost] = []
    start = max(1, page)

    if mode == "hot" and rerank:
        hot_pages = max_pages if max_pages is not None else min(
            8 * lang_boost, max(2, (want + page_size - 1) // page_size) * lang_boost
        )
        latest_pages = max_pages if max_pages is not None else min(
            12 * lang_boost, max(4, (want + page_size - 1) // page_size + 2) * lang_boost
        )
        hot_pages = min(hot_pages, 20)
        latest_pages = min(latest_pages, 40)
        for i in range(start, start + hot_pages):
            if _past_deadline(deadline):
                break
            batch = _fetch_hashtag_page(
                c, tag, order="HOT", page=i, page_size=page_size, headers=headers
            )
            if not batch:
                break
            collected.extend(batch)
            if len(prefer_lang_then_fill(dedupe_posts(collected), lang, min_keep=want)) >= want:
                break
        for i in range(start, start + latest_pages):
            if _past_deadline(deadline):
                break
            if len(prefer_lang_then_fill(dedupe_posts(collected), lang, min_keep=want)) >= want:
                break
            batch = _fetch_hashtag_page(
                c, tag, order="LATEST", page=i, page_size=page_size, headers=headers
            )
            if not batch:
                break
            collected.extend(batch)
        if not collected:
            collected.extend(search_posts(tag, mode="hot", page_size=min(50, want), client=c, lang="all"))
        posts = sort_posts(prefer_lang_then_fill(dedupe_posts(collected), lang), "hot")
        return posts[:want]

    order = "HOT" if mode == "hot" else "LATEST"
    if max_pages is None:
        max_pages = min(
            4 * lang_boost,
            max(1, (want + page_size - 1) // page_size + 1) * lang_boost,
        )
        max_pages = min(max_pages, 10)
    for i in range(start, start + max_pages):
        if _past_deadline(deadline):
            break
        batch = _fetch_hashtag_page(
            c, tag, order=order, page=i, page_size=page_size, headers=headers
        )
        if not batch:
            break
        collected.extend(batch)
        if len(prefer_lang_then_fill(dedupe_posts(collected), lang)) >= want:
            break

    posts = prefer_lang_then_fill(dedupe_posts(collected), lang)
    if rerank:
        posts = sort_posts(posts, mode)
    return posts[:want]


def search_posts(
    coin: str,
    *,
    mode: SortMode = "latest",
    page: int = 1,
    page_size: int = 20,
    client: HybridClient | None = None,
    lang: LangFilter = "all",
) -> list[SocialPost]:
    """Square search; hot = local re-rank if API has no order switch."""
    c = client or default_client()
    q = coin.strip().upper()
    data = c.post_json(
        SEARCH_URL,
        headers=_headers_for_lang(lang),
        json_body={
            "scene": "web",
            "searchContent": q,
            "type": 1,
            "pageIndex": page,
            "pageSize": page_size,
        },
    )
    payload = (data or {}).get("data") or {}
    feeds = payload.get("list") or payload.get("feedData") or payload.get("data") or []
    posts: list[SocialPost] = []
    for item in feeds:
        if not isinstance(item, dict):
            continue
        card = item.get("body") if isinstance(item.get("body"), dict) else item
        if isinstance(item.get("body"), dict) and (
            "likeCount" in item["body"] or "contentId" in item["body"]
        ):
            card = item["body"]
        p = _post_from_feed(card if isinstance(card, dict) else item, q)
        if p:
            posts.append(p)
    return sort_posts(prefer_lang_then_fill(posts, lang), mode)


def fetch_latest_chunk(
    coin: str,
    *,
    page: int = 1,
    page_size: int = 50,
    want: int = 30,
    max_scan: int = 8,
    client: HybridClient | None = None,
    lang: LangFilter = "all",
) -> tuple[list[SocialPost], int, bool]:
    """
    时间序增量：从 page 起向后扫，凑够 want 条（语种过滤后）再返回。
    返回 (posts, next_page, has_more)。
    """
    c = client or default_client()
    tag = coin.strip().lstrip("#").upper()
    headers = _headers_for_lang(lang)
    start = max(1, page)
    collected: list[SocialPost] = []
    last = start - 1
    exhausted = False
    for i in range(start, start + max(1, max_scan)):
        batch = _fetch_hashtag_page(
            c, tag, order="LATEST", page=i, page_size=page_size, headers=headers
        )
        last = i
        if not batch:
            exhausted = True
            break
        collected.extend(batch)
        if len(prefer_lang_then_fill(dedupe_posts(collected), lang)) >= want:
            break

    posts = sort_posts(prefer_lang_then_fill(dedupe_posts(collected), lang), "latest")[:want]
    next_page = last + 1
    has_more = (not exhausted) and bool(posts)
    return posts, next_page, has_more
