"""Post language detect / filter (zh / en / all)."""

from __future__ import annotations

import re
from typing import Literal

from .models import SocialPost
from .translate import is_mostly_chinese

LangFilter = Literal["all", "zh", "en"]

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

_ZH_CODES = frozenset(
    {
        "zh",
        "zh-cn",
        "zh-tw",
        "zh-hk",
        "zh-hans",
        "zh-hant",
        "cn",
        "chinese",
    }
)
_EN_CODES = frozenset({"en", "en-us", "en-gb", "eng", "english"})


def normalize_lang_filter(raw: str | None) -> LangFilter:
    s = (raw or "all").strip().lower()
    if s in ("zh", "zh-cn", "cn", "chinese", "中文", "汉语", "汉"):
        return "zh"
    if s in ("en", "en-us", "eng", "english", "英文", "英语"):
        return "en"
    return "all"


def is_mostly_english(text: str, threshold: float = 0.72) -> bool:
    """Latin letters dominate and almost no CJK — treat as English-ish."""
    s = (text or "").strip()
    if not s or is_mostly_chinese(s):
        return False
    letters = _LATIN_RE.findall(s) + _CJK_RE.findall(s)
    if len(letters) < 8:
        return False
    latin = sum(1 for ch in letters if _LATIN_RE.match(ch))
    return (latin / len(letters)) >= threshold


def detect_post_lang(post: SocialPost) -> str:
    """
    Prefer API detectedLanguage (Binance); fallback to script heuristic.
    Returns short code: zh / en / other.
    """
    code = (post.detected_language or "").strip().lower().replace("_", "-")
    if code in _ZH_CODES or code.startswith("zh"):
        return "zh"
    if code in _EN_CODES or code.startswith("en"):
        return "en"
    if is_mostly_chinese(post.text):
        return "zh"
    if is_mostly_english(post.text):
        return "en"
    return "other"


def post_matches_lang(post: SocialPost, lang: LangFilter) -> bool:
    if lang == "all":
        return True
    return detect_post_lang(post) == lang


def filter_posts_by_lang(posts: list[SocialPost], lang: LangFilter) -> list[SocialPost]:
    if lang == "all":
        return posts
    return [p for p in posts if post_matches_lang(p, lang)]


def prefer_lang_then_fill(
    posts: list[SocialPost],
    lang: LangFilter,
    *,
    min_keep: int = 8,
) -> list[SocialPost]:
    """目标语种优先；凑不满时用其它语种垫上，避免整列空白。"""
    if lang == "all":
        return posts
    matched = filter_posts_by_lang(posts, lang)
    if len(matched) >= min_keep:
        return matched
    seen = {p.id for p in matched}
    extra = [p for p in posts if p.id not in seen]
    return [*matched, *extra]
