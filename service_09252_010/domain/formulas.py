"""指标公式求值：在观察期窗口内把已换算的观测聚合为指标值。

公式规格（JSON 可序列化）：
- {"type": "sum", "measure": "enrollment_count"}
- {"type": "ratio", "numerator": "employed_count", "denominator": "graduate_count", "scale": 100}
- {"type": "latest", "measure": "..."}
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import MissingDataError, ValidationError
from .models import MissingPolicy

_ALLOWED_SCALE_DEFAULT = 1.0


@dataclass(frozen=True)
class LineResult:
    """单个指标在窗口内的求值结果。"""

    value: float | None
    covered_periods: tuple[str, ...] = ()
    missing_periods: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def validate_formula(formula: dict) -> None:
    """登记指标版本前校验公式规格。"""
    if not isinstance(formula, dict):
        raise ValidationError("公式必须为对象")
    ftype = formula.get("type")
    if ftype == "sum" or ftype == "latest":
        if not formula.get("measure"):
            raise ValidationError(f"{ftype} 公式缺少 measure")
    elif ftype == "ratio":
        if not formula.get("numerator") or not formula.get("denominator"):
            raise ValidationError("ratio 公式缺少 numerator/denominator")
        scale = formula.get("scale", _ALLOWED_SCALE_DEFAULT)
        if not isinstance(scale, (int, float)) or scale == 0:
            raise ValidationError("ratio 公式 scale 必须为非零数值")
    else:
        raise ValidationError(f"未知公式类型: {ftype!r}")


def formula_measures(formula: dict) -> tuple[str, ...]:
    """公式依赖的度量集合，用于计算时收集输入。"""
    if formula["type"] == "ratio":
        return (formula["numerator"], formula["denominator"])
    return (formula["measure"],)


def _series(
    measure: str,
    periods: list[str],
    values: dict[str, dict[str, float | None]],
    policy: MissingPolicy,
    missing: list[str],
) -> list[float | None]:
    """取某度量在窗口内的值序列，按缺失策略处理。"""
    series: list[float | None] = []
    by_period = values.get(measure, {})
    for period in periods:
        value = by_period.get(period)
        if value is None:
            if policy is MissingPolicy.FAIL:
                raise MissingDataError(f"度量 {measure} 在期间 {period} 缺失")
            if policy is MissingPolicy.ZERO:
                series.append(0.0)
            else:
                series.append(None)
            missing.append(period)
        else:
            series.append(value)
    return series


def evaluate(
    formula: dict,
    policy: MissingPolicy,
    periods: list[str],
    values: dict[str, dict[str, float | None]],
) -> LineResult:
    """对公式求值。values: measure -> period -> 已换算值（None 表示缺失）。"""
    validate_formula(formula)
    missing: list[str] = []
    notes: list[str] = []
    ftype = formula["type"]

    if ftype == "ratio":
        num_missing: list[str] = []
        den_missing: list[str] = []
        numerator = _series(formula["numerator"], periods, values, policy, num_missing)
        denominator = _series(formula["denominator"], periods, values, policy, den_missing)
        num_total = sum(v for v in numerator if v is not None)
        den_total = sum(v for v in denominator if v is not None)
        missing = sorted(set(num_missing) | set(den_missing))
        if den_total == 0:
            notes.append("分母为零或全部缺失，无法计算比率")
            return LineResult(None, _covered(periods, missing), tuple(missing), tuple(notes))
        scale = float(formula.get("scale", _ALLOWED_SCALE_DEFAULT))
        value = num_total / den_total * scale
        if num_missing or den_missing:
            notes.append("部分期间缺失，按缺失策略处理")
        return LineResult(value, _covered(periods, missing), tuple(missing), tuple(notes))

    series = _series(formula["measure"], periods, values, policy, missing)
    present = [v for v in series if v is not None]
    if ftype == "sum":
        if not present:
            notes.append("窗口内无有效数据")
            return LineResult(None, (), tuple(missing), tuple(notes))
        return LineResult(sum(present), _covered(periods, missing), tuple(missing), tuple(notes))

    # latest：取窗口内最后一个非缺失期间的值
    for period, value in zip(reversed(periods), reversed(series)):
        if value is not None:
            return LineResult(value, (period,), tuple(missing), tuple(notes))
    notes.append("窗口内无有效数据")
    return LineResult(None, (), tuple(missing), tuple(notes))


def _covered(periods: list[str], missing: list[str]) -> tuple[str, ...]:
    missing_set = set(missing)
    return tuple(p for p in periods if p not in missing_set)
