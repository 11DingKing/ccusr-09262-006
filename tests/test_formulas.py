"""公式求值与观察期窗口：缺失值策略、跨年度窗口、比率与最新值。"""
from __future__ import annotations

import unittest

from service_09252_010.domain.errors import MissingDataError, ValidationError
from service_09252_010.domain.formulas import evaluate
from service_09252_010.domain.models import MissingPolicy
from service_09252_010.domain.periods import iter_periods, period_key


class PeriodWindowTests(unittest.TestCase):
    def test_cross_year_window(self) -> None:
        periods = iter_periods("2023-11", "2024-02")
        self.assertEqual(periods, ["2023-11", "2023-12", "2024-01", "2024-02"])

    def test_single_month_window(self) -> None:
        self.assertEqual(iter_periods("2024-06", "2024-06"), ["2024-06"])

    def test_reversed_window_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            iter_periods("2024-12", "2024-01")

    def test_invalid_period_format(self) -> None:
        for bad in ("2024-13", "2024-00", "24-01", "2024-1", "abcd-ef"):
            with self.assertRaises(ValidationError, msg=bad):
                period_key(bad)


class MissingValueTests(unittest.TestCase):
    WINDOW = ["2024-01", "2024-02", "2024-03"]
    FORMULA = {"type": "sum", "measure": "enrollment_count"}

    def _values(self) -> dict:
        # 2024-02 缺失（None），2024-03 无记录
        return {"enrollment_count": {"2024-01": 10.0, "2024-02": None}}

    def test_skip_policy_ignores_missing(self) -> None:
        result = evaluate(self.FORMULA, MissingPolicy.SKIP, self.WINDOW,
                          self._values())
        self.assertEqual(result.value, 10.0)
        self.assertEqual(result.missing_periods, ("2024-02", "2024-03"))
        self.assertEqual(result.covered_periods, ("2024-01",))

    def test_zero_policy_counts_missing_as_zero(self) -> None:
        result = evaluate(self.FORMULA, MissingPolicy.ZERO, self.WINDOW,
                          self._values())
        self.assertEqual(result.value, 10.0)
        self.assertEqual(result.missing_periods, ("2024-02", "2024-03"))

    def test_fail_policy_raises(self) -> None:
        with self.assertRaises(MissingDataError):
            evaluate(self.FORMULA, MissingPolicy.FAIL, self.WINDOW,
                     self._values())

    def test_all_missing_skip_yields_none(self) -> None:
        result = evaluate(self.FORMULA, MissingPolicy.SKIP, self.WINDOW, {})
        self.assertIsNone(result.value)
        self.assertEqual(result.missing_periods, tuple(self.WINDOW))


class RatioAndLatestTests(unittest.TestCase):
    WINDOW = ["2023-12", "2024-01"]  # 跨年

    def test_ratio_over_cross_year_window(self) -> None:
        formula = {"type": "ratio", "numerator": "employed_count",
                   "denominator": "graduate_count", "scale": 100}
        values = {
            "employed_count": {"2023-12": 40.0, "2024-01": 50.0},
            "graduate_count": {"2023-12": 60.0, "2024-01": 60.0},
        }
        result = evaluate(formula, MissingPolicy.SKIP, self.WINDOW, values)
        self.assertAlmostEqual(result.value, 75.0)

    def test_ratio_zero_denominator_yields_none(self) -> None:
        formula = {"type": "ratio", "numerator": "employed_count",
                   "denominator": "graduate_count", "scale": 100}
        values = {"employed_count": {"2023-12": 5.0},
                  "graduate_count": {"2023-12": 0.0}}
        result = evaluate(formula, MissingPolicy.SKIP, self.WINDOW, values)
        self.assertIsNone(result.value)
        self.assertTrue(result.notes)

    def test_latest_takes_last_present_period(self) -> None:
        formula = {"type": "latest", "measure": "trained_teacher_count"}
        values = {"trained_teacher_count": {"2023-12": 7.0, "2024-01": 9.0}}
        result = evaluate(formula, MissingPolicy.SKIP, self.WINDOW, values)
        self.assertEqual(result.value, 9.0)
        self.assertEqual(result.covered_periods, ("2024-01",))


if __name__ == "__main__":
    unittest.main()
