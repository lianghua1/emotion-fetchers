"""Kaito Mindshare（hub.kaito.ai 匿名 GET）——情绪模块内自包含，不依赖基本面目录。

查币贴文时附带：心智份额 / 波动 / 排名（默认 ALL · 7D）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from .http_client import HybridClient, default_client

HUB = "https://hub.kaito.ai/api/v1"
SITE = "https://www.kaito.ai"

# ticker → 常见 company_id / name 别名
_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("BTC", "BITCOIN"),
    "ETH": ("ETH", "ETHEREUM"),
    "SOL": ("SOL", "SOLANA"),
    "DOGE": ("DOGE", "DOGECOIN"),
    "XRP": ("XRP", "RIPPLE"),
    "ADA": ("ADA", "CARDANO"),
    "AVAX": ("AVAX", "AVALANCHE"),
    "LINK": ("LINK", "CHAINLINK"),
    "SUI": ("SUI",),
    "PEPE": ("PEPE",),
    "BNB": ("BNB", "BINANCE"),
}


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": SITE,
        "Referer": f"{SITE}/",
    }


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    return []


def _match_row(coin: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    tag = coin.strip().lstrip("#$").upper()
    aliases = {tag, *(_ALIASES.get(tag) or ())}
    aliases_u = {a.upper() for a in aliases}

    for row in rows:
        cid = str(row.get("company_id") or row.get("id") or "").upper()
        name = str(row.get("name") or "").upper().replace(" ", "")
        if cid in aliases_u or name in aliases_u:
            return row
    # 宽松：name 含 ticker
    for row in rows:
        cid = str(row.get("company_id") or "").upper()
        name = str(row.get("name") or "").upper()
        if tag == cid or tag in name.split() or name.startswith(tag):
            return row
    return None


def _pct(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    # hub 返回 0~1 小数
    return round(v * 100, 4) if abs(v) <= 1.5 else round(v, 4)


def lookup_mindshare(
    coin: str,
    *,
    duration: str = "7d",
    sector: str = "ALL",
    limit: int = 200,
    client: HybridClient | None = None,
) -> dict[str, Any]:
    """按币种在 sector leaderboard 中定位一行。"""
    tag = coin.strip().lstrip("#$").upper() or "BTC"
    duration = (duration or "7d").strip().lower()
    if duration not in ("24h", "7d", "30d", "3m", "6m", "12m"):
        duration = "7d"
    sector = (sector or "ALL").strip().upper() or "ALL"
    limit = max(1, min(int(limit), 100))

    http = client or default_client()
    path = f"/voices/crypto/company_sector_leaderboard"
    qs = urlencode({"sector": sector, "duration": duration, "limit": limit})
    url = f"{HUB}{path}?{qs}"
    try:
        payload = http.get_json(url, headers=_headers())
    except Exception as e:  # noqa: BLE001
        return {
            "coin": tag,
            "found": False,
            "error": str(e),
            "duration": duration,
            "sector": sector,
        }

    rows = _rows(payload)
    if not rows and isinstance(payload, dict) and payload.get("message"):
        return {
            "coin": tag,
            "found": False,
            "error": str(payload.get("message")),
            "duration": duration,
            "sector": sector,
            "code": payload.get("code"),
        }

    row = _match_row(tag, rows)
    if not row:
        return {
            "coin": tag,
            "found": False,
            "error": None,
            "duration": duration,
            "sector": sector,
            "scanned": len(rows),
            "hint": "未进入当前榜单前段，可换 30d 或加大 limit",
        }

    ms = _pct(row.get("mindshare"))
    delta = _pct(row.get("mindshare_delta"))
    try:
        rank = int(row.get("rank") or 0) or None
    except (TypeError, ValueError):
        rank = None

    return {
        "coin": tag,
        "found": True,
        "duration": duration,
        "sector": sector,
        "rank": rank,
        "company_id": row.get("company_id") or row.get("id"),
        "name": row.get("name"),
        "mindshare": row.get("mindshare"),
        "mindshare_pct": ms,
        "mindshare_delta": row.get("mindshare_delta"),
        "mindshare_delta_pct": delta,
        "logo": row.get("logo"),
        "source": "kaito-hub",
        "scanned": len(rows),
    }
