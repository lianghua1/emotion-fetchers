"""CLI smoke test — no Playwright.

Examples:
  python -m fetch binance BTC --mode hot
  python -m fetch okx BTC --mode latest
  python -m fetch both ETH --mode hot --limit 10
  python -m fetch follower-audit <handle> --sample 200 --pretty
  python -m fetch xhunt cz_binance zhusu --live --pretty
  python -m fetch xhunt --file handles.txt --live --workers 8 --rps 2

Env:
  RESIDENTIAL_PROXY_POOL   semicolon/newline separated proxies
  OKX_REGION / OKX_SITE_CODE / OKX_ENTITY   for x-site-info
  OKX_X_SITE_INFO           override raw header
  FETCH_IMPERSONATE         primp profile (default chrome_131)
  TWITTER_SCRAPER_ROOT / TWITTER_COOKIE / TWITTER_ACCOUNTS_FILE
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python -m fetch` when cwd is modules/情绪
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fetch.binance import fetch_hashtag  # noqa: E402
from fetch.http_client import default_client  # noqa: E402
from fetch.lang import normalize_lang_filter  # noqa: E402
from fetch.models import SortMode  # noqa: E402
from fetch.okx import search_by_coin  # noqa: E402
from fetch.okx_siteinfo import encode_site_info, site_info_from_env  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="情绪面板社交抓取（primp，无浏览器）")
    p.add_argument(
        "source",
        choices=("binance", "okx", "both", "siteinfo", "follower-audit", "xhunt"),
    )
    p.add_argument(
        "coin",
        nargs="?",
        default="BTC",
        help="币种；follower-audit 的 @handle；xhunt 时可省略（用后面 handles）",
    )
    p.add_argument("handles", nargs="*", help="xhunt: 一个或多个 Twitter username")
    p.add_argument("--file", type=str, default=None, help="xhunt: handles 文件（每行一个）")
    p.add_argument("--mode", choices=("latest", "hot"), default="hot")
    p.add_argument("--lang", choices=("all", "zh", "en"), default="all")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--sample", type=int, default=200, help="follower-audit 采样粉丝数")
    p.add_argument("--live", action="store_true", help="xhunt: primp 签名 live API（可上云）")
    p.add_argument("--cache-only", action="store_true", help="xhunt: 仅读本机插件缓存")
    p.add_argument("--workers", type=int, default=None, help="xhunt: 并发 batch 数")
    p.add_argument("--batch-size", type=int, default=None, help="xhunt: 每请求 username 数≤55")
    p.add_argument("--rps", type=float, default=None, help="xhunt: 全局请求速率")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args(argv)

    if args.source == "siteinfo":
        print(json.dumps({"x-site-info": site_info_from_env(), "sample_jp": encode_site_info()}, ensure_ascii=False))
        return 0

    if args.source == "xhunt":
        from fetch.xhunt import XHuntClient, get_ranks, read_session  # noqa: E402

        names = [*(args.handles or [])]
        if args.coin and args.coin.upper() != "BTC" and not str(args.coin).endswith(".txt"):
            names = [args.coin, *names]
        names = [n for n in names if n]
        file_path = getattr(args, "file", None)
        if file_path:
            from pathlib import Path as _P

            names.extend(
                ln.split(",")[0].strip()
                for ln in _P(file_path).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            )
        if not names:
            print(
                "usage: python -m fetch xhunt <handle> [handle2 ...] [--live]\n"
                "       python -m fetch xhunt --file handles.txt --live",
                file=sys.stderr,
            )
            return 2

        prefer_live = bool(args.live) and not getattr(args, "cache_only", False)
        if prefer_live:
            client = XHuntClient(
                workers=getattr(args, "workers", None),
                batch_size=getattr(args, "batch_size", None),
                rps=getattr(args, "rps", None),
                use_cache=True,
            )
            ranks = client.get_ranks(names, prefer_live=True)
            session = {"twitter_id": client.twitter_id, "username": client.user_id}
        else:
            session = read_session()
            ranks = get_ranks(names, prefer_live=False)

        out = {
            "session": session,
            "count": len(ranks),
            "ranks": {
                u: (
                    {
                        "kolRank": ranks[key].kol_rank,
                        "rank_followers": ranks[key].rank_followers,
                        "user_id": ranks[key].user_id,
                        "source": ranks[key].source,
                    }
                    if (key := u.lstrip("@").lower()) in ranks
                    else None
                )
                for u in names
            },
        }
        print(json.dumps(out, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0

    if args.source == "follower-audit":
        from fetch.follower_audit import audit_handle  # noqa: E402

        handle = (args.coin or "").lstrip("@").strip()
        if not handle or handle.upper() == "BTC":
            print("usage: python -m fetch follower-audit <handle> [--sample 200]", file=sys.stderr)
            return 2
        out = audit_handle(handle, sample=max(1, args.sample))
        print(json.dumps(out, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0

    mode: SortMode = args.mode  # type: ignore[assignment]
    lang = normalize_lang_filter(args.lang)
    client = default_client()
    out: dict = {"coin": args.coin.upper(), "mode": mode, "lang": lang, "results": {}}

    if args.source in ("binance", "both"):
        posts = fetch_hashtag(
            args.coin, mode=mode, page_size=args.limit, client=client, lang=lang
        )
        out["results"]["binance"] = [x.to_dict() for x in posts[: args.limit]]

    if args.source in ("okx", "both"):
        posts = search_by_coin(
            args.coin, mode=mode, size=args.limit, client=client, lang=lang
        )
        out["results"]["okx"] = [x.to_dict() for x in posts[: args.limit]]

    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
