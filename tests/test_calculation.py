"""计算服务：跨年度窗口、幂等提交、断点恢复、报告不可变与迟到数据。"""
from __future__ import annotations

import threading
import unittest

from service_09252_010.domain.errors import (
    ConflictError,
    MissingDataError,
    PermissionDeniedError,
    ValidationError,
)
from service_09252_010.domain.models import Principal
from support import INST_A, INST_B, PROJECT, SUPERVISOR, RigTestCase, TARGET

WINDOW = ("2023-11", "2024-02")  # 跨年度观察期


class SeededCase(RigTestCase):
    """预置三类指标、一条换算规则与跨年度数据。"""

    def setUp(self) -> None:
        super().setUp()
        rig = self.rig
        rig.indicators.register(
            SUPERVISOR, code="enrollment_total", name="招生总数",
            category="招生", unit="人",
            formula={"type": "sum", "measure": "enrollment_count"},
            missing_policy="skip",
        )
        rig.indicators.register(
            SUPERVISOR, code="employment_rate", name="就业率",
            category="就业", unit="%",
            formula={"type": "ratio", "numerator": "employed_count",
                     "denominator": "graduate_count", "scale": 100},
            missing_policy="skip",
        )
        rig.indicators.register(
            SUPERVISOR, code="trained_teachers", name="师资培养数",
            category="师资培养", unit="人",
            formula={"type": "sum", "measure": "trained_teacher_count"},
            missing_policy="skip",
        )
        self.evidence = self.seed_evidence()
        self.seed_rule(factor=2.0, measure="enrollment_count")
        rows = [
            ("enrollment_count", "2023-11", "DE-DUAL", 10),
            ("enrollment_count", "2023-12", TARGET, 20),
            ("enrollment_count", "2024-01", "DE-DUAL", 30),
            ("enrollment_count", "2024-02", TARGET, None),  # 缺失值
            ("employed_count", "2024-01", TARGET, 45),
            ("graduate_count", "2024-01", TARGET, 60),
            ("trained_teacher_count", "2023-12", TARGET, 7),
            ("trained_teacher_count", "2024-02", TARGET, 9),
        ]
        self.seed_import(
            [{"measure": m, "period": p, "caliber": c, "value": v,
              "evidence_id": self.evidence} for m, p, c, v in rows],
            self.evidence,
        )
        rig.grant(INST_A.institution_id, permission="calculate")
        rig.grant(INST_A.institution_id, permission="view")

    def submit_and_run(self, key: str = "idem-1") -> str:
        submitted = self.rig.calculation.submit(
            INST_A, project_id=PROJECT, window_start=WINDOW[0],
            window_end=WINDOW[1], target_caliber=TARGET,
            idempotency_key=key,
        )
        result = self.rig.calculation.run(submitted["task_id"])
        return result["report_id"]

    def line(self, report_id: str, code: str) -> dict:
        report = self.rig.calculation.get_report(SUPERVISOR, report_id)
        return next(l for l in report["lines"] if l["code"] == code)


class CrossYearCalculationTests(SeededCase):
    def test_cross_year_window_aggregates_with_conversion(self) -> None:
        report_id = self.submit_and_run()
        enrollment = self.line(report_id, "enrollment_total")
        # 10*2(DE 换算) + 20 + 30*2(DE 换算) = 100，2024-02 缺失跳过
        self.assertEqual(enrollment["value"], 100.0)
        self.assertEqual(enrollment["missing_periods"], ["2024-02"])
        self.assertEqual(enrollment["covered_periods"],
                         ["2023-11", "2023-12", "2024-01"])

        employment = self.line(report_id, "employment_rate")
        self.assertAlmostEqual(employment["value"], 75.0)

        teachers = self.line(report_id, "trained_teachers")
        self.assertEqual(teachers["value"], 16.0)
        self.assertEqual(teachers["missing_periods"], ["2023-11", "2024-01"])

    def test_report_pins_conversion_basis(self) -> None:
        report_id = self.submit_and_run()
        report = self.rig.calculation.get_report(SUPERVISOR, report_id)
        self.assertEqual(report["pins"]["data_version"], 1)
        self.assertEqual(
            report["pins"]["rules"],
            {f"enrollment_count|DE-DUAL|{TARGET}": 1},
        )
        self.assertEqual(report["pins"]["indicators"]["enrollment_total"], 1)
        self.assertTrue(report["input_fingerprint"])
        self.assertTrue(report["result_fingerprint"])
        # 证据来源可追溯到行
        enrollment = self.line(report_id, "enrollment_total")
        self.assertEqual(enrollment["evidence_ids"], [self.evidence])

    def test_reverify_matches(self) -> None:
        report_id = self.submit_and_run()
        result = self.rig.calculation.reverify(SUPERVISOR, report_id)
        self.assertTrue(result["input_match"])
        self.assertTrue(result["result_match"])


class IdempotencyTests(SeededCase):
    def test_same_key_same_params_returns_existing_task(self) -> None:
        first = self.rig.calculation.submit(
            INST_A, project_id=PROJECT, window_start=WINDOW[0],
            window_end=WINDOW[1], target_caliber=TARGET,
            idempotency_key="key-1",
        )
        second = self.rig.calculation.submit(
            INST_A, project_id=PROJECT, window_start=WINDOW[0],
            window_end=WINDOW[1], target_caliber=TARGET,
            idempotency_key="key-1",
        )
        self.assertFalse(first["already_exists"])
        self.assertTrue(second["already_exists"])
        self.assertEqual(first["task_id"], second["task_id"])

    def test_same_key_different_params_conflicts(self) -> None:
        self.rig.calculation.submit(
            INST_A, project_id=PROJECT, window_start=WINDOW[0],
            window_end=WINDOW[1], target_caliber=TARGET,
            idempotency_key="key-1",
        )
        with self.assertRaises(ConflictError):
            self.rig.calculation.submit(
                INST_A, project_id=PROJECT, window_start="2024-01",
                window_end=WINDOW[1], target_caliber=TARGET,
                idempotency_key="key-1",
            )

    def test_run_is_idempotent(self) -> None:
        submitted = self.rig.calculation.submit(
            INST_A, project_id=PROJECT, window_start=WINDOW[0],
            window_end=WINDOW[1], target_caliber=TARGET,
            idempotency_key="key-1",
        )
        first = self.rig.calculation.run(submitted["task_id"])
        second = self.rig.calculation.run(submitted["task_id"])
        self.assertEqual(first["report_id"], second["report_id"])
        reports = self.rig.calculation.list_reports(SUPERVISOR, PROJECT)
        self.assertEqual(len(reports), 1)

    def test_concurrent_submit_converges_to_one_task(self) -> None:
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[Exception] = []

        def submit() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(self.rig.calculation.submit(
                    INST_A, project_id=PROJECT, window_start=WINDOW[0],
                    window_end=WINDOW[1], target_caliber=TARGET,
                    idempotency_key="key-race",
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["task_id"], results[1]["task_id"])
        self.assertEqual(
            sorted(r["already_exists"] for r in results), [False, True]
        )


class CheckpointRecoveryTests(SeededCase):
    def test_resume_skips_completed_steps(self) -> None:
        svc = self.rig.calculation
        task_id = svc.submit(
            INST_A, project_id=PROJECT, window_start=WINDOW[0],
            window_end=WINDOW[1], target_caliber=TARGET,
            idempotency_key="key-ckpt",
        )["task_id"]
        # 模拟：前两步完成后进程崩溃
        svc._run_step(task_id, "snapshot")
        svc._run_step(task_id, "convert")

        executed: list[str] = []
        original = svc._execute_step

        def spy(store, task, step, checkpoints):
            executed.append(step)
            return original(store, task, step, checkpoints)

        svc._execute_step = spy
        try:
            result = svc.run(task_id)
        finally:
            svc._execute_step = original
        self.assertTrue(result["resumed"])
        self.assertEqual(executed, ["aggregate"])  # 已落断点的步骤未重跑
        task = svc.get_task(task_id)
        self.assertEqual(task["status"], "done")

    def test_crash_marks_failed_then_resume_completes(self) -> None:
        svc = self.rig.calculation
        task_id = svc.submit(
            INST_A, project_id=PROJECT, window_start=WINDOW[0],
            window_end=WINDOW[1], target_caliber=TARGET,
            idempotency_key="key-crash",
        )["task_id"]
        original = svc._execute_step

        def boom(store, task, step, checkpoints):
            if step == "aggregate":
                raise RuntimeError("模拟进程崩溃")
            return original(store, task, step, checkpoints)

        svc._execute_step = boom
        try:
            with self.assertRaises(RuntimeError):
                svc.run(task_id)
        finally:
            svc._execute_step = original

        task = svc.get_task(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["current_step"], "aggregate")
        self.assertIn("模拟进程崩溃", task["error"])
        self.assertEqual(sorted(task["completed_steps"]),
                         ["convert", "snapshot"])

        # 恢复执行：与无崩溃运行得到相同结果指纹
        recovered = svc.run(task_id)
        clean_report = self.submit_and_run(key="key-clean")
        recovered_lines = self.rig.calculation.get_report(
            SUPERVISOR, recovered["report_id"])["result_fingerprint"]
        clean_lines = self.rig.calculation.get_report(
            SUPERVISOR, clean_report)["result_fingerprint"]
        self.assertEqual(recovered_lines, clean_lines)

    def test_missing_rule_stops_at_convert_checkpoint(self) -> None:
        # 补一条无换算规则的口径数据
        ev = self.seed_evidence("b")
        self.seed_import(
            [{"measure": "enrollment_count", "period": "2024-02",
              "caliber": "FR-APP", "value": 5, "evidence_id": ev}],
            ev, reason="新增法国口径数据",
        )
        svc = self.rig.calculation
        task_id = svc.submit(
            INST_A, project_id=PROJECT, window_start=WINDOW[0],
            window_end=WINDOW[1], target_caliber=TARGET,
            idempotency_key="key-norule",
        )["task_id"]
        with self.assertRaises(ValidationError):
            svc.run(task_id)
        task = svc.get_task(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["current_step"], "convert")
        self.assertEqual(task["completed_steps"], ["snapshot"])

        # 补齐规则并会签后，从断点恢复完成
        self.seed_rule(factor=1.0, measure="enrollment_count",
                       from_caliber="FR-APP")
        result = svc.run(task_id)
        enrollment = self.line(result["report_id"], "enrollment_total")
        self.assertEqual(enrollment["value"], 105.0)  # 100 + 5*1


class ImmutabilityTests(SeededCase):
    def test_indicator_update_does_not_rewrite_old_report(self) -> None:
        report_id = self.submit_and_run()
        before = self.rig.calculation.get_report(SUPERVISOR, report_id)

        self.rig.indicators.add_version(
            SUPERVISOR, "enrollment_total",
            formula={"type": "sum", "measure": "other_measure"},
            missing_policy="skip",
        )
        after = self.rig.calculation.get_report(SUPERVISOR, report_id)
        self.assertEqual(before["lines"], after["lines"])
        self.assertEqual(before["result_fingerprint"],
                         after["result_fingerprint"])
        self.assertEqual(self.line(report_id, "enrollment_total")
                         ["version_no"], 1)

        # 旧报告仍可按固化版本复算
        check = self.rig.calculation.reverify(SUPERVISOR, report_id)
        self.assertTrue(check["result_match"])

        # 新计算使用新版本定义
        new_report = self.submit_and_run(key="idem-2")
        new_line = self.line(new_report, "enrollment_total")
        self.assertEqual(new_line["version_no"], 2)
        self.assertIsNone(new_line["value"])  # other_measure 无数据

    def test_late_data_forms_new_version_and_reports_differ(self) -> None:
        report_v1 = self.submit_and_run(key="idem-v1")
        # 迟到数据：补上 2024-02 的缺失值
        late = self.seed_import(
            [{"measure": "enrollment_count", "period": "2024-02",
              "caliber": TARGET, "value": 40,
              "evidence_id": self.evidence}],
            self.evidence, reason="迟到补报",
        )
        self.assertEqual(late["version_no"], 2)
        self.assertEqual(late["diff"]["changed"], [{
            "measure": "enrollment_count", "period": "2024-02",
            "caliber": TARGET, "old_value": None, "new_value": 40.0,
        }])

        report_v2 = self.submit_and_run(key="idem-v2")
        self.assertEqual(self.line(report_v1, "enrollment_total")["value"],
                         100.0)
        v2_line = self.line(report_v2, "enrollment_total")
        self.assertEqual(v2_line["value"], 140.0)
        self.assertEqual(v2_line["missing_periods"], [])

        old = self.rig.calculation.get_report(SUPERVISOR, report_v1)
        new = self.rig.calculation.get_report(SUPERVISOR, report_v2)
        self.assertEqual(old["data_version_no"], 1)
        self.assertEqual(new["data_version_no"], 2)
        # 旧报告按固化的数据版本复算，不受迟到数据影响
        self.assertTrue(
            self.rig.calculation.reverify(SUPERVISOR, report_v1)["result_match"]
        )

    def test_rule_rollback_changes_new_reports_only(self) -> None:
        # v2 规则（factor=3）生效后计算
        self.seed_rule(factor=3.0, measure="enrollment_count")
        report_v2 = self.submit_and_run(key="idem-r2")
        self.assertEqual(self.line(report_v2, "enrollment_total")["value"],
                         140.0)  # 10*3 + 20 + 30*3

        self.rig.calibers.rollback(
            SUPERVISOR, f"enrollment_count|DE-DUAL|{TARGET}", to_version=1
        )
        report_back = self.submit_and_run(key="idem-r1")
        self.assertEqual(self.line(report_back, "enrollment_total")["value"],
                         100.0)  # 回到 factor=2

        # 用 v2 规则生成的旧报告不被改写，且仍按固化规则复算一致
        self.assertEqual(self.line(report_v2, "enrollment_total")["value"],
                         140.0)
        check = self.rig.calculation.reverify(SUPERVISOR, report_v2)
        self.assertTrue(check["result_match"])
        self.assertTrue(check["input_match"])


class MissingPolicyTests(RigTestCase):
    def test_fail_policy_fails_task_with_missing_data(self) -> None:
        self.rig.indicators.register(
            SUPERVISOR, code="strict_measure", name="严格指标",
            category="招生", unit="人",
            formula={"type": "sum", "measure": "enrollment_count"},
            missing_policy="fail",
        )
        ev = self.seed_evidence()
        self.seed_import(
            [{"measure": "enrollment_count", "period": "2024-01",
              "caliber": TARGET, "value": 10, "evidence_id": ev}],
            ev,
        )
        self.rig.grant(INST_A.institution_id, permission="calculate")
        task_id = self.rig.calculation.submit(
            INST_A, project_id=PROJECT, window_start="2024-01",
            window_end="2024-03", target_caliber=TARGET,
            idempotency_key="key-fail",
        )["task_id"]
        with self.assertRaises(MissingDataError):
            self.rig.calculation.run(task_id)
        task = self.rig.calculation.get_task(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("缺失", task["error"])

    def test_zero_policy_counts_missing_as_zero(self) -> None:
        self.rig.indicators.register(
            SUPERVISOR, code="zero_measure", name="零填指标",
            category="招生", unit="人",
            formula={"type": "sum", "measure": "enrollment_count"},
            missing_policy="zero",
        )
        ev = self.seed_evidence()
        self.seed_import(
            [{"measure": "enrollment_count", "period": "2024-01",
              "caliber": TARGET, "value": 10, "evidence_id": ev}],
            ev,
        )
        self.rig.grant(INST_A.institution_id, permission="calculate")
        report_id = self.rig.calculation.run(
            self.rig.calculation.submit(
                INST_A, project_id=PROJECT, window_start="2024-01",
                window_end="2024-03", target_caliber=TARGET,
                idempotency_key="key-zero",
            )["task_id"]
        )["report_id"]
        report = self.rig.calculation.get_report(SUPERVISOR, report_id)
        line = report["lines"][0]
        self.assertEqual(line["value"], 10.0)
        self.assertEqual(line["missing_periods"], ["2024-02", "2024-03"])


class AccessTests(SeededCase):
    def test_calculate_requires_grant(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.rig.calculation.submit(
                INST_B, project_id=PROJECT, window_start=WINDOW[0],
                window_end=WINDOW[1], target_caliber=TARGET,
                idempotency_key="key-denied",
            )

    def test_report_lines_filtered_by_granted_category(self) -> None:
        report_id = self.submit_and_run()
        # INST_A 仅有 "*" 类别 view 授权（setUp 中授予），改为按类别验证：
        rig = self.rig
        rig.grant(INST_B.institution_id, category="招生", permission="view")
        view = rig.calculation.get_report(INST_B, report_id)
        self.assertEqual([l["code"] for l in view["lines"]],
                         ["enrollment_total"])
        self.assertEqual(sorted(view["redacted_categories"]),
                         ["就业", "师资培养"])

        full = rig.calculation.get_report(SUPERVISOR, report_id)
        self.assertEqual(len(full["lines"]), 3)
        self.assertEqual(full["redacted_categories"], [])


if __name__ == "__main__":
    unittest.main()
