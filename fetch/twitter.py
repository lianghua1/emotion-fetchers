"""Twitter/X coin discussion via an external twitter-scraper SearchTimeline.

Env:
  TWITTER_SCRAPER_ROOT  path to the external twitter-scraper checkout
  TWITTER_COOKIE        optional override cookie string
  TWITTER_ACCOUNTS_FILE optional extra accounts JSON (default fetch/.twitter_accounts.json)

账号 JSON 字段：username / auth_token / cookie（可选， token 登录后回写）。
仅 auth_token 时会先打开一次 x.com 配齐 ct0，再进搜索池。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .lang import LangFilter, filter_posts_by_lang
from .models import SocialPost, SortMode, dedupe_posts, sort_posts

DEFAULT_SCRAPER_ROOT = Path("twitter-scraper")
ACCOUNTS_FILE = Path(__file__).resolve().parent / ".twitter_accounts.json"
_CURSOR_SEP = "\x1f"
_COOKIE_KEYS = (
    "auth_token",
    "ct0",
    "twid",
    "guest_id",
    "guest_id_ads",
    "guest_id_marketing",
    "personalization_id",
    "lang",
)
_COOLDOWN_SEC = 15 * 60  # 风控后先冷 15 分钟，期间只用别的号
_BANNED_HANDLES = {
    x.strip().lstrip("@").lower()
    for x in os.getenv("TWITTER_BAN_HANDLES", "").split(",")
    if x.strip()
}
_BANNED_TWIDS = {
    x.strip()
    for x in os.getenv("TWITTER_BAN_TWIDS", "").split(",")
    if x.strip()
}


def scraper_root() -> Path:
    raw = os.getenv("TWITTER_SCRAPER_ROOT", "").strip()
    return Path(raw) if raw else DEFAULT_SCRAPER_ROOT


def _ensure_scraper_on_path() -> Path:
    root = scraper_root()
    if not root.is_dir():
        raise FileNotFoundError(f"twitter-scraper not found: {root}")
    p = str(root.resolve())
    if p not in sys.path:
        sys.path.insert(0, p)
    return root


def _accounts_path() -> Path:
    raw = os.getenv("TWITTER_ACCOUNTS_FILE", "").strip()
    return Path(raw) if raw else ACCOUNTS_FILE


def _twid_of(cookie: str) -> str:
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip() != "twid":
            continue
        raw = value.strip().strip('"')
        return raw.replace("u%3D", "").replace("u=", "")
    return ""


def _is_banned(name: str = "", cookie: str = "") -> bool:
    handle = (name or "").strip().lstrip("@").lower()
    if handle in _BANNED_HANDLES:
        return True
    twid = _twid_of(cookie)
    return bool(twid and twid in _BANNED_TWIDS)


def _auth_token_of(cookie: str) -> str:
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip() == "auth_token":
            return value.strip()
    return cookie.strip()


def _cookie_string(session) -> str:
    picked: dict[str, str] = {}
    x_ct0: str | None = None
    for ck in session.cookies:
        if ck.name not in _COOKIE_KEYS or not ck.value:
            continue
        if ck.name == "ct0" and "x.com" in (ck.domain or ""):
            x_ct0 = ck.value
            continue
        picked.setdefault(ck.name, ck.value)
    if x_ct0:
        picked["ct0"] = x_ct0
    return "; ".join(f"{k}={picked[k]}" for k in _COOKIE_KEYS if k in picked)


def _sync_csrf(client) -> None:
    x_ct0 = None
    any_ct0 = None
    for ck in client.session.cookies:
        if ck.name != "ct0" or not ck.value:
            continue
        any_ct0 = ck.value
        if "x.com" in (ck.domain or ""):
            x_ct0 = ck.value
    client.csrf_token = x_ct0 or any_ct0


def _hydrate_token(client, auth_token: str) -> str:
    """Token-only login: homepage issues matching ct0/twid (Chrome 插件同思路，无浏览器)."""
    token = auth_token.strip()
    client.session.cookies.clear()
    client.session.cookies.set("auth_token", token, domain=".x.com")
    client.session.get("https://x.com/", timeout=20, allow_redirects=True)
    _sync_csrf(client)
    cookie = _cookie_string(client.session)
    if not cookie or "auth_token=" not in cookie:
        raise RuntimeError("Twitter token hydrate produced empty cookie")
    with contextlib.redirect_stdout(io.StringIO()):
        ok = client.load_cookies_string(cookie)
    if not ok:
        raise RuntimeError("Twitter token hydrate failed csrf/guide check")
    return cookie


def _quiet_login(client, cookie: str) -> bool:
    with contextlib.redirect_stdout(io.StringIO()):
        return bool(client.load_cookies_string(cookie))


@dataclass
class _Slot:
    name: str
    cookie: str = ""
    auth_token: str = ""
    client: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    cooldown_until: float = 0.0
    dead: bool = False
    persist: bool = False


_SLOTS: list[_Slot] = []
_SLOT_I = 0
_POOL_LOCK = threading.Lock()
_POOL_READY = False


def _load_legacy_cookies(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    env = os.getenv("TWITTER_COOKIE", "").strip()
    if env:
        found.append(("env", env))
    cfg = root / "config.json"
    if cfg.is_file():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        cookie = (data.get("cookie") or "").strip()
        if cookie:
            found.append(("config.json", cookie))
    extra = root / "新号.json"
    if extra.is_file():
        data = json.loads(extra.read_text(encoding="utf-8"))
        cookie = (data.get("cookie") or "").strip()
        if cookie:
            found.append(("新号.json", cookie))
    cookie_file = root / "cookie.txt"
    if cookie_file.is_file():
        text = cookie_file.read_text(encoding="utf-8").strip()
        if text:
            found.append(("cookie.txt", text))
    return found


def _load_account_file() -> list[dict[str, Any]]:
    path = _accounts_path()
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [x for x in rows if isinstance(x, dict)]


def _persist_cookie(username: str, cookie: str) -> None:
    path = _accounts_path()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    rows = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return
    changed = False
    for row in rows:
        if isinstance(row, dict) and str(row.get("username") or "") == username:
            if row.get("cookie") != cookie:
                row["cookie"] = cookie
                changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _dedupe_slots(slots: list[_Slot]) -> list[_Slot]:
    seen: set[str] = set()
    out: list[_Slot] = []
    for slot in slots:
        key = _auth_token_of(slot.cookie) or slot.auth_token or slot.name
        if key in seen:
            continue
        seen.add(key)
        out.append(slot)
    return out


def _init_pool() -> None:
    global _SLOTS, _SLOT_I, _POOL_READY
    with _POOL_LOCK:
        if _POOL_READY:
            return
        _ensure_scraper_on_path()
        slots: list[_Slot] = []
        for row in _load_account_file():
            name = str(row.get("username") or "").strip() or "account"
            cookie = str(row.get("cookie") or "").strip()
            token = str(row.get("auth_token") or "").strip()
            if not cookie and token:
                cookie = f"auth_token={token}"
            if not cookie and not token:
                continue
            if _is_banned(name, cookie):
                continue
            slots.append(_Slot(name=name, cookie=cookie, auth_token=token, persist=True))
        for name, cookie in _load_legacy_cookies(scraper_root()):
            if _is_banned(name, cookie):
                continue
            slots.append(_Slot(name=name, cookie=cookie, auth_token=_auth_token_of(cookie)))
        _SLOTS = _dedupe_slots(slots)
        _SLOT_I = 0
        _POOL_READY = True
        if not _SLOTS:
            raise RuntimeError("Twitter cookie missing (.twitter_accounts.json / config.json / TWITTER_COOKIE)")


def _scraper_errors():
    _ensure_scraper_on_path()
    from scraper import APIError, AuthError, RateLimitError  # type: ignore

    return APIError, AuthError, RateLimitError


def _login_slot(slot: _Slot) -> Any:
    from scraper import TwitterClient  # type: ignore

    client = TwitterClient()
    cookie = slot.cookie
    token = slot.auth_token or _auth_token_of(cookie)
    ok = False
    if cookie and "ct0=" in cookie:
        ok = _quiet_login(client, cookie)
    if not ok and token:
        cookie = _hydrate_token(client, token)
        slot.cookie = cookie
        if slot.persist and slot.name:
            _persist_cookie(slot.name, cookie)
        ok = True
    elif not ok and cookie:
        ok = _quiet_login(client, cookie)
        if not ok and token:
            cookie = _hydrate_token(client, token)
            slot.cookie = cookie
            if slot.persist and slot.name:
                _persist_cookie(slot.name, cookie)
            ok = True
    if not ok:
        slot.dead = True
        raise RuntimeError(f"Twitter cookie auth failed ({slot.name})")
    slot.client = client
    slot.dead = False
    return client


def _slot_client(slot: _Slot) -> Any:
    with slot.lock:
        if slot.client is None:
            return _login_slot(slot)
        return slot.client


def _slot_ready(slot: _Slot, now: float) -> bool:
    return (not slot.dead) and slot.cooldown_until <= now and not _is_banned(slot.name, slot.cookie)


def _acquire_slot(*, prefer: str | None = None, exclude: frozenset[str] | set[str] | None = None) -> _Slot:
    """拿一个未风控的号。prefer 若正在冷却则忽略，立刻切别的号。"""
    _init_pool()
    global _SLOT_I
    skip = {*(exclude or ())}
    now = time.monotonic()
    with _POOL_LOCK:
        named = [
            s
            for s in _SLOTS
            if prefer and s.name == prefer and s.name not in skip and _slot_ready(s, now)
        ]
        ordered = named + _SLOTS[_SLOT_I:] + _SLOTS[:_SLOT_I]
        seen: set[int] = set()
        cold: list[_Slot] = []
        for slot in ordered:
            if id(slot) in seen:
                continue
            seen.add(id(slot))
            if slot.name in skip or slot.dead or _is_banned(slot.name, slot.cookie):
                continue
            if slot.cooldown_until > now:
                cold.append(slot)
                continue
            _SLOT_I = (_SLOTS.index(slot) + 1) % max(len(_SLOTS), 1)
            return slot
        waitable = [s for s in cold if s.name not in skip] or cold
        if waitable:
            slot = min(waitable, key=lambda s: s.cooldown_until)
            wait = max(0.0, slot.cooldown_until - now)
            _SLOT_I = (_SLOTS.index(slot) + 1) % max(len(_SLOTS), 1)
        else:
            wait = 0.0
            slot = None
    if slot is None:
        raise RuntimeError("Twitter account pool empty")
    if wait:
        time.sleep(min(wait, _COOLDOWN_SEC))
    return slot


def _mark_cooldown(slot: _Slot, seconds: float = _COOLDOWN_SEC) -> None:
    slot.cooldown_until = time.monotonic() + seconds


def _client():
    """兼容 follower-audit：取一个可用号。"""
    slot = _acquire_slot()
    return _slot_client(slot)


def _wrap_cursor(slot: _Slot | None, cursor: str | None) -> str | None:
    if slot is None:
        return f"*{_CURSOR_SEP}{cursor or ''}"
    if not cursor:
        return None
    return f"{slot.name}{_CURSOR_SEP}{cursor}"


def _resume_any_cursor() -> str:
    return f"*{_CURSOR_SEP}"


def _unwrap_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    if not cursor:
        return None, None
    if _CURSOR_SEP in cursor:
        name, real = cursor.split(_CURSOR_SEP, 1)
        if name == "*":
            return None, real or None
        return name or None, real or None
    return None, cursor


def coin_query(coin: str, lang: LangFilter = "all") -> str:
    tag = coin.strip().lstrip("#$").upper()
    # 短 ticker 加 crypto 降噪；语种用 Twitter 运算符（比本地滤更有效）
    if len(tag) <= 3:
        q = f"${tag} crypto"
    else:
        q = f"${tag}"
    if lang == "zh":
        q = f"{q} lang:zh"
    elif lang == "en":
        q = f"{q} lang:en"
    return q


def _parse_created_ms(created_at: str) -> int:
    if not created_at:
        return 0
    try:
        dt = parsedate_to_datetime(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0


def _images_from(tweet: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for m in tweet.get("media") or []:
        if not isinstance(m, dict):
            continue
        u = m.get("url") or m.get("media_url_https")
        if u:
            out.append(str(u))
    return out


def _post_from_tweet(tweet: dict[str, Any], coin: str) -> SocialPost | None:
    tid = str(tweet.get("id") or "")
    if not tid:
        return None
    screen = str(tweet.get("user_screen_name") or "")
    author = str(tweet.get("user_name") or screen or "")
    try:
        views = int(tweet.get("view_count") or 0)
    except (TypeError, ValueError):
        views = 0
    url = f"https://x.com/{screen}/status/{tid}" if screen else f"https://x.com/i/status/{tid}"
    return SocialPost(
        source="twitter",
        id=tid,
        text=str(tweet.get("text") or ""),
        author=author if not screen else f"{author} @{screen}".strip(),
        like_count=int(tweet.get("favorite_count") or 0),
        comment_count=int(tweet.get("reply_count") or 0),
        view_count=views,
        share_count=int(tweet.get("retweet_count") or 0) + int(tweet.get("quote_count") or 0),
        publish_time_ms=_parse_created_ms(str(tweet.get("created_at") or "")),
        images=_images_from(tweet),
        url=url,
        coin=coin.upper(),
        detected_language=str(tweet.get("lang") or ""),
        raw=tweet,
    )


def search_by_coin_chunk(
    coin: str,
    *,
    size: int = 30,
    cursor: str | None = None,
    product: str = "Latest",
    lang: LangFilter = "all",
    max_scan: int | None = None,
    delay: float = 0.6,
) -> tuple[list[SocialPost], str | None, bool]:
    """
    时间序增量（Latest）或 Top 一截。
    返回 (posts, next_cursor, has_more)。
    注意：语种过滤后本页可能为空，只要还有 cursor 仍应 has_more=True，便于继续翻。
    """
    q = coin_query(coin, lang=lang)
    collected: list[SocialPost] = []
    prefer, cur = _unwrap_cursor(cursor)
    next_cur: str | None = None
    exhausted = False
    scans = max_scan if max_scan is not None else (10 if lang != "all" else 4)
    APIError, AuthError, RateLimitError = _scraper_errors()
    last_err: Exception | None = None
    slot_used: _Slot | None = None
    failed: set[str] = set()
    _init_pool()
    tries = max(1, min(8, len(_SLOTS) or 1))
    switched = False

    for attempt in range(tries):
        try:
            slot = _acquire_slot(prefer=prefer if attempt == 0 and not switched else None, exclude=failed)
        except RuntimeError as e:
            last_err = e
            break
        slot_used = slot
        # 换号不能复用旧 cursor
        page_cur = cur if (not switched and prefer and slot.name == prefer) else None
        try:
            client = _slot_client(slot)
            with slot.lock:
                for i in range(max(1, scans)):
                    batch, nxt = client.search_tweets(
                        query=q, cursor=page_cur, count=20, product=product
                    )
                    if not batch:
                        exhausted = True
                        next_cur = None
                        break
                    for tw in batch:
                        p = _post_from_tweet(tw, coin)
                        if p:
                            collected.append(p)
                    if not nxt or nxt == page_cur:
                        exhausted = True
                        next_cur = None
                        break
                    next_cur = nxt
                    page_cur = nxt
                    if len(filter_posts_by_lang(dedupe_posts(collected), lang)) >= size:
                        break
                    if i + 1 < scans:
                        time.sleep(delay)
            last_err = None
            break
        except RuntimeError as e:
            last_err = e
            failed.add(slot.name)
            slot.dead = True
            slot.client = None
            print(f"[twitter] {slot.name} 登录失败，切号")
            prefer = None
            cur = None
            switched = True
            continue
        except (RateLimitError, AuthError, APIError) as e:
            last_err = e
            failed.add(slot.name)
            _mark_cooldown(slot)
            print(f"[twitter] {slot.name} 风控/失败（{type(e).__name__}），切号")
            prefer = None
            cur = None
            switched = True
            if collected:
                exhausted = False
                next_cur = None
                last_err = None
                break
            continue

    if last_err and not collected:
        raise last_err
    if switched and collected and not next_cur:
        next_cur = _resume_any_cursor()
        exhausted = False
    elif slot_used:
        next_cur = _wrap_cursor(slot_used, next_cur)
    if next_cur == _resume_any_cursor():
        has_more_override = True
    else:
        has_more_override = False

    posts = dedupe_posts(collected)
    # 查询已带 lang:zh/en 时仍做轻量本地滤；无 detected_language 时靠正文启发式
    posts = filter_posts_by_lang(posts, lang)
    if product == "Latest":
        posts = sort_posts(posts, "latest")
    else:
        posts = sort_posts(posts, "hot")
    posts = posts[:size]
    # 滤空时仍保留翻页能力（否则中文会卡死在 0 条）；风控切号后强制可续拉
    has_more = has_more_override or ((not exhausted) and bool(next_cur))
    return posts, next_cur, has_more


def search_by_coin(
    coin: str,
    *,
    mode: SortMode = "latest",
    size: int = 50,
    lang: LangFilter = "all",
    max_pages: int | None = None,
    delay: float = 0.5,
    deadline: float | None = None,
) -> list[SocialPost]:
    """
    - latest: SearchTimeline Latest
    - hot: 多页 Latest 扩池后按浏览量本地倒序（一次拉齐，不渐进）
    - deadline: monotonic 截止；超时前返回已收集部分
    - zh/en：Twitter lang: 运算符结果偏少时，回退无语种运算符 + 本地过滤补足
    """
    pages = max_pages if max_pages is not None else (8 if mode == "hot" else 3)
    if lang != "all":
        pages = min(pages + 4, 14)
    want = size
    collected: list[SocialPost] = []

    def _pull(query_lang: LangFilter, scans: int) -> None:
        nonlocal collected
        cursor: str | None = None
        for i in range(scans):
            if deadline is not None and time.monotonic() >= deadline:
                break
            if len(filter_posts_by_lang(dedupe_posts(collected), lang)) >= want:
                break
            batch, nxt, has_more = search_by_coin_chunk(
                coin,
                size=max(want, 40),
                cursor=cursor,
                product="Latest",
                lang=query_lang,
                max_scan=3 if query_lang == "all" else 2,
                delay=0,
            )
            collected.extend(batch)
            if not has_more or not nxt:
                break
            cursor = nxt
            if i + 1 < scans:
                time.sleep(delay)

    # 先按目标语种拉（Twitter 端过滤）；不够再全语种多扫 + 本地滤补足
    _pull(lang, pages)
    if lang != "all" and len(filter_posts_by_lang(dedupe_posts(collected), lang)) < want:
        _pull("all", max(10, pages))

    posts = filter_posts_by_lang(dedupe_posts(collected), lang)
    return sort_posts(posts, mode)[:want]
