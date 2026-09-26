"""观察期工具：期间格式为 YYYY-MM，窗口为闭区间，支持跨年度。"""
from __future__ import annotations

import re

from .errors import ValidationError

_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def validate_period(period: str) -> str:
    """校验并返回规范期间串，非法时抛 ValidationError。"""
    if not isinstance(period, str) or not _PERIOD_RE.match(period):
        raise ValidationError(f"非法期间格式: {period!r}，应为 YYYY-MM")
    return period


def period_key(period: str) -> int:
    """把期间转换为可比较的整数键。"""
    validate_period(period)
    year, month = int(period[:4]), int(period[5:7])
    return year * 12 + (month - 1)


def iter_periods(start: str, end: str) -> list[str]:
    """生成闭区间 [start, end] 的全部期间，支持跨年。"""
    start_key, end_key = period_key(start), period_key(end)
    if start_key > end_key:
        raise ValidationError(f"观察期起点 {start} 晚于终点 {end}")
    periods: list[str] = []
    key = start_key
    while key <= end_key:
        periods.append(f"{key // 12:04d}-{key % 12 + 1:02d}")
        key += 1
    return periods
