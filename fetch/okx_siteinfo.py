"""OKX x-site-info: reverse(base64(json)). No browser needed."""

from __future__ import annotations

import base64
import json
import os


def encode_site_info(
    *,
    region: str = "JP",
    code: str = "OKX_GLOBAL",
    entity: int = 1,
) -> str:
    payload = json.dumps(
        {"region": region, "code": code, "entity": entity},
        separators=(",", ":"),
    )
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return b64[::-1]


def site_info_from_env() -> str:
    override = os.getenv("OKX_X_SITE_INFO")
    if override:
        return override.strip()
    return encode_site_info(
        region=os.getenv("OKX_REGION", "JP"),
        code=os.getenv("OKX_SITE_CODE", "OKX_GLOBAL"),
        entity=int(os.getenv("OKX_ENTITY", "1")),
    )
