"""
HTTP transport: primp browser impersonation + residential proxy rotation + captcha hook.

No Playwright / no Chromium. JSON APIs are primary; selectolax only if HTML bootstrap needed.
"""

from __future__ import annotations

import os
import random
import socket
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import primp

CaptchaSolver = Callable[[str, Mapping[str, Any]], str | None]

# 进程级共享 primp 客户端池：(impersonate, impersonate_os, proxy, timeout) -> Client。
# reqwest/primp 的 Client 可安全跨线程复用；避免每次请求重付 TLS+代理隧道握手。
_SHARED_CLIENTS: dict[tuple[str, str, str, float], "primp.Client"] = {}
_SHARED_CLIENTS_LOCK = threading.Lock()

_LOCAL_PROXY_CACHE: tuple[float, str | None] | None = None


def shared_client(
    impersonate: str,
    impersonate_os: str | None,
    proxy: str | None,
    timeout: float,
) -> "primp.Client":
    key = (impersonate, impersonate_os or "", proxy or "", round(float(timeout), 3))
    with _SHARED_CLIENTS_LOCK:
        client = _SHARED_CLIENTS.get(key)
        if client is None:
            kwargs: dict[str, Any] = {"impersonate": impersonate, "timeout": timeout}
            if impersonate_os:
                kwargs["impersonate_os"] = impersonate_os
            if proxy:
                kwargs["proxy"] = proxy
            client = primp.Client(**kwargs)
            _SHARED_CLIENTS[key] = client
        return client


def _split_proxy_pool(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.replace(";", "\n").splitlines() if p.strip()]


def detect_local_proxy() -> str | None:
    """Env first, then common Clash/V2Ray loopback ports. 扫描结果进程级缓存 60s。"""
    global _LOCAL_PROXY_CACHE
    now = time.monotonic()
    if _LOCAL_PROXY_CACHE is not None:
        at, value = _LOCAL_PROXY_CACHE
        if value is not None or now - at < 60.0:
            return value
    for env in (
        "RESIDENTIAL_PROXY_POOL",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
        "ALL_PROXY",
    ):
        raw = (os.getenv(env) or "").strip()
        if raw:
            value = raw.split(",")[0].strip().split(";")[0].strip()
            _LOCAL_PROXY_CACHE = (now, value)
            return value
    for port in (7897, 7890, 10809, 7891, 1080, 8080):
        s = socket.socket()
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
            value = f"http://127.0.0.1:{port}"
            _LOCAL_PROXY_CACHE = (now, value)
            return value
        except OSError:
            pass
        finally:
            s.close()
    # 未发现代理：60s 后才重扫，避免每请求都付端口扫描开销
    _LOCAL_PROXY_CACHE = (now, None)
    return None


def _retry_after_seconds(resp) -> float | None:
    """读取 Retry-After 头（仅支持秒数形式），封顶 60s；HTTP-date 形式返回 None 走默认退避。"""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return min(value, 60.0)


class HybridClient:
    """Industrial-style thin client around primp."""

    def __init__(
        self,
        *,
        impersonate: str | None = None,
        impersonate_os: str | None = None,
        proxy_pool: list[str] | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        captcha_solver: CaptchaSolver | None = None,
    ) -> None:
        # primp 1.3.x: unknown profiles fall back to random with a stderr warning
        # primp pyi examples use chrome_146; older wheels may warn and fall back to random
        self.impersonate = impersonate or os.getenv("FETCH_IMPERSONATE", "chrome_146")
        self.impersonate_os = impersonate_os or os.getenv("FETCH_IMPERSONATE_OS") or None
        env_pool = _split_proxy_pool(
            os.getenv("RESIDENTIAL_PROXY_POOL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        )
        self._prefer_direct = proxy_pool is None and not env_pool
        if proxy_pool is not None:
            self.proxy_pool = proxy_pool
        else:
            self.proxy_pool = list(env_pool)
            if not self.proxy_pool:
                local = detect_local_proxy()
                if local:
                    self.proxy_pool = [local]
        self.timeout = timeout
        self.max_retries = max_retries
        self.captcha_solver = captcha_solver
        self._proxy_idx = 0
        # host -> (mode, since)；直连通就直连，更快；不通再代理。proxy 模式 30s 后重测，
        # 避免某次抖动把 host 永久钉在坏代理上
        self._route: dict[str, tuple[str, float]] = {}
        self._route_proxy_ttl = float(os.getenv("ROUTE_PROXY_TTL", "30"))

    def _next_proxy(self) -> str | None:
        if not self.proxy_pool:
            return None
        proxy = self.proxy_pool[self._proxy_idx % len(self.proxy_pool)]
        self._proxy_idx += 1
        if len(self.proxy_pool) > 1 and random.random() < 0.15:
            proxy = random.choice(self.proxy_pool)
        return proxy

    @staticmethod
    def _host_of(url: str) -> str:
        try:
            from urllib.parse import urlparse

            return (urlparse(url).hostname or "").lower()
        except Exception:
            return ""

    def _host_tcp_ok(self, host: str, port: int = 443, wait: float = 1.2) -> bool:
        if not host:
            return False
        s = socket.socket()
        s.settimeout(wait)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _pick_proxy(self, url: str, *, force_proxy: bool = False) -> str | None:
        if force_proxy or not self._prefer_direct:
            return self._next_proxy()
        host = self._host_of(url)
        entry = self._route.get(host)
        now = time.monotonic()
        if entry is not None:
            mode, since = entry
            if mode == "direct":
                return None
            if now - since < self._route_proxy_ttl:
                return self._next_proxy()
            # proxy 缓存过期：重测直连，通了就切回直连
            mode = "direct" if self._host_tcp_ok(host) else "proxy"
            self._route[host] = (mode, now)
            if mode == "direct":
                return None
            return self._next_proxy()
        mode = "direct" if self._host_tcp_ok(host) else "proxy"
        self._route[host] = (mode, now)
        if mode == "direct":
            return None
        return self._next_proxy()

    def _client(self, proxy: str | None) -> primp.Client:
        return shared_client(self.impersonate, self.impersonate_os, proxy, self.timeout)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
    ) -> Any:
        last_err: Exception | None = None
        method_u = method.upper()
        full_url = url
        if params:
            qs = urlencode({k: str(v) for k, v in params.items() if v is not None})
            full_url = f"{url}?{qs}" if "?" not in url else f"{url}&{qs}"
        host = self._host_of(full_url)
        force_proxy = False

        for attempt in range(self.max_retries):
            proxy = self._pick_proxy(full_url, force_proxy=force_proxy)
            client = self._client(proxy)
            try:
                kwargs: dict[str, Any] = {"headers": dict(headers or {})}
                if json_body is not None:
                    kwargs["json"] = json_body
                if data is not None:
                    kwargs["data"] = data

                if method_u == "GET":
                    resp = client.get(full_url, **kwargs)
                elif method_u == "POST":
                    resp = client.post(full_url, **kwargs)
                else:
                    resp = client.request(method_u, full_url, **kwargs)

                status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
                text = getattr(resp, "text", "") or ""

                if status in (403, 429, 503) or _looks_like_challenge(text):
                    # 429/503 带 Retry-After 时按服务器指示等待，优先于固定退避
                    if status in (429, 503):
                        wait = _retry_after_seconds(resp)
                        if wait is not None and attempt + 1 < self.max_retries:
                            time.sleep(wait)
                            continue
                    if self.captcha_solver:
                        token = self.captcha_solver(full_url, {"status": status, "body": text[:2000]})
                        if token:
                            hdrs = dict(headers or {})
                            hdrs["x-captcha-token"] = token
                            headers = hdrs
                            time.sleep(0.2 + random.random() * 0.4)
                            continue
                    if attempt + 1 < self.max_retries:
                        time.sleep(0.4 * (attempt + 1) + random.random() * 0.3)
                        continue

                if hasattr(resp, "json"):
                    try:
                        return resp.json()
                    except Exception:
                        pass
                try:
                    import orjson

                    return orjson.loads(text)
                except Exception:
                    return {"_raw": text, "_status": status}
            except Exception as e:  # noqa: BLE001 — transport absorbs & retries
                last_err = e
                msg = str(e).lower()
                timed_out = "timeout" in msg or "timed out" in msg or "connect" in msg
                if proxy:
                    # 代理失败：立刻切回直连，别把 host 钉在坏代理上
                    if host:
                        self._route[host] = ("direct", time.monotonic())
                    force_proxy = False
                    continue
                if not proxy and timed_out:
                    if host:
                        self._route[host] = ("proxy", time.monotonic())
                    force_proxy = True
                    continue
                time.sleep(0.35 * (attempt + 1))
        if last_err:
            raise last_err
        raise RuntimeError(f"request failed: {method} {url}")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)


def _looks_like_challenge(text: str) -> bool:
    low = (text or "").lower()
    needles = (
        "cf-challenge",
        "attention required",
        "captcha",
        "akamai",
        "access denied",
        "just a moment",
    )
    return any(n in low for n in needles)


def default_client() -> HybridClient:
    from .captcha import env_http_solver

    solver = env_http_solver if os.getenv("CAPTCHA_SOLVER_URL") else None
    return HybridClient(captcha_solver=solver)
