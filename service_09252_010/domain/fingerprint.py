"""可复算指纹：对规范化 JSON 取 SHA-256，保证同一输入必得同一指纹。"""
from __future__ import annotations

import hashlib
import json


def canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
