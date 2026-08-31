"""本地演示服务：币安广场 + 欧易星球 + 推特（无 Playwright）。

用法（在 modules/情绪 下）:
  python demo_server.py
  浏览器打开 http://127.0.0.1:8765/

推特依赖外部 twitter-scraper（可用 TWITTER_SCRAPER_ROOT / TWITTER_COOKIE 覆盖）。

加载策略：
  - 默认时间序：增量翻页（币安 page / 欧易 cursor / 推特 cursor）
  - 浏览序：扩池后按浏览量排（后台预拉）
"""

from __future__ import annotations

import json
import os
import random
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fetch.binance import fetch_hashtag, fetch_latest_chunk
from fetch.http_client import HybridClient
from fetch.kaito import lookup_mindshare
from fetch.lang import normalize_lang_filter
from fetch.okx import search_by_coin, search_by_coin_chunk
from fetch.rootdata import lookup_hot_trend
from fetch.translate import translate_posts
from fetch import twitter as tw

ROOT = Path(__file__).resolve().parent
DEMO_HTML = ROOT / "demo.html"
HOST = os.environ.get("SENTIMENT_HOST") or os.environ.get("HOST") or "127.0.0.1"
PORT = int(os.environ.get("SENTIMENT_PORT") or os.environ.get("PORT") or "8765")

# 进程级共享客户端：避免每个 /api/feed 请求重建 HybridClient
# （重付本地代理探测 + 丢弃直连/代理路由缓存）
_DEFAULT_CLIENT: HybridClient | None = None


def get_default_client() -> HybridClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = HybridClient(timeout=22.0, max_retries=3)
    return _DEFAULT_CLIENT

COIN_POOL = [
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "DOGE",
    "ADA",
    "AVAX",
    "LINK",
    "PEPE",
    "SUI",
]


def _posts_payload(posts: list, *, translate: bool = True) -> list[dict]:
    if translate:
        return translate_posts(posts)
    return [p.to_dict() for p in posts]


def _top_meta(posts: list) -> dict | None:
    if not posts:
        return None
    top = posts[0]
    return {
        "like": top.like_count,
        "comment": top.comment_count,
        "view": top.view_count,
        "heat": top.heat_score,
        "lang": top.detected_language,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter
        print(f"[demo] {args[0]}")

    def _cors(self) -> None:
        origin = self.headers.get("Origin") or ""
        # 终端壳 :5180 iframe / fetch
        if origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: object) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/" or path == "/demo.html":
            html = DEMO_HTML.read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path.startswith("/vendor/"):
            rel = path[len("/vendor/") :]
            if ".." in rel or rel.startswith(("/", "\\")):
                self._json(400, {"error": "bad path"})
                return
            fpath = ROOT / "vendor" / rel
            if not fpath.is_file():
                self._json(404, {"error": "not found"})
                return
            data = fpath.read_bytes()
            ctype = "application/javascript"
            if fpath.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif fpath.suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            self._send(200, data, ctype)
            return

        if path == "/api/coins":
            self._json(200, {"coins": COIN_POOL, "random": random.choice(COIN_POOL)})
            return

        if path == "/api/mindshare":
            qs = parse_qs(parsed.query)
            coin = (qs.get("coin") or [random.choice(COIN_POOL)])[0].strip().upper() or "BTC"
            duration = (qs.get("duration") or ["7d"])[0].strip().lower()
            sector = (qs.get("sector") or ["ALL"])[0].strip().upper() or "ALL"
            try:
                out = lookup_mindshare(coin, duration=duration, sector=sector, limit=100)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                out = {"coin": coin, "found": False, "error": str(e), "duration": duration, "sector": sector}
            self._json(200, out)
            return

        if path == "/api/rootdata/hot":
            qs = parse_qs(parsed.query)
            coin = (qs.get("coin") or [random.choice(COIN_POOL)])[0].strip().upper() or "BTC"
            try:
                out = lookup_hot_trend(coin)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                out = {"coin": coin, "found": False, "error": str(e)}
            self._json(200, out)
            return

        if path == "/api/feed":
            qs = parse_qs(parsed.query)
            coin = (qs.get("coin") or [random.choice(COIN_POOL)])[0].strip().upper() or "BTC"
            mode = (qs.get("mode") or ["latest"])[0].strip().lower()
            if mode not in ("hot", "latest"):
                mode = "latest"
            limit = int((qs.get("limit") or ["30" if mode == "latest" else "100"])[0])
            limit = max(1, min(limit, 150 if mode == "hot" else 300))
            translate = (qs.get("translate") or ["0"])[0].strip() not in ("0", "false", "no")
            lang = normalize_lang_filter((qs.get("lang") or ["all"])[0])
            page = max(1, int((qs.get("page") or ["1"])[0]))
            okx_cursor = (qs.get("okx_cursor") or ["0"])[0]
            tw_cursor_raw = (qs.get("tw_cursor") or [""])[0]
            tw_cursor = tw_cursor_raw or None

            client = get_default_client()
            errors: dict[str, str] = {}
            binance_posts: list = []
            okx_posts: list = []
            twitter_posts: list = []
            meta: dict[str, object] = {"lang": lang, "mode": mode}
            next_page = page
            next_okx_cursor: object = okx_cursor
            next_tw_cursor: object = tw_cursor
            has_more_bn = False
            has_more_okx = False
            has_more_tw = False

            sources_raw = (qs.get("sources") or ["binance,okx,twitter"])[0]
            wanted = {
                s.strip().lower()
                for s in str(sources_raw).split(",")
                if s.strip()
            }
            if not wanted:
                wanted = {"binance", "okx", "twitter"}
            # 兼容别名
            if "bn" in wanted:
                wanted.add("binance")
            if "tw" in wanted or "x" in wanted:
                wanted.add("twitter")
            meta["sources"] = sorted(wanted)

            # 单源可给更长时间；多源并行仍用总预算
            single = len(wanted) == 1
            # 中文过滤需多翻页；单源给足预算避免凑不满就超时截断
            source_budget_s = 55.0 if single else (28.0 if mode == "hot" else 12.0)
            # 浏览序：单源最多 100；多源略降以免互相拖死
            if mode == "hot":
                limit = min(limit, 100 if single else 80)
            deadline = time.monotonic() + max(4.0, source_budget_s - 2.5)

            def _fetch_binance():
                if mode == "latest":
                    return fetch_latest_chunk(
                        coin,
                        page=page,
                        want=limit,
                        client=client,
                        lang=lang,
                    )
                posts = fetch_hashtag(
                    coin,
                    mode="hot",
                    page_size=50,
                    limit=limit,
                    client=client,
                    lang=lang,
                    deadline=deadline,
                )
                return posts, page, False

            def _fetch_okx():
                if mode == "latest":
                    return search_by_coin_chunk(
                        coin,
                        size=limit,
                        cursor=okx_cursor,
                        client=client,
                        lang=lang,
                    )
                posts = search_by_coin(
                    coin,
                    mode="hot",
                    size=limit,
                    client=client,
                    lang=lang,
                    deadline=deadline,
                )
                return posts, None, False

            def _fetch_twitter():
                if mode == "latest":
                    return tw.search_by_coin_chunk(
                        coin,
                        size=limit,
                        cursor=tw_cursor,
                        product="Latest",
                        lang=lang,
                    )
                posts = tw.search_by_coin(
                    coin,
                    mode="hot",
                    size=limit,
                    lang=lang,
                    deadline=deadline,
                )
                return posts, None, False

            fetchers = []
            if "binance" in wanted:
                fetchers.append((_fetch_binance, "binance"))
            if "okx" in wanted:
                fetchers.append((_fetch_okx, "okx"))
            if "twitter" in wanted:
                fetchers.append((_fetch_twitter, "twitter"))

            pool = ThreadPoolExecutor(max_workers=max(1, len(fetchers)))
            try:
                futures = {pool.submit(fn): name for fn, name in fetchers}
                try:
                    for fut in as_completed(futures, timeout=source_budget_s):
                        src = futures[fut]
                        try:
                            posts, nxt, more = fut.result(timeout=0.1)
                        except Exception as e:  # noqa: BLE001
                            errors[src] = str(e)
                            traceback.print_exc()
                            continue
                        if src == "binance":
                            binance_posts = posts
                            next_page = nxt
                            has_more_bn = bool(more)
                            meta["binance_top"] = _top_meta(binance_posts)
                        elif src == "okx":
                            okx_posts = posts
                            next_okx_cursor = nxt
                            has_more_okx = bool(more)
                            meta["okx_top"] = _top_meta(okx_posts)
                        else:
                            twitter_posts = posts
                            next_tw_cursor = nxt
                            has_more_tw = bool(more)
                            meta["twitter_top"] = _top_meta(twitter_posts)
                except TimeoutError:
                    for fut, src in futures.items():
                        if fut.done():
                            # 已完成但未取结果时补取（避免空帖+超时）
                            if src not in errors and (
                                (src == "binance" and not binance_posts)
                                or (src == "okx" and not okx_posts)
                                or (src == "twitter" and not twitter_posts)
                            ):
                                try:
                                    posts, nxt, more = fut.result(timeout=0)
                                    if src == "binance":
                                        binance_posts = posts
                                        next_page = nxt
                                        has_more_bn = bool(more)
                                    elif src == "okx":
                                        okx_posts = posts
                                        next_okx_cursor = nxt
                                        has_more_okx = bool(more)
                                    else:
                                        twitter_posts = posts
                                        next_tw_cursor = nxt
                                        has_more_tw = bool(more)
                                except Exception as e:  # noqa: BLE001
                                    errors.setdefault(src, str(e))
                            continue
                        errors.setdefault(src, f"timeout>{source_budget_s:.0f}s")
                        fut.cancel()
                    meta["partial"] = True
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

            try:
                do_tr = translate and lang != "zh"
                bn_out = _posts_payload(binance_posts, translate=do_tr)
                ok_out = _posts_payload(okx_posts, translate=do_tr)
                tw_out = _posts_payload(twitter_posts, translate=do_tr)
                meta["translated"] = do_tr
                meta["translated_count"] = sum(
                    1 for p in bn_out + ok_out + tw_out if p.get("translated")
                )
            except Exception as e:  # noqa: BLE001
                errors["translate"] = str(e)
                traceback.print_exc()
                bn_out = [p.to_dict() for p in binance_posts]
                ok_out = [p.to_dict() for p in okx_posts]
                tw_out = [p.to_dict() for p in twitter_posts]

            meta.update(
                {
                    "page": page,
                    "next_page": next_page,
                    "okx_cursor": okx_cursor,
                    "next_okx_cursor": next_okx_cursor,
                    "tw_cursor": tw_cursor_raw,
                    "next_tw_cursor": next_tw_cursor or "",
                    "has_more": bool(has_more_bn or has_more_okx or has_more_tw),
                    "has_more_binance": has_more_bn,
                    "has_more_okx": has_more_okx,
                    "has_more_twitter": has_more_tw,
                }
            )

            self._json(
                200,
                {
                    "coin": coin,
                    "mode": mode,
                    "lang": lang,
                    "binance": bn_out,
                    "okx": ok_out,
                    "twitter": tw_out,
                    "meta": meta,
                    "errors": errors,
                },
            )
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    if not DEMO_HTML.exists():
        raise SystemExit(f"missing {DEMO_HTML}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"情绪讨论演示 → http://{HOST}:{PORT}/")
    print("Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
