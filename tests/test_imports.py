"""数据导入：迟到数据只形成新版本并给出差异；撤回与校验。"""
from __future__ import annotations

from service_09252_010.domain.errors import PermissionDeniedError, ValidationError
from support import INST_A, INST_B, PROJECT, SUPERVISOR, RigTestCase, TARGET


def rec(measure: str, period: str, value, evidence_id: str,
        caliber: str = TARGET) -> dict:
    return {"measure": measure, "period": period, "caliber": caliber,
            "value": value, "evidence_id": evidence_id}


class ImportTests(RigTestCase):
    def test_first_import_creates_version_one(self) -> None:
        ev = self.seed_evidence()
        result = self.seed_import([rec("enrollment_count", "2024-01", 100, ev)], ev)
        self.assertEqual(result["version_no"], 1)
        self.assertEqual(len(result["diff"]["added"]), 1)

    def test_late_data_forms_new_version_with_diff(self) -> None:
        """迟到数据不得改写旧版本：只追加为新版本，差异可见。"""
        ev = self.seed_evidence()
        self.seed_import([
            rec("enrollment_count", "2024-01", 100, ev),
            rec("enrollment_count", "2024-02", 80, ev),
        ], ev)
        late = self.seed_import([
            rec("enrollment_count", "2024-02", 95, ev),  # 迟到更正
            rec("enrollment_count", "2024-03", 110, ev),  # 迟到补报
        ], ev, reason="迟到数据")
        self.assertEqual(late["version_no"], 2)
        diff = late["diff"]
        self.assertEqual(diff["changed"], [{
            "measure": "enrollment_count", "period": "2024-02",
            "caliber": TARGET, "old_value": 80.0, "new_value": 95.0,
        }])
        self.assertEqual(len(diff["added"]), 1)
        self.assertEqual(diff["retracted"], [])

        # 旧版本可完整重放
        old = self.rig.imports.version_diff(PROJECT, 1)
        self.assertEqual(old["from"], 0)
        self.assertEqual(len(old["diff"]["added"]), 2)

    def test_retraction_marks_removal_in_diff(self) -> None:
        ev = self.seed_evidence()
        self.seed_import([rec("enrollment_count", "2024-01", 100, ev)], ev)
        result = self.seed_import([
            {"measure": "enrollment_count", "period": "2024-01",
             "caliber": TARGET, "evidence_id": ev, "retract": True},
        ], ev, reason="撤回误报")
        self.assertEqual(result["diff"]["retracted"], [{
            "measure": "enrollment_count", "period": "2024-01",
            "caliber": TARGET, "old_value": 100.0,
        }])

    def test_missing_value_recorded_as_null(self) -> None:
        ev = self.seed_evidence()
        result = self.seed_import(
            [rec("enrollment_count", "2024-01", None, ev)], ev
        )
        self.assertEqual(result["diff"]["added"][0]["value"], None)

    def test_duplicate_natural_key_in_batch_rejected(self) -> None:
        ev = self.seed_evidence()
        with self.assertRaises(ValidationError):
            self.seed_import([
                rec("enrollment_count", "2024-01", 100, ev),
                rec("enrollment_count", "2024-01", 101, ev),
            ], ev)

    def test_unknown_evidence_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.seed_import([rec("enrollment_count", "2024-01", 1, "ev-x")],
                             "ev-x")

    def test_retract_of_absent_key_rejected(self) -> None:
        ev = self.seed_evidence()
        with self.assertRaises(ValidationError):
            self.seed_import([
                {"measure": "enrollment_count", "period": "2024-01",
                 "caliber": TARGET, "evidence_id": ev, "retract": True},
            ], ev)

    def test_bad_value_type_rejected(self) -> None:
        ev = self.seed_evidence()
        with self.assertRaises(ValidationError):
            self.seed_import([rec("enrollment_count", "2024-01", "一百", ev)],
                             ev)

    def test_import_requires_grant(self) -> None:
        ev = self.seed_evidence()
        with self.assertRaises(PermissionDeniedError):
            self.rig.imports.import_batch(
                INST_B, PROJECT,
                records=[rec("enrollment_count", "2024-01", 1, ev)],
                reason="未授权导入",
            )
        # 授权机构可以导入
        self.rig.grant(INST_A.institution_id, permission="import")
        result = self.rig.imports.import_batch(
            INST_A, PROJECT,
            records=[rec("enrollment_count", "2024-01", 1, ev)],
            reason="授权导入",
        )
        self.assertEqual(result["version_no"], 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
