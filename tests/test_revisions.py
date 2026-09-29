"""已签报告修订：新修订签发、旧版读取、版本冲突与留痕。"""
from __future__ import annotations

import unittest

from service_09252_010.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from support import INST_A, INST_B, SUPERVISOR, TARGET
from test_calculation import SeededCase


class RevisionCase(SeededCase):
    """预置已签发报告，并补一批迟到数据形成新数据版本。"""

    def setUp(self) -> None:
        super().setUp()
        self.rig.grant(INST_A.institution_id, permission="review")
        self.report_id = self.submit_and_run()
        self.rig.review.review(SUPERVISOR, self.report_id, approve=True,
                               reason="复核通过")
        # 迟到数据：2024-02 的缺失值被补齐（原数据版本 v1 → v2）
        self.seed_import(
            [{"measure": "enrollment_count", "period": "2024-02",
              "caliber": TARGET, "value": 5, "evidence_id": self.evidence}],
            self.evidence, reason="迟到数据补录",
        )

    def enrollment_value(self, version: int) -> float | None:
        entry = self.rig.revisions.get_version(
            SUPERVISOR, self.report_id, version
        )
        line = next(l for l in entry["lines"] if l["code"] == "enrollment_total")
        return line["value"]


class RevisionFlowTests(RevisionCase):
    def test_correction_creates_new_revision_and_preserves_old(self) -> None:
        # 更正申请 → 生成 proposed 修订，当前内容不变
        proposal = self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="迟到数据补录后重算"
        )
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(proposal["version"], 1)
        self.assertEqual(proposal["correction_reason"], "迟到数据补录后重算")
        self.assertEqual(proposal["base_version"], 0)
        self.assertEqual(proposal["pins"]["data_version"], 2)
        current = self.rig.calculation.get_report(SUPERVISOR, self.report_id)
        self.assertEqual(current["revision_version"], 0)
        self.assertEqual(current["data_version_no"], 1)

        # 签发 → 当前内容更新为新修订，旧版仍可读取
        issued = self.rig.revisions.issue_revision(
            SUPERVISOR, self.report_id, 1
        )
        self.assertEqual(issued["status"], "issued")
        self.assertTrue(issued["current"])
        current = self.rig.calculation.get_report(SUPERVISOR, self.report_id)
        self.assertEqual(current["revision_version"], 1)
        self.assertEqual(current["data_version_no"], 2)

        # 读者同时看到旧签发内容（100）与新内容（105）及更正理由
        self.assertEqual(self.enrollment_value(0), 100.0)
        self.assertEqual(self.enrollment_value(1), 105.0)
        # 修订后的当前报告仍可按固化版本复算核对
        check = self.rig.calculation.reverify(SUPERVISOR, self.report_id)
        self.assertTrue(check["result_match"])
        self.assertTrue(check["input_match"])
        history = self.rig.revisions.list_revisions(SUPERVISOR, self.report_id)
        self.assertEqual(history["current_version"], 1)
        versions = [h["version"] for h in history["history"]]
        self.assertEqual(versions, [0, 1])
        self.assertIsNone(history["history"][0]["correction_reason"])
        self.assertEqual(history["history"][1]["correction_reason"],
                         "迟到数据补录后重算")
        events = [e["event"] for e in history["events"]]
        self.assertEqual(events, ["computed", "reviewed",
                                  "revision_requested", "revision_issued"])

    def test_old_version_survives_multiple_corrections(self) -> None:
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="第一次更正"
        )
        self.rig.revisions.issue_revision(SUPERVISOR, self.report_id, 1)
        # 再次迟到数据 → 第二次更正
        self.seed_import(
            [{"measure": "trained_teacher_count", "period": "2023-11",
              "caliber": TARGET, "value": 3, "evidence_id": self.evidence}],
            self.evidence, reason="第二次补录",
        )
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="第二次更正"
        )
        self.rig.revisions.issue_revision(SUPERVISOR, self.report_id, 2)
        # 三个签发版本全部可读
        self.assertEqual(self.enrollment_value(0), 100.0)
        self.assertEqual(self.enrollment_value(1), 105.0)
        history = self.rig.revisions.list_revisions(SUPERVISOR, self.report_id)
        self.assertEqual([h["version"] for h in history["history"]],
                         [0, 1, 2])
        self.assertEqual(history["current_version"], 2)

    def test_reverify_after_new_indicator_registered(self) -> None:
        # 旧报告签发后又登记了新指标：按固化版本复算不应受影响
        self.rig.indicators.register(
            SUPERVISOR, code="new_metric", name="新指标", category="招生",
            unit="人",
            formula={"type": "sum", "measure": "new_measure"},
            missing_policy="skip",
        )
        check = self.rig.calculation.reverify(SUPERVISOR, self.report_id)
        self.assertTrue(check["result_match"])
        self.assertTrue(check["input_match"])

    def test_unissued_proposal_not_visible_as_version(self) -> None:
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="待签发"
        )
        with self.assertRaises(NotFoundError):
            self.rig.revisions.get_version(SUPERVISOR, self.report_id, 1)
        # 但签发历史列表中可见 proposed 状态
        history = self.rig.revisions.list_revisions(SUPERVISOR, self.report_id)
        self.assertEqual(history["history"][1]["status"], "proposed")
        self.assertEqual(history["current_version"], 0)


class RevisionConflictTests(RevisionCase):
    def test_stale_expected_version_conflicts(self) -> None:
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="第一次更正"
        )
        self.rig.revisions.issue_revision(SUPERVISOR, self.report_id, 1)
        # 基于旧版本号再申请 → 清晰冲突
        with self.assertRaises(ConflictError) as ctx:
            self.rig.revisions.request_revision(
                INST_A, self.report_id, reason="基于旧版",
                expected_version=0,
            )
        self.assertEqual(ctx.exception.detail["expected_version"], 0)
        self.assertEqual(ctx.exception.detail["current_version"], 1)

    def test_issue_with_stale_expected_version_conflicts(self) -> None:
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="第一次更正"
        )
        self.rig.revisions.issue_revision(SUPERVISOR, self.report_id, 1)
        # 新数据到来后才能再次提出有差异的更正申请
        self.seed_import(
            [{"measure": "trained_teacher_count", "period": "2023-11",
              "caliber": TARGET, "value": 3, "evidence_id": self.evidence}],
            self.evidence, reason="再次补录",
        )
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="第二次更正"
        )
        with self.assertRaises(ConflictError):
            self.rig.revisions.issue_revision(
                SUPERVISOR, self.report_id, 2, expected_version=0
            )

    def test_superseded_proposal_conflicts(self) -> None:
        # 两个申请基于同一版本，先签发的生效，后签发的被超越
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="申请甲"
        )
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="申请乙"
        )
        self.rig.revisions.issue_revision(SUPERVISOR, self.report_id, 2)
        with self.assertRaises(ConflictError):
            self.rig.revisions.issue_revision(SUPERVISOR, self.report_id, 1)

    def test_double_issue_rejected(self) -> None:
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="更正"
        )
        self.rig.revisions.issue_revision(SUPERVISOR, self.report_id, 1)
        with self.assertRaises(StateError):
            self.rig.revisions.issue_revision(SUPERVISOR, self.report_id, 1)


class RevisionGuardTests(RevisionCase):
    def test_unsigned_report_cannot_be_corrected(self) -> None:
        unsigned = self.submit_and_run(key="idem-2")
        with self.assertRaises(StateError):
            self.rig.revisions.request_revision(
                INST_A, unsigned, reason="尚未签发"
            )

    def test_noop_correction_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.rig.revisions.request_revision(
                INST_A, self.report_id, reason="内容无变化",
                pins=self.rig.calculation.get_report(
                    SUPERVISOR, self.report_id)["pins"],
            )

    def test_reason_required(self) -> None:
        with self.assertRaises(ValidationError):
            self.rig.revisions.request_revision(
                INST_A, self.report_id, reason="  "
            )

    def test_request_requires_calculate_grant(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.rig.revisions.request_revision(
                INST_B, self.report_id, reason="未授权机构"
            )

    def test_issue_requires_review_grant(self) -> None:
        self.rig.revisions.request_revision(
            INST_A, self.report_id, reason="更正"
        )
        with self.assertRaises(PermissionDeniedError):
            self.rig.revisions.issue_revision(INST_B, self.report_id, 1)

    def test_history_requires_view_grant(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.rig.revisions.list_revisions(INST_B, self.report_id)

    def test_missing_revision_404(self) -> None:
        with self.assertRaises(NotFoundError):
            self.rig.revisions.issue_revision(SUPERVISOR, self.report_id, 9)


if __name__ == "__main__":
    unittest.main()
