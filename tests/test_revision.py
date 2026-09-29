"""已签报告修订：更正只生成新修订、旧签发内容与理由可同时读取、版本冲突。"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from service_09252_010.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from service_09252_010.persistence.database import Database
from service_09252_010.persistence.store import Store
from support import INST_A, INST_B, SUPERVISOR, RigTestCase
from test_calculation import SeededCase


def corrected_lines(current: list[dict], code: str, value: float) -> list[dict]:
    """基于当前签发行复制并修改单个指标值（更正不改指标集合）。"""
    return [
        {**line, "value": value} if line["code"] == code else dict(line)
        for line in current
    ]


class RevisionFlowTests(SeededCase):
    def setUp(self) -> None:
        super().setUp()
        self.rig.grant(INST_A.institution_id, permission="review")
        self.rig.grant(INST_A.institution_id, permission="export")
        self.report_id = self.submit_and_run()
        # 主管单位复核通过 = 首次签发（v1）
        self.rig.review.review(SUPERVISOR, self.report_id, approve=True,
                               reason="复核通过")
        self.signed = self.rig.calculation.get_report(SUPERVISOR, self.report_id)
        self.assertEqual(self.signed["current_revision_no"], 1)

    def _request(self, expected: int = 1, value: float = 120.0,
                 principal=INST_A, reason: str = "源数据口径更正"):
        lines = corrected_lines(self.signed["lines"], "enrollment_total", value)
        return self.rig.revisions.request_correction(
            principal, self.report_id, expected_version=expected,
            lines=lines, reason=reason,
        )

    def test_signed_report_keeps_v1_history(self) -> None:
        history = self.rig.revisions.history(SUPERVISOR, self.report_id)
        self.assertEqual([v["revision_no"] for v in history["versions"]], [1])
        v1 = history["versions"][0]
        self.assertEqual(v1["change_reason"], "首次签发")
        # 旧版读取：直接按版本号取，内容与首次签发一致
        old = self.rig.revisions.history(SUPERVISOR, self.report_id, 1)
        self.assertEqual(
            [l["value"] for l in old["version"]["lines"]
             if l["code"] == "enrollment_total"],
            [100.0],
        )
        self.assertEqual(old["version"]["result_fingerprint"],
                         self.signed["result_fingerprint"])

    def test_correction_creates_new_version_without_overwriting(self) -> None:
        request = self._request(value=120.0)
        decision = self.rig.revisions.decide(
            SUPERVISOR, request["revision_id"], approve=True,
            reason="证据充分，批准更正",
        )
        self.assertEqual(decision["status"], "approved")
        self.assertEqual(decision["current_revision_no"], 2)

        # 当前报告内容已是更正后的 v2
        current = self.rig.calculation.get_report(SUPERVISOR, self.report_id)
        self.assertEqual(current["current_revision_no"], 2)
        self.assertTrue(current["revised"])
        self.assertEqual(
            [l["value"] for l in current["lines"]
             if l["code"] == "enrollment_total"],
            [120.0],
        )

        # 读者同时看到旧签发内容（v1）与更正理由（v2）
        history = self.rig.revisions.history(SUPERVISOR, self.report_id)
        self.assertEqual([v["revision_no"] for v in history["versions"]],
                         [1, 2])
        v1, v2 = history["versions"]
        self.assertEqual(
            [l["value"] for l in v1["lines"] if l["code"] == "enrollment_total"],
            [100.0],
        )
        self.assertEqual(v2["change_reason"], "源数据口径更正")
        self.assertEqual(
            [l["value"] for l in v2["lines"] if l["code"] == "enrollment_total"],
            [120.0],
        )
        self.assertNotEqual(v1["result_fingerprint"], v2["result_fingerprint"])

        # 旧版仍可单独读取；原始固化指纹未被改写，复算仍一致
        old = self.rig.revisions.history(SUPERVISOR, self.report_id, 1)
        self.assertEqual(old["current_revision_no"], 2)
        check = self.rig.calculation.reverify(SUPERVISOR, self.report_id)
        self.assertTrue(check["result_match"])

    def test_request_with_stale_version_gets_conflict(self) -> None:
        # 第一次更正批准，当前版本推进到 v2
        first = self._request(value=120.0)
        self.rig.revisions.decide(SUPERVISOR, first["revision_id"], approve=True)

        # 仍基于 v1 提交：必须得到清晰冲突响应，而不是静默覆盖
        with self.assertRaises(ConflictError) as cm:
            self._request(expected=1, value=130.0)
        self.assertEqual(cm.exception.detail, {"expected": 1, "current": 2})
        self.assertIn("v1", cm.exception.message)

    def test_approving_stale_request_after_another_correction_conflicts(self) -> None:
        first = self._request(expected=1, value=120.0)
        # 同一报告不能并存两个待审批申请
        with self.assertRaises(StateError):
            self._request(expected=1, value=125.0)
        self.rig.revisions.decide(SUPERVISOR, first["revision_id"], approve=True)

        # 直接构造一条基于 v1 的迟到申请（模拟版本前移后才送达的旧申请）
        stale_lines = corrected_lines(self.signed["lines"],
                                      "enrollment_total", 130.0)
        with self.rig.db.uow() as uow:
            from service_09252_010.domain.models import ReportRevision, RevisionStatus
            store = Store(uow.conn)
            store.add_report_revision(ReportRevision(
                id="revision-stale", report_id=self.report_id,
                base_revision_no=1, lines=stale_lines,
                correction_reason="迟到的旧申请",
                status=RevisionStatus.PENDING, requested_by=INST_A.institution_id,
                requested_at="2026-01-02T00:00:00+00:00",
            ))
        with self.assertRaises(ConflictError) as cm:
            self.rig.revisions.decide(SUPERVISOR, "revision-stale",
                                      approve=True)
        self.assertEqual(cm.exception.detail, {"expected": 1, "current": 2})
        # 冲突后当前内容不被污染
        history = self.rig.revisions.history(SUPERVISOR, self.report_id)
        self.assertEqual([v["revision_no"] for v in history["versions"]],
                         [1, 2])

    def test_decide_expected_version_guard(self) -> None:
        request = self._request(value=120.0)
        # 审批人携带的期望版本过期：冲突
        with self.assertRaises(ConflictError):
            self.rig.revisions.decide(
                SUPERVISOR, request["revision_id"], approve=True,
                expected_version=9,
            )

    def test_approver_must_not_be_applicant(self) -> None:
        request = self._request(principal=INST_A)
        with self.assertRaises(PermissionDeniedError):
            self.rig.revisions.decide(
                INST_A, request["revision_id"], approve=True,
            )

    def test_reject_does_not_create_version(self) -> None:
        request = self._request(value=120.0)
        result = self.rig.revisions.decide(
            SUPERVISOR, request["revision_id"], approve=False, reason="理由不足",
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["current_revision_no"], 1)
        history = self.rig.revisions.history(SUPERVISOR, self.report_id)
        self.assertEqual([v["revision_no"] for v in history["versions"]], [1])
        # 驳回后可重新申请
        again = self._request(expected=1, value=121.0)
        self.assertTrue(again["revision_id"])
        # 已结案的申请不可重复审批
        with self.assertRaises(StateError):
            self.rig.revisions.decide(
                SUPERVISOR, request["revision_id"], approve=True,
            )

    def test_correction_only_on_signed_report(self) -> None:
        # 另一份未签发报告不能申请更正
        report_id = self.submit_and_run(key="idem-unsigned")
        lines = self.rig.calculation.get_report(SUPERVISOR, report_id)["lines"]
        with self.assertRaises(StateError):
            self.rig.revisions.request_correction(
                INST_A, report_id, expected_version=1,
                lines=corrected_lines(lines, "enrollment_total", 1.0),
                reason="x",
            )

    def test_validation_requirements(self) -> None:
        good = corrected_lines(self.signed["lines"], "enrollment_total", 120.0)
        with self.assertRaises(ValidationError):
            self.rig.revisions.request_correction(
                INST_A, self.report_id, expected_version=1, lines=good,
                reason="  ",
            )
        with self.assertRaises(ValidationError):
            self.rig.revisions.request_correction(
                INST_A, self.report_id, expected_version=1, lines=[],
                reason="理由",
            )
        # 指标集合不一致（缺行）拒绝
        with self.assertRaises(ValidationError):
            self.rig.revisions.request_correction(
                INST_A, self.report_id, expected_version=1,
                lines=[l for l in good if l["code"] != "employment_rate"],
                reason="理由",
            )

    def test_request_requires_grant(self) -> None:
        lines = corrected_lines(self.signed["lines"], "enrollment_total", 120.0)
        with self.assertRaises(PermissionDeniedError):
            self.rig.revisions.request_correction(
                INST_B, self.report_id, expected_version=1,
                lines=lines, reason="理由",
            )

    def test_unknown_old_version_404(self) -> None:
        with self.assertRaises(NotFoundError):
            self.rig.revisions.history(SUPERVISOR, self.report_id, 99)

    def test_export_uses_current_version_and_carries_chain(self) -> None:
        request = self._request(value=120.0)
        self.rig.revisions.decide(SUPERVISOR, request["revision_id"], approve=True)
        exported = self.rig.exports.export(INST_A, self.report_id)
        doc = exported["document"]
        self.assertEqual(doc["current_revision_no"], 2)
        self.assertEqual(
            [l["value"] for l in doc["lines"]
             if l["code"] == "enrollment_total"],
            [120.0],
        )
        self.assertEqual([h["revision_no"] for h in doc["revision_history"]],
                         [1, 2])
        self.assertEqual(doc["revision_history"][1]["change_reason"],
                         "源数据口径更正")


class LegacySchemaMigrationTests(unittest.TestCase):
    """旧库（无修订表/版本指针）连接后自动补签发历史 v1。"""

    def test_legacy_reviewed_report_seeded_as_revision_v1(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="svc09252-legacy-")
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "legacy.db")
        # 按修订功能上线前的最小旧模式造一份已签发报告
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE reports (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    window_start TEXT NOT NULL, window_end TEXT NOT NULL,
                    target_caliber TEXT NOT NULL, data_version_no INTEGER NOT NULL,
                    pins_json TEXT NOT NULL, lines_json TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL, result_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE report_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL,
                    event TEXT NOT NULL, actor TEXT NOT NULL,
                    reason TEXT, at TEXT NOT NULL
                );
                """
            )
            lines = [{"code": "enrollment_total", "category": "招生",
                      "value": 100.0}]
            conn.execute(
                "INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r-old", "P1", "2024-01", "2024-12", "CN-STD", 1,
                 json.dumps({"data_version": 1}), json.dumps(lines),
                 "in-fp", "old-fp", "reviewed", "机构A",
                 "2026-01-01T00:00:00+00:00", "task-old"),
            )

        db = Database(db_path)  # 连接即迁移
        with db.read() as conn:
            store = Store(conn)
            report = store.get_report("r-old")
            self.assertEqual(report.current_revision_no, 1)
            v1 = store.get_revision_history("r-old", 1)
        self.assertIsNotNone(v1)
        self.assertEqual(v1["result_fingerprint"], "old-fp")
        self.assertEqual(v1["change_reason"], "首次签发")

        # 迁移后的旧签发报告可正常发起并批准更正
        from service_09252_010.domain.models import Principal
        from service_09252_010.services.revision import RevisionService
        from support import FixedClock, SeqIds

        service = RevisionService(db, FixedClock(), SeqIds())
        new_lines = [{**l, "value": 108.0} for l in v1["lines"]]
        req = service.request_correction(
            SUPERVISOR, "r-old", expected_version=1, lines=new_lines,
            reason="旧库报告更正",
        )
        approver = Principal(institution_id="复核委员会", role="supervisor")
        decision = service.decide(approver, req["revision_id"], approve=True)
        self.assertEqual(decision["current_revision_no"], 2)
        history = service.history(SUPERVISOR, "r-old")
        self.assertEqual([v["revision_no"] for v in history["versions"]],
                         [1, 2])
        self.assertEqual(history["versions"][0]["change_reason"], "首次签发")
        self.assertEqual(history["versions"][1]["change_reason"], "旧库报告更正")
