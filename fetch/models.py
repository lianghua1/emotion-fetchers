"""Shared post model for Binance Square + OKX Orbit + Twitter."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


SortMode = Literal["latest", "hot"]
Source = Literal["binance", "okx", "twitter"]


@dataclass(slots=True)
class SocialPost:
    source: Source
    id: str
    text: str
    author: str = ""
    like_count: int = 0
    comment_count: int = 0
    view_count: int = 0
    share_count: int = 0
    publish_time_ms: int = 0
    images: list[str] = field(default_factory=list)
    url: str = ""
    coin: str = ""
    detected_language: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d

    @property
    def heat_score(self) -> int:
        """浏览打底，赞/评加权；评在远超赞时降权。"""
        comments = self.comment_count
        if comments > max(30, self.like_count * 8):
            comments = min(comments, max(self.like_count * 3, 10))
        return self.view_count + self.like_count * 400 + comments * 60 + self.share_count * 80

    @property
    def looks_spammy(self) -> bool:
        t = (self.text or "").lower()
        needles = (
            "click here",
            "web3.binance.com/clip",
            "👈🌹",
            "airdrop secret",
            "guaranteed profit",
        )
        return any(n in t for n in needles)


def sort_posts(posts: list[SocialPost], mode: SortMode) -> list[SocialPost]:
    if mode == "hot":
        # 热度模式按浏览量倒序（赞/评仅作并列打平）
        clean = [p for p in posts if not p.looks_spammy]
        pool = clean or posts
        return sorted(
            pool,
            key=lambda p: (p.view_count, p.like_count, p.comment_count),
            reverse=True,
        )
    return sorted(posts, key=lambda p: p.publish_time_ms, reverse=True)


def dedupe_posts(posts: list[SocialPost]) -> list[SocialPost]:
    seen: set[str] = set()
    out: list[SocialPost] = []
    for p in posts:
        key = f"{p.source}:{p.id}"
        if not p.id or key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out
