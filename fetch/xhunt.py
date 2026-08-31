"""XHunt KOL rank — cloud-friendly live client (no Chrome/CDP).

Transport: primp Chrome TLS impersonation + HMAC-SHA512 request signing.
Guest twitter id works when ``x-user-id`` is non-empty.

Env:
  XHUNT_TWITTER_ID   signer twitter id (default 999999999 guest)
  XHUNT_USER_ID      non-empty x-user-id header (default xhunt-cloud)
  XHUNT_TOKEN        optional Token auth
  XHUNT_BATCH_SIZE   usernames per request (default 40, max 55)
  XHUNT_RPS          requests per second soft limit (default 1.5)
  XHUNT_WORKERS      concurrent batch workers (default 4)
  XHUNT_CACHE_DIR    optional JSONL/dir cache root
  XHUNT_CACHE_TTL    cache seconds (default 3600)
  FETCH_IMPERSONATE  primp profile (default chrome_146)
  RESIDENTIAL_PROXY_POOL / HTTPS_PROXY  optional proxies
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote

from .http_client import HybridClient, default_client

DEFAULT_EXT_ID = "gonmfafjcdkngkbhcpmcphlgfhabkeji"
DEFAULT_STORAGE = (
    Path.home()
    / "AppData/Local/Google/Chrome/User Data/Default/Local Extension Settings"
    / DEFAULT_EXT_ID
)

HMAC_KEY = b"gluf69fcec9274b73196e8c42d50b32fd69fd7ff0742d14dd2409957676da10g"
FINGERPRINT = "deadbeefdeadbeefdeadbeefdeadbeef"
GUEST_TWITTER_ID = "999999999"
API_BASE = "https://kb.xhunt.ai"
EXT_VERSION = "0.3.5"
MAX_BATCH = 55


@dataclass
class RankHit:
    username: str
    kol_rank: int
    source: str  # live | cache
    timestamp: int | None = None
    rank_followers: int | None = None
    user_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _RateLimiter:
    """Simple global RPS limiter (token bucket-ish)."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / max(rps, 0.05)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                delay = self._next - now
            else:
                delay = 0.0
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            time.sleep(delay)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    n = max(1, min(size, MAX_BATCH))
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _norm_handles(usernames: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in usernames:
        u = str(raw or "").strip().lstrip("@").lower()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _sign(method: str, path_with_query: str, ts: str, rid: str, twitter_id: str, body: str = "") -> str:
    body_hash = hashlib.sha512(body.encode()).hexdigest()
    canonical = "\n".join(
        [method.upper(), path_with_query, ts, rid, FINGERPRINT, body_hash, twitter_id]
    )
    return hmac.new(HMAC_KEY, canonical.encode(), hashlib.sha512).hexdigest()


def _build_rank_url(usernames: list[str], *, language: str = "en") -> tuple[str, str]:
    path = "/api/xhunt/proxy/public/fetch/twitter/rank"
    params = {
        "target": "k8s_kota",
        "usernames": ",".join(usernames),
        "x-language": language,
    }
    qs = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items()))
    pwq = f"{path}?{qs}"
    return f"{API_BASE}{pwq}", pwq


def _parse_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    # HybridClient may return parsed JSON directly
    body = payload.get("data", payload)
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        rows = body["data"]
    elif isinstance(body, list):
        rows = body
    elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("data"), list):
        rows = payload["data"]["data"]
    else:
        # wrapped by our live helpers
        inner = payload.get("json") or payload.get("data")
        if isinstance(inner, dict):
            return _parse_rows(inner)
        return []
    return [x for x in rows if isinstance(x, dict)]


def _rows_to_hits(rows: list[dict[str, Any]], *, source: str = "live") -> dict[str, RankHit]:
    out: dict[str, RankHit] = {}
    for row in rows:
        u = str(row.get("username") or "").strip().lstrip("@").lower()
        if not u or row.get("kolRank") is None:
            continue
        out[u] = RankHit(
            username=u,
            kol_rank=int(row["kolRank"]),
            source=source,
            rank_followers=int(row["rank_followers"]) if row.get("rank_followers") is not None else None,
            user_id=str(row["user_id"]) if row.get("user_id") is not None else None,
            timestamp=int(time.time() * 1000),
        )
    return out


class FileRankCache:
    """Per-user JSON cache files for multi-instance cloud workers."""

    def __init__(self, root: Path | None = None, ttl: int = 3600) -> None:
        self.root = Path(root or _env("XHUNT_CACHE_DIR") or (Path.cwd() / ".xhunt_cache"))
        self.ttl = int(ttl if ttl > 0 else _env_int("XHUNT_CACHE_TTL", 3600))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, username: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", username.lower())[:80]
        return self.root / f"{safe}.json"

    def get(self, username: str) -> RankHit | None:
        p = self._path(username)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            ts = int(data.get("timestamp") or 0)
            if self.ttl > 0 and ts and (time.time() * 1000 - ts) > self.ttl * 1000:
                return None
            return RankHit(
                username=str(data["username"]).lower(),
                kol_rank=int(data["kol_rank"]),
                source=str(data.get("source") or "cache"),
                timestamp=ts or None,
                rank_followers=data.get("rank_followers"),
                user_id=data.get("user_id"),
            )
        except Exception:
            return None

    def put_many(self, hits: dict[str, RankHit]) -> None:
        for hit in hits.values():
            p = self._path(hit.username)
            p.write_text(json.dumps(hit.to_dict(), ensure_ascii=False), encoding="utf-8")


class XHuntClient:
    """Stateless signed rank client suitable for cloud batch jobs."""

    def __init__(
        self,
        *,
        twitter_id: str | None = None,
        user_id: str | None = None,
        token: str | None = None,
        batch_size: int | None = None,
        rps: float | None = None,
        workers: int | None = None,
        language: str = "en",
        client: HybridClient | None = None,
        cache: FileRankCache | None = None,
        use_cache: bool = True,
    ) -> None:
        self.twitter_id = (twitter_id or _env("XHUNT_TWITTER_ID") or GUEST_TWITTER_ID).strip()
        # CRITICAL: empty x-user-id makes upstream return MISSING_SIGNATURE_HEADERS
        self.user_id = (user_id or _env("XHUNT_USER_ID") or "xhunt-cloud").strip() or "xhunt-cloud"
        self.token = (token if token is not None else _env("XHUNT_TOKEN")).strip()
        self.batch_size = max(1, min(batch_size or _env_int("XHUNT_BATCH_SIZE", 40), MAX_BATCH))
        self.workers = max(1, workers or _env_int("XHUNT_WORKERS", 4))
        self.language = language
        self.http = client or default_client()
        self.limiter = _RateLimiter(rps if rps is not None else _env_float("XHUNT_RPS", 1.5))
        self.cache = cache
        if cache is None and use_cache:
            self.cache = FileRankCache()
        self._force_cache = use_cache

    def fetch_batch(self, usernames: list[str]) -> dict[str, RankHit]:
        handles = _norm_handles(usernames)
        if not handles:
            return {}
        url, pwq = _build_rank_url(handles, language=self.language)
        ts = str(int(time.time() * 1000))
        rid = str(uuid.uuid4())
        sig = _sign("GET", pwq, ts, rid, self.twitter_id)
        headers = {
            "x-request-id": rid,
            "x-request-timestamp": ts,
            "x-device-fingerprint": FINGERPRINT,
            "x-request-signature": sig,
            "x-signature-version": "v2",
            "x-extension-version": EXT_VERSION,
            "x-user-id": self.user_id,
            "x-tw-id": self.twitter_id,
            "x-window-location-href": "https://x.com/home",
            "x-language": self.language,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://x.com",
            "Referer": "https://x.com/",
        }
        if self.token:
            headers["authorization"] = f"Token {self.token}"

        self.limiter.wait()
        # Avoid HybridClient swallowing 4xx into retries without body — call primp-like path carefully.
        # Use low-level request; on dict with error key, raise.
        payload = self.http.request("GET", url, headers=headers)
        if isinstance(payload, dict) and payload.get("error") and "data" not in payload and payload.get("code") != 200:
            err = payload.get("error")
            raise RuntimeError(f"xhunt rank error: {err} body={payload}")
        if isinstance(payload, dict) and payload.get("_status") and payload.get("_status") >= 400:
            raise RuntimeError(f"xhunt http {payload.get('_status')}: {payload.get('_raw', '')[:300]}")
        # success shape: {code:200, data:{data:[...]}}
        if isinstance(payload, dict) and payload.get("code") not in (None, 200) and "data" not in payload:
            raise RuntimeError(f"xhunt unexpected payload: {str(payload)[:300]}")
        hits = _rows_to_hits(_parse_rows(payload), source="live")
        if self.cache and hits:
            self.cache.put_many(hits)
        return hits

    def get_ranks(
        self,
        usernames: Iterable[str],
        *,
        prefer_live: bool = True,
        fill_cache: bool = True,
    ) -> dict[str, RankHit]:
        wanted = _norm_handles(usernames)
        out: dict[str, RankHit] = {}
        missing = list(wanted)

        if fill_cache and self.cache:
            still: list[str] = []
            for u in missing:
                hit = self.cache.get(u)
                if hit is not None:
                    hit.source = "cache"
                    out[u] = hit
                else:
                    still.append(u)
            missing = still

        if not prefer_live or not missing:
            return {u: out[u] for u in wanted if u in out}

        batches = list(_chunks(missing, self.batch_size))
        if len(batches) == 1 or self.workers == 1:
            for batch in batches:
                out.update(self.fetch_batch(batch))
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futs = {pool.submit(self.fetch_batch, b): b for b in batches}
                for fut in as_completed(futs):
                    out.update(fut.result())

        return {u: out[u] for u in wanted if u in out}


# ---------- optional local Chrome extension LevelDB cache (desktop only) ----------

def _storage_blobs(storage_dir: Path) -> bytes:
    if not storage_dir.is_dir():
        return b""
    chunks: list[bytes] = []
    for f in storage_dir.iterdir():
        if f.suffix.lower() in {".log", ".ldb"}:
            try:
                chunks.append(f.read_bytes())
            except OSError:
                pass
    return b"".join(chunks)


def read_rank_cache(storage_dir: Path | None = None) -> dict[str, RankHit]:
    storage_dir = storage_dir or DEFAULT_STORAGE
    data = _storage_blobs(storage_dir)
    pairs = re.findall(
        rb'\\"([A-Za-z0-9_]{1,40})\\":\{\\"kolRank\\":(-?\d+),\\"timestamp\\":(\d+)',
        data,
    )
    if not pairs:
        pairs = re.findall(
            rb'"([A-Za-z0-9_]{1,40})":\{"kolRank":(-?\d+),"timestamp":(\d+)',
            data,
        )
    out: dict[str, RankHit] = {}
    for user, rank, ts in pairs:
        u = user.decode("ascii").lower()
        hit = RankHit(username=u, kol_rank=int(rank), source="cache", timestamp=int(ts))
        prev = out.get(u)
        if prev is None or (hit.timestamp or 0) >= (prev.timestamp or 0):
            out[u] = hit
    return out


def read_session(storage_dir: Path | None = None) -> dict[str, Any]:
    """Best-effort local plugin session (desktop). Cloud jobs should use env instead."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return {}
    storage_dir = storage_dir or DEFAULT_STORAGE
    data = _storage_blobs(storage_dir)
    if not data:
        return {}
    key_mat = hashlib.sha256(b"xhunt-fixed-secret-v1").digest()
    v1 = re.compile(rb"v1:[A-Za-z0-9+/]+=*:[A-Za-z0-9+/]+=*")
    marker = b"@xhunt/initial-state-current-user"
    for m in re.finditer(re.escape(marker), data):
        window = data[m.end() : m.end() + 20000]
        vm = v1.search(window)
        if not vm:
            continue
        blob = vm.group(0).decode("ascii")
        for end in range(len(blob), 40, -1):
            cand = blob[:end]
            if cand.count(":") != 2:
                continue
            try:
                _, iv_b64, ct_b64 = cand.split(":")
                import base64

                pt = AESGCM(key_mat).decrypt(base64.b64decode(iv_b64), base64.b64decode(ct_b64), None)
                user = json.loads(pt.decode("utf-8"))
                if isinstance(user, dict) and user.get("id_str"):
                    return {
                        "user": user,
                        "twitter_id": str(user["id_str"]),
                        "username": user.get("screen_name"),
                    }
            except Exception:
                continue
    return {}


# ---------- public helpers (CLI / module API) ----------

def fetch_ranks_live(usernames: list[str], **kwargs: Any) -> dict[str, Any]:
    """Fetch live ranks; returns diagnostic wrapper for CLI."""
    client = XHuntClient(**{k: v for k, v in kwargs.items() if k in {
        "twitter_id", "user_id", "token", "batch_size", "rps", "workers", "language", "use_cache"
    }})
    handles = _norm_handles(usernames)
    hits = client.get_ranks(handles, prefer_live=True)
    return {
        "status": 200 if hits else None,
        "twitter_id": client.twitter_id,
        "user_id": client.user_id,
        "count": len(hits),
        "ranks": {k: v.to_dict() for k, v in hits.items()},
    }


def get_ranks(
    usernames: list[str],
    *,
    prefer_live: bool = False,
    storage_dir: Path | None = None,
) -> dict[str, RankHit]:
    """Resolve ranks. prefer_live uses primp signed API; else local plugin cache only."""
    wanted = _norm_handles(usernames)
    if prefer_live:
        client = XHuntClient(use_cache=True)
        return client.get_ranks(wanted, prefer_live=True)
    cache = read_rank_cache(storage_dir)
    return {u: cache[u] for u in wanted if u in cache}


def get_rank_cached(username: str, storage_dir: Path | None = None) -> RankHit | None:
    return read_rank_cache(storage_dir).get(username.lower().lstrip("@"))


def batch_from_file(path: Path, *, prefer_live: bool = True) -> dict[str, RankHit]:
    lines = path.read_text(encoding="utf-8").splitlines()
    names: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # allow csv first column
        names.append(line.split(",")[0].strip())
    client = XHuntClient(use_cache=True)
    return client.get_ranks(names, prefer_live=prefer_live)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="XHunt KOL rank (primp, cloud-ready)")
    p.add_argument("usernames", nargs="*", help="twitter handles")
    p.add_argument("--file", "-f", type=Path, help="handles file, one per line")
    p.add_argument("--live", action="store_true", default=True, help="use live API (default)")
    p.add_argument("--cache-only", action="store_true", help="only read local plugin cache")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--rps", type=float, default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    names = list(args.usernames or [])
    if args.file:
        names.extend(
            ln.split(",")[0].strip()
            for ln in args.file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
    if not names:
        p.error("provide usernames or --file")

    if args.cache_only:
        ranks = get_ranks(names, prefer_live=False)
    else:
        client = XHuntClient(workers=args.workers, batch_size=args.batch_size, rps=args.rps, use_cache=True)
        ranks = client.get_ranks(names, prefer_live=True)

    if args.json:
        print(json.dumps({k: v.to_dict() for k, v in ranks.items()}, ensure_ascii=False, indent=2))
    else:
        for u in _norm_handles(names):
            hit = ranks.get(u)
            if hit:
                print(f"@{hit.username}\t#{hit.kol_rank}\t[{hit.source}]\tfq={hit.rank_followers}")
            else:
                print(f"@{u}\t?\t[miss]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
