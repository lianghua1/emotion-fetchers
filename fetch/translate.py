"""Auto-translate post text to Simplified Chinese.

Uses deep-translator (Google) by default; skip when already mostly Chinese.
Set TRANSLATE=0 to disable. Optional TRANSLATE_WORKERS (default 6).
"""

from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CACHE: dict[str, str] = {}
_MAX_CHUNK = 4500


def _enabled() -> bool:
    return os.getenv("TRANSLATE", "1").strip() not in ("0", "false", "False", "no")


def is_mostly_chinese(text: str, threshold: float = 0.28) -> bool:
    s = (text or "").strip()
    if not s:
        return True
    letters = re.findall(r"[A-Za-z\u4e00-\u9fff]", s)
    if not letters:
        return True
    cjk = sum(1 for ch in letters if _CJK_RE.match(ch))
    return (cjk / len(letters)) >= threshold


def _cache_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def _translate_chunk(text: str) -> str:
    from deep_translator import GoogleTranslator

    return GoogleTranslator(source="auto", target="zh-CN").translate(text)


def translate_to_zh(text: str) -> str:
    raw = (text or "").strip()
    if not raw or not _enabled() or is_mostly_chinese(raw):
        return raw

    key = _cache_key(raw)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit

    try:
        if len(raw) <= _MAX_CHUNK:
            out = _translate_chunk(raw)
        else:
            parts: list[str] = []
            buf = raw
            while buf:
                piece, buf = buf[:_MAX_CHUNK], buf[_MAX_CHUNK:]
                # try break on newline
                if buf and "\n" in piece:
                    i = piece.rfind("\n")
                    if i > _MAX_CHUNK // 2:
                        piece, buf = piece[: i + 1], piece[i + 1 :] + buf
                parts.append(_translate_chunk(piece))
            out = "".join(parts)
        out = (out or raw).strip() or raw
    except Exception:  # noqa: BLE001 — keep original on failure
        out = raw

    _CACHE[key] = out
    if len(_CACHE) > 4000:
        # drop arbitrary half
        for k in list(_CACHE.keys())[:2000]:
            _CACHE.pop(k, None)
    return out


def translate_posts(posts: Iterable[object], *, workers: int | None = None) -> list[dict]:
    """
    Convert SocialPost (or dict-like) list into payload dicts with:
      text (中文展示), text_original, translated
    """
    items = list(posts)
    if not items:
        return []

    workers = workers or int(os.getenv("TRANSLATE_WORKERS", "6"))
    workers = max(1, min(workers, 12))

    # Normalize to dict first
    base: list[dict] = []
    for p in items:
        if hasattr(p, "to_dict"):
            d = p.to_dict()
        elif isinstance(p, dict):
            d = dict(p)
        else:
            continue
        original = str(d.get("text") or "")
        d["text_original"] = original
        d["translated"] = False
        base.append(d)

    if not _enabled():
        return base

    need_idx = [
        i
        for i, d in enumerate(base)
        if d["text_original"] and not is_mostly_chinese(d["text_original"])
    ]
    if not need_idx:
        return base

    def _job(i: int) -> tuple[int, str]:
        return i, translate_to_zh(base[i]["text_original"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_job, i) for i in need_idx]
        for fut in as_completed(futs):
            i, zh = fut.result()
            orig = base[i]["text_original"]
            if zh and zh != orig:
                base[i]["text"] = zh
                base[i]["translated"] = True
            else:
                base[i]["text"] = orig
    return base
