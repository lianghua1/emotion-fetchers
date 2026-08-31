"""Pull recent followers via twitter-scraper cookie, score with follower-standard.

Requires an external twitter-scraper checkout and its cookie (same as fetch.twitter).

Examples:
    python -m fetch follower-audit <handle> --sample 200 --pretty
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .twitter import _client, scraper_root

MODULE_ROOT = Path(__file__).resolve().parent.parent
CAPTURES = MODULE_ROOT / "captures"
SCORE_JS = MODULE_ROOT / "scripts" / "score_followers.mjs"


def _twitter_date_to_iso(created_at: str) -> str:
    if not created_at:
        return ""
    try:
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        return created_at


def _normalize_v1_user(u: dict[str, Any]) -> dict[str, Any]:
    pic = u.get("profile_image_url_https") or u.get("profile_image_url") or ""
    # follower-standard 用 URL 是否含 default_profile 判默认头像
    if u.get("default_profile_image"):
        pic = "https://abs.twimg.com/sticky/default_profile_images/default_profile_normal.png"
    return {
        "id": str(u.get("id_str") or u.get("id") or ""),
        "screen_name": u.get("screen_name") or "",
        "name": u.get("name") or "",
        "description": u.get("description") or "",
        "profile_image_url_https": pic,
        "created_at": _twitter_date_to_iso(str(u.get("created_at") or "")),
        "followers_count": int(u.get("followers_count") or 0),
        "friends_count": int(u.get("friends_count") or 0),
        "statuses_count": int(u.get("statuses_count") or 0),
        "default_profile_image": bool(u.get("default_profile_image")),
        "verified": bool(u.get("verified") or u.get("ext_is_blue_verified")),
    }


def fetch_followers(
    handle: str,
    *,
    sample: int = 200,
    delay: float = 0.8,
) -> dict[str, Any]:
    handle = handle.lstrip("@").strip()
    client = _client()
    user = client.get_user_by_screen_name(handle)
    legacy = user.get("legacy") or {}
    profile = {
        "id": str(user.get("rest_id") or ""),
        "screen_name": legacy.get("screen_name") or handle,
        "name": legacy.get("name") or "",
        "followers_count": int(legacy.get("followers_count") or 0),
        "friends_count": int(legacy.get("friends_count") or 0),
        "statuses_count": int(legacy.get("statuses_count") or 0),
        "created_at": _twitter_date_to_iso(str(legacy.get("created_at") or "")),
        "description": legacy.get("description") or "",
    }

    users: list[dict[str, Any]] = []
    cursor: Any = -1
    url = "https://x.com/i/api/1.1/followers/list.json"
    while len(users) < sample and cursor != 0:
        count = min(200, sample - len(users))
        params = {
            "screen_name": handle,
            "count": count,
            "skip_status": "true",
            "include_user_entities": "false",
            "cursor": cursor,
        }
        resp = client.session.get(url, params=params, headers=client._auth_headers(), timeout=30)
        if resp.status_code == 429:
            raise RuntimeError("Twitter rate limited (429) on followers/list")
        if resp.status_code != 200:
            raise RuntimeError(f"followers/list HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        batch = data.get("users") or []
        if not batch:
            break
        users.extend(_normalize_v1_user(u) for u in batch if isinstance(u, dict))
        next_cursor = data.get("next_cursor")
        if next_cursor in (None, 0, "0"):
            break
        cursor = next_cursor
        time.sleep(delay)

    users = users[:sample]
    return {
        "handle": handle,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sample": len(users),
        "profile": profile,
        "followers": users,
        "source": "twitter-scraper:followers/list.json",
        "scraper_root": str(scraper_root()),
    }


def score_with_follower_standard(payload: dict[str, Any]) -> dict[str, Any]:
    CAPTURES.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    handle = payload.get("handle") or "unknown"
    raw_path = CAPTURES / f"followers_{handle}_{stamp}.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = CAPTURES / f"followers_{handle}_latest.json"
    latest.write_text(raw_path.read_text(encoding="utf-8"), encoding="utf-8")

    if not SCORE_JS.is_file():
        raise FileNotFoundError(f"missing scorer: {SCORE_JS}")
    dist = MODULE_ROOT / "vendor" / "follower-standard" / "dist" / "index.js"
    if not dist.is_file():
        raise FileNotFoundError(
            f"follower-standard not built: {dist} (cd vendor/follower-standard && npm i && npm run build)"
        )

    proc = subprocess.run(
        ["node", str(SCORE_JS), str(raw_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(MODULE_ROOT),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"score_followers failed: {proc.stderr or proc.stdout}")
    scored = json.loads(proc.stdout)
    scored["raw_path"] = str(raw_path)
    out_path = CAPTURES / f"audit_{handle}_{stamp}.json"
    out_path.write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
    (CAPTURES / f"audit_{handle}_latest.json").write_text(
        out_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    scored["audit_path"] = str(out_path)
    return scored


def audit_handle(handle: str, *, sample: int = 200, delay: float = 0.8) -> dict[str, Any]:
    payload = fetch_followers(handle, sample=sample, delay=delay)
    return score_with_follower_standard(payload)
