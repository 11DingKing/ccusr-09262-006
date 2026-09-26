"""口径换算：把各国口径的观测值换算到目标口径。

规则键为 measure|from_caliber|to_caliber；换算式 canonical = value * factor + offset。
缺失规则是显式错误——主管单位必须保留每一笔换算的依据。
"""
from __future__ import annotations

from .errors import ValidationError
from .models import SnapshotRow


def rule_key(measure: str, from_caliber: str, to_caliber: str) -> str:
    return f"{measure}|{from_caliber}|{to_caliber}"


def parse_rule_key(key: str) -> tuple[str, str, str]:
    parts = key.split("|")
    if len(parts) != 3 or not all(parts):
        raise ValidationError(f"非法规则键: {key!r}")
    return parts[0], parts[1], parts[2]


def convert_rows(
    rows: list[SnapshotRow],
    rules: dict[str, tuple[float, float]],
    target_caliber: str,
) -> tuple[list[SnapshotRow], list[str]]:
    """按规则表换算观测值。

    返回 (换算后行, 缺失规则的规则键列表)。已是目标口径的行原样保留；
    缺失值（value 为 None）不参与换算，原样传递以保留缺失语义。
    """
    converted: list[SnapshotRow] = []
    missing_keys: list[str] = []
    for row in rows:
        if row.caliber == target_caliber or row.value is None:
            # 已是目标口径，或缺失值（不参与换算，保留缺失语义）
            converted.append(row)
            continue
        key = rule_key(row.measure, row.caliber, target_caliber)
        rule = rules.get(key)
        if rule is None:
            if key not in missing_keys:
                missing_keys.append(key)
            continue
        factor, offset = rule
        converted.append(
            SnapshotRow(
                measure=row.measure,
                period=row.period,
                caliber=target_caliber,
                value=row.value * factor + offset,
                evidence_id=row.evidence_id,
            )
        )
    return converted, missing_keys
