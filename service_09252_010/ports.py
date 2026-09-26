"""可替换端口：时间与标识。测试用确定性实现替换，保证状态变化可稳定复现。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> str:
        """返回 ISO8601 UTC 时间串。"""
        ...


class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str:
        """生成带前缀的标识。"""
        ...


class SystemClock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SystemIdGenerator:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"
