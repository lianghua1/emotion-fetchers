"""RootData RD 热度 / 增长指数 —— 情绪模块自包含。

数据源优先级：
  1) open/skill：ser_inv / get_item(eval) / hot_index(rank) / id_map(总数)
  2) /pc/lang/eval/eval_rank：榜内 rankChange、chartUrl（需可访问 www；本机常走代理）
  3) public SVG：近 7 日热度折线；并估算约 24h 变化
  4) /pc/project/get_metrics：官网「表现」增长指数等（常需过 WAF，失败则留空）
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any

from .http_client import HybridClient, default_client

WWW = "https://www.rootdata.com"
PUBLIC = "https://public.rootdata.com"
SKILL_BASE = "https://api.rootdata.com/open/skill"

BROWSER_LIKE = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": WWW,
    "Referer": f"{WWW}/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_DEFAULT_KEY = "shjkhd9477k@326h"
_SKILL_KEY_PATH = Path(__file__).resolve().parent / ".rootdata_skill_key"

_skill_key_mem: str | None = None
_total_projects_cache: tuple[float, int] | None = None  # (ts, n)


class RootDataError(RuntimeError):
    pass


def _aes_key() -> bytes:
    env = (os.getenv("ROOTDATA_PC_AES_KEY") or "").strip()
    if env:
        try:
            raw = base64.b64decode(env)
            if len(raw) in (16, 24, 32):
                return raw
        except Exception:
            pass
        return env.encode("utf-8")
    return _DEFAULT_KEY.encode("utf-8")


def decrypt_pc_payload(blob: str) -> Any:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except Exception as e:  # noqa: BLE001
        raise RootDataError(f"pycryptodome required: {e}") from e
    raw = base64.b64decode(blob)
    plain = unpad(AES.new(_aes_key(), AES.MODE_ECB).decrypt(raw), 16).decode("utf-8")
    try:
        return json.loads(plain)
    except json.JSONDecodeError:
        return plain


def _detect_proxy() -> str | None:
    for env in ("ROOTDATA_PROXY", "RESIDENTIAL_PROXY_POOL", "HTTPS_PROXY", "HTTP_PROXY"):
        raw = (os.getenv(env) or "").strip()
        if raw:
            return raw.split(",")[0].strip().split(";")[0].strip()
    # 本机 Clash 等常见端口：直连 www 常被地区墙，代理可过
    for port in (7897, 7890, 10809, 7891):
        s = socket.socket()
        s.settimeout(0.25)
        try:
            s.connect(("127.0.0.1", port))
            return f"http://127.0.0.1:{port}"
        except OSError:
            pass
        finally:
            s.close()
    return None


def _primp_client(proxy: str | None = None, *, timeout: float = 40.0):
    import primp

    kwargs: dict[str, Any] = {
        "impersonate": os.getenv("FETCH_IMPERSONATE", "chrome_146"),
        "timeout": timeout,
    }
    px = proxy if proxy is not None else _detect_proxy()
    if px:
        kwargs["proxy"] = px
    return primp.Client(**kwargs)


def pc_post(endpoint: str, body: dict | None = None, *, client: HybridClient | None = None) -> Any:
    """POST www /pc；优先带本地代理。client 参数保留兼容，实际多用 primp+proxy。"""
    del client  # HybridClient 无代理时易 403
    ep = endpoint.lstrip("/")
    if not ep.startswith("pc/"):
        ep = f"pc/{ep}"
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            http = _primp_client()
            resp = http.post(f"{WWW}/{ep}", headers=BROWSER_LIKE, json=body or {})
            status = getattr(resp, "status_code", None)
            text = getattr(resp, "text", "") or ""
            if status in (403, 429) or "Access Restricted" in text:
                raise RootDataError(f"www blocked status={status}")
            raw = resp.json() if hasattr(resp, "json") else json.loads(text)
            if not isinstance(raw, dict):
                raise RootDataError(f"non-json {ep}")
            data = raw.get("data")
            if data is True:
                return True
            if isinstance(data, str) and len(data) > 32:
                return decrypt_pc_payload(data)
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.35 * (attempt + 1))
    raise RootDataError(str(last_err) if last_err else f"pc_post failed {ep}")


def _load_skill_key() -> str | None:
    global _skill_key_mem
    if _skill_key_mem:
        return _skill_key_mem
    env = (os.getenv("ROOTDATA_SKILL_KEY") or "").strip()
    if env:
        _skill_key_mem = env
        return env
    if _SKILL_KEY_PATH.exists():
        key = _SKILL_KEY_PATH.read_text(encoding="utf-8").strip()
        if key:
            _skill_key_mem = key
            return key
    return None


def _save_skill_key(key: str) -> None:
    global _skill_key_mem
    _skill_key_mem = key
    try:
        _SKILL_KEY_PATH.write_text(key, encoding="utf-8")
    except OSError:
        pass


def ensure_skill_key() -> str:
    existing = _load_skill_key()
    if existing:
        return existing
    http = _primp_client()
    resp = http.post(f"{SKILL_BASE}/init", json={})
    try:
        j = resp.json()
    except Exception as e:  # noqa: BLE001
        raise RootDataError(f"skill/init non-json: {e}") from e
    key = (j or {}).get("api_key") if isinstance(j, dict) else None
    if not key:
        raise RootDataError(f"skill/init failed: {(getattr(resp, 'text', '') or '')[:200]}")
    _save_skill_key(str(key))
    return str(key)


def skill_post(path: str, body: dict | None = None) -> Any:
    key = ensure_skill_key()
    http = _primp_client()
    resp = http.post(
        f"{SKILL_BASE}{path}",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "language": "cn",
        },
        json=body or {},
    )
    status = getattr(resp, "status_code", None)
    try:
        j = resp.json()
    except Exception as e:  # noqa: BLE001
        raise RootDataError(f"skill {path} non-json status={status}: {e}") from e
    if status == 401:
        # key 失效则清缓存重试一次
        global _skill_key_mem
        _skill_key_mem = None
        try:
            _SKILL_KEY_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        key = ensure_skill_key()
        resp = http.post(
            f"{SKILL_BASE}{path}",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "language": "cn",
            },
            json=body or {},
        )
        j = resp.json()
    if not isinstance(j, dict):
        raise RootDataError(f"skill {path} bad payload")
    if j.get("result") not in (None, 200, 1) and j.get("error"):
        raise RootDataError(f"skill {path}: {j.get('error')}")
    return j.get("data")


def _parse_i18n_name(raw: Any) -> str:
    if not isinstance(raw, str):
        return str(raw or "")
    s = raw.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            return str(obj.get("cn_value") or obj.get("en_value") or obj.get("tw_value") or "")
        except json.JSONDecodeError:
            return s
    return s


def _parse_svg_points(svg: str) -> list[tuple[float, float]]:
    m = re.search(r'<path[^>]*\sd="([^"]+)"', svg, re.I)
    if not m:
        return []
    pts: list[tuple[float, float]] = []
    for cmd in re.finditer(r"[ML]\s*([-\d.]+)[,\s]+([-\d.]+)", m.group(1)):
        pts.append((float(cmd.group(1)), float(cmd.group(2))))
    return pts


def _series_from_svg(svg: str, *, eval_score: float, eval_time_ms: int) -> dict[str, list]:
    pts = _parse_svg_points(svg)
    if not pts:
        return {"ts": [], "values": []}
    ys = [y for _, y in pts]
    ymin, ymax = min(ys), max(ys)
    span = (ymax - ymin) or 1.0
    norm = [(ymax - y) / span for y in ys]
    end = float(eval_score) if eval_score else 1.0
    last = norm[-1]
    if last > 1e-9:
        values = [round(v / last * end, 4) for v in norm]
    else:
        peak = max(norm) or 1.0
        values = [round(v / peak * end, 4) for v in norm]
        values[-1] = round(end, 4)

    n = len(values)
    end_s = int(eval_time_ms / 1000) if eval_time_ms else int(time.time())
    start_s = end_s - 7 * 24 * 3600
    if n == 1:
        ts = [end_s]
    else:
        step = (end_s - start_s) / (n - 1)
        ts = [int(start_s + i * step) for i in range(n)]
    return {"ts": ts, "values": values}


def _change_24h_from_series(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    # 近 7 日均匀采样 → 约 24h 回看 1/7
    n = max(1, len(values) // 7)
    prev = values[-(n + 1)]
    cur = values[-1]
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 2)


def _match_row(coin: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    tag = coin.strip().lstrip("#$").upper()
    aliases = {tag}
    for extra in {
        "BTC": {"BITCOIN"},
        "ETH": {"ETHEREUM"},
        "SOL": {"SOLANA"},
        "BNB": {"BINANCE"},
    }.get(tag, set()):
        aliases.add(extra)

    for row in rows:
        sym = str(row.get("tokenSymbol") or row.get("token_symbol") or "").upper()
        name = _parse_i18n_name(row.get("cName") or row.get("project_name") or row.get("name")).upper().replace(" ", "")
        if sym in aliases or name in aliases:
            return row
    for row in rows:
        sym = str(row.get("tokenSymbol") or row.get("token_symbol") or "").upper()
        name = _parse_i18n_name(row.get("cName") or row.get("project_name") or row.get("name")).upper()
        if tag == sym or tag in name.split() or name.startswith(tag):
            return row
    return None


def fetch_eval_rank(*, page: int = 1, page_size: int = 100, client: HybridClient | None = None) -> list[dict]:
    data = pc_post(
        "pc/lang/eval/eval_rank",
        {"page": page, "pageSize": page_size},
        client=client,
    )
    if data is True or not isinstance(data, dict):
        return []
    arr = data.get("array")
    return [x for x in arr if isinstance(x, dict)] if isinstance(arr, list) else []


def fetch_project_metrics(project_id: int) -> dict[str, Any] | None:
    """官网表现页核心接口；过不了 WAF / 参数校验时返回 None。"""
    for body in (
        {"id": project_id},
        {"projectId": project_id},
        {"itemId": project_id},
        {"cid": project_id},
    ):
        try:
            data = pc_post("pc/project/get_metrics", body)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and data is not True and data:
            return data
    return None


def _pick_growth_fields(metrics: dict[str, Any]) -> dict[str, Any]:
    """从 get_metrics 响应里尽量抽出增长/热度字段（字段名随版本可能变）。"""
    out: dict[str, Any] = {}
    # 常见候选
    aliases = {
        "growth": ("growth", "rdGrowth", "growthIndex", "growthScore", "growth_eval", "rise", "soar"),
        "growth_change_24h": (
            "growthChange",
            "growthChange24",
            "growthPercentChange24",
            "rdGrowthChange",
            "riseChange",
        ),
        "growth_rank": ("growthRank", "riseRank", "rdGrowthRank", "soarRank"),
        "popularity": ("impact", "rdImpact", "hotIndex", "popularity", "eval", "impactScore"),
        "popularity_change_24h": (
            "impactChange",
            "evalChange",
            "hotChange",
            "percentChangeEval",
            "rdImpactChange",
        ),
        "popularity_rank": ("impactRank", "hotRank", "evalRank", "rank"),
        "total": ("total", "totalCount", "projectCount", "maxRank", "rankTotal"),
    }
    lower_map = {str(k).lower(): (k, v) for k, v in metrics.items()}
    for dest, cands in aliases.items():
        for c in cands:
            hit = lower_map.get(c.lower())
            if hit and hit[1] is not None:
                out[dest] = hit[1]
                break
    # 嵌套 dict 再扫一层
    if len(out) < 2:
        for v in metrics.values():
            if isinstance(v, dict):
                nested = _pick_growth_fields(v)
                for k, val in nested.items():
                    out.setdefault(k, val)
    return out


def total_projects(*, max_age_s: float = 3600.0) -> int | None:
    global _total_projects_cache
    now = time.time()
    if _total_projects_cache and now - _total_projects_cache[0] < max_age_s:
        return _total_projects_cache[1]
    try:
        data = skill_post("/id_map", {"type": 1})
        n = len(data) if isinstance(data, list) else None
        if n:
            _total_projects_cache = (now, n)
            return n
    except Exception:  # noqa: BLE001
        return _total_projects_cache[1] if _total_projects_cache else None
    return None


def _resolve_via_skill(coin: str) -> dict[str, Any] | None:
    tag = coin.strip().lstrip("#$").upper()
    hits = skill_post("/ser_inv", {"query": tag, "precise_x_search": False})
    if not isinstance(hits, list) or not hits:
        return None
    # type 1 = project
    projects = [h for h in hits if isinstance(h, dict) and int(h.get("type") or 1) == 1]
    pool = projects or [h for h in hits if isinstance(h, dict)]
    # prefer exact symbol/name
    best = None
    for h in pool:
        name = str(h.get("name") or "").upper().replace(" ", "")
        if name == tag or name.startswith(tag):
            best = h
            break
    if not best:
        best = pool[0]
    pid = best.get("id")
    if pid is None:
        return None
    detail = skill_post("/get_item", {"project_id": int(pid)})
    if not isinstance(detail, dict):
        detail = {}
    rank = None
    try:
        hot = skill_post("/hot_index", {"days": 1})
        if isinstance(hot, list):
            row = next(
                (
                    x
                    for x in hot
                    if isinstance(x, dict)
                    and (
                        int(x.get("project_id") or 0) == int(pid)
                        or str(x.get("token_symbol") or "").upper() == tag
                    )
                ),
                None,
            )
            if row:
                rank = int(row.get("rank") or 0) or None
    except Exception:  # noqa: BLE001
        pass
    try:
        eval_score = float(detail.get("eval")) if detail.get("eval") is not None else None
    except (TypeError, ValueError):
        eval_score = None
    return {
        "project_id": int(pid),
        "name": detail.get("project_name") or best.get("name"),
        "token_symbol": detail.get("token_symbol"),
        "eval": eval_score,
        "rank": rank,
        "rootdataurl": detail.get("rootdataurl") or best.get("rootdataurl"),
        "one_liner": detail.get("one_liner") or best.get("one_liner"),
        "source": "rootdata-skill",
    }


def _fetch_svg_series(cid: int | str, *, eval_score: float, eval_time_ms: int = 0) -> tuple[dict, str | None]:
    bt = eval_time_ms or int(time.time() * 1000)
    chart_url = f"{PUBLIC}/charts/1/{cid}/line_chart.svg?bt={bt}"
    try:
        http = _primp_client(timeout=20)
        resp = http.get(
            chart_url,
            headers={"Accept": "image/svg+xml,*/*", "Referer": f"{WWW}/"},
        )
        svg = getattr(resp, "text", "") or ""
        if "<svg" in svg:
            return _series_from_svg(svg, eval_score=eval_score or 1.0, eval_time_ms=bt), None
        return {"ts": [], "values": []}, f"not svg status={getattr(resp, 'status_code', None)}"
    except Exception as e:  # noqa: BLE001
        return {"ts": [], "values": []}, str(e)


def lookup_hot_trend(
    coin: str,
    *,
    client: HybridClient | None = None,
    page_size: int = 100,
    max_pages: int = 3,
) -> dict[str, Any]:
    """查币：RD 热度指数、排名、24h%、增长指数（若可得）、近 7 日趋势。"""
    del page_size, max_pages  # 保留签名兼容
    tag = coin.strip().lstrip("#$").upper() or "BTC"
    warnings: list[str] = []
    sources: list[str] = []

    skill_info: dict[str, Any] | None = None
    try:
        skill_info = _resolve_via_skill(tag)
        if skill_info:
            sources.append("skill")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"skill: {e}")

    rank_row: dict[str, Any] | None = None
    try:
        batch = fetch_eval_rank(page=1, page_size=100, client=client)
        rank_row = _match_row(tag, batch) if batch else None
        if rank_row:
            sources.append("eval_rank")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"eval_rank: {e}")

    if not skill_info and not rank_row:
        return {
            "coin": tag,
            "found": False,
            "hint": "未在 RootData 找到该项目",
            "warnings": warnings,
            "source": "rootdata",
        }

    # 合并字段：skill 提供稳定 project_id/eval；榜单补充 rankChange / chart
    project_id = None
    name = None
    token_symbol = None
    eval_score = None
    rank = None
    rank_change = 0
    eval_time_ms = 0
    chart_url = ""
    price = None
    rootdataurl = None

    if skill_info:
        project_id = skill_info.get("project_id")
        name = skill_info.get("name")
        token_symbol = skill_info.get("token_symbol")
        eval_score = skill_info.get("eval")
        rank = skill_info.get("rank")
        rootdataurl = skill_info.get("rootdataurl")

    if rank_row:
        project_id = project_id or rank_row.get("cid") or rank_row.get("id")
        name = name or _parse_i18n_name(rank_row.get("cName"))
        token_symbol = token_symbol or rank_row.get("tokenSymbol")
        try:
            if eval_score is None:
                eval_score = float(rank_row.get("eval") or 0)
        except (TypeError, ValueError):
            pass
        try:
            if rank is None:
                rank = int(rank_row.get("rank") or 0) or None
        except (TypeError, ValueError):
            pass
        try:
            rank_change = int(rank_row.get("rankChange") or 0)
        except (TypeError, ValueError):
            rank_change = 0
        try:
            eval_time_ms = int(rank_row.get("evalTime") or 0)
        except (TypeError, ValueError):
            eval_time_ms = 0
        chart_url = str(rank_row.get("chartUrl") or "")
        price = rank_row.get("price")

    try:
        eval_f = float(eval_score) if eval_score is not None else 0.0
    except (TypeError, ValueError):
        eval_f = 0.0

    cid = project_id
    if not chart_url and cid is not None:
        chart_url = f"{PUBLIC}/charts/1/{cid}/line_chart.svg?bt={eval_time_ms or int(time.time() * 1000)}"

    series, svg_err = ({"ts": [], "values": []}, None)
    if cid is not None:
        series, svg_err = _fetch_svg_series(cid, eval_score=eval_f, eval_time_ms=eval_time_ms)
        if series.get("ts"):
            sources.append("chart_svg")

    pop_change_24h = _change_24h_from_series(list(series.get("values") or []))
    if pop_change_24h is not None:
        # 标注为 SVG 估算，非官网精确字段
        pass

    total = None
    try:
        total = total_projects()
        if total:
            sources.append("id_map")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"id_map: {e}")

    growth = None
    growth_change_24h = None
    growth_rank = None
    metrics_raw_keys: list[str] | None = None
    if project_id is not None:
        try:
            metrics = fetch_project_metrics(int(project_id))
            if metrics:
                sources.append("get_metrics")
                metrics_raw_keys = list(metrics.keys())[:40]
                picked = _pick_growth_fields(metrics)
                if picked.get("growth") is not None:
                    try:
                        growth = float(picked["growth"])
                    except (TypeError, ValueError):
                        growth = picked["growth"]
                if picked.get("growth_change_24h") is not None:
                    try:
                        growth_change_24h = float(picked["growth_change_24h"])
                    except (TypeError, ValueError):
                        growth_change_24h = picked["growth_change_24h"]
                if picked.get("growth_rank") is not None:
                    try:
                        growth_rank = int(picked["growth_rank"])
                    except (TypeError, ValueError):
                        growth_rank = picked["growth_rank"]
                if picked.get("popularity") is not None and not eval_f:
                    try:
                        eval_f = float(picked["popularity"])
                    except (TypeError, ValueError):
                        pass
                if picked.get("popularity_change_24h") is not None:
                    try:
                        pop_change_24h = float(picked["popularity_change_24h"])
                    except (TypeError, ValueError):
                        pass
                if picked.get("total") is not None:
                    try:
                        total = int(picked["total"])
                    except (TypeError, ValueError):
                        pass
            else:
                warnings.append("get_metrics: 官网增长指数暂不可用（需过 WAF/正确会话）")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"get_metrics: {e}")

    return {
        "coin": tag,
        "found": True,
        "source": "+".join(sources) or "rootdata",
        "sources": sources,
        "cid": cid,
        "project_id": project_id,
        "name": name,
        "token_symbol": token_symbol,
        "rootdataurl": rootdataurl,
        # 热度（RD Popularity / Impact）
        "eval": eval_f,
        "popularity": eval_f,
        "popularity_max": 500,
        "rank": rank,
        "rank_change": rank_change,
        "popularity_change_24h": pop_change_24h,
        "popularity_change_24h_approx": True,
        # 增长（RD Growth）
        "growth": growth,
        "growth_max": 2000,
        "growth_change_24h": growth_change_24h,
        "growth_rank": growth_rank,
        "total_projects": total,
        "eval_time_ms": eval_time_ms,
        "price": price,
        "chart_url": chart_url,
        "series": series,
        "points": len(series.get("ts") or []),
        "svg_error": svg_err,
        "metrics_keys": metrics_raw_keys,
        "warnings": warnings,
    }
