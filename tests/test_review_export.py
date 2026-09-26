"""复核与导出：独立性约束、状态机、授权粒度与导出留痕。"""
from __future__ import annotations

import json
import unittest

from service_09252_010.domain.errors import (
    PermissionDeniedError,
    StateError,
)
from support import INST_A, INST_B, PROJECT, SUPERVISOR, RigTestCase, TARGET
from test_calculation import SeededCase, WINDOW


class ReviewExportTests(SeededCase):
    def setUp(self) -> None:
        super().setUp()
        self.rig.grant(INST_A.institution_id, permission="export")
        self.report_id = self.submit_and_run()

    def test_export_before_review_rejected(self) -> None:
        with self.assertRaises(StateError):
            self.rig.exports.export(INST_A, self.report_id)

    def test_reviewer_must_differ_from_calculator(self) -> None:
        self.rig.grant(INST_A.institution_id, permission="review")
        with self.assertRaises(PermissionDeniedError):
            self.rig.review.review(INST_A, self.report_id, approve=True)

    def test_review_requires_grant(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.rig.review.review(INST_B, self.report_id, approve=True)

    def test_approve_then_export_with_basis(self) -> None:
        result = self.rig.review.review(SUPERVISOR, self.report_id,
                                        approve=True, reason="复核通过")
        self.assertEqual(result["status"], "reviewed")

        exported = self.rig.exports.export(INST_A, self.report_id)
        doc = exported["document"]
        self.assertEqual(doc["status"], "reviewed")
        self.assertEqual(len(doc["lines"]), 3)
        # 换算依据与会签记录随报告导出
        self.assertEqual(len(doc["conversion_basis"]), 1)
        basis = doc["conversion_basis"][0]
        self.assertEqual(basis["rule_key"],
                         f"enrollment_count|DE-DUAL|{TARGET}")
        self.assertEqual(sorted(basis["signatures"]),
                         ["expert_panel", "partner_institution", "supervisor"])
        self.assertIn("x * 2.0", basis["formula"])
        # 证据来源清单
        self.assertEqual(len(doc["evidence"]), 1)
        self.assertEqual(doc["evidence"][0]["evidence_id"], self.evidence)
        # 重复导出内容可复算（摘要一致），且每次导出留痕
        again = self.rig.exports.export(INST_A, self.report_id)
        self.assertEqual(exported["digest"], again["digest"])
        self.assertEqual(len(again["export_history"]), 2)
        # 导出文本可独立解析
        parsed = json.loads(exported["canonical_document"])
        self.assertEqual(parsed["report_id"], self.report_id)

    def test_reject_is_terminal(self) -> None:
        self.rig.review.review(SUPERVISOR, self.report_id, approve=False,
                               reason="数据存疑")
        with self.assertRaises(StateError):
            self.rig.exports.export(INST_A, self.report_id)
        with self.assertRaises(StateError):
            self.rig.review.review(SUPERVISOR, self.report_id, approve=True)

    def test_double_review_rejected(self) -> None:
        self.rig.review.review(SUPERVISOR, self.report_id, approve=True)
        with self.assertRaises(StateError):
            self.rig.review.review(SUPERVISOR, self.report_id, approve=True)

    def test_export_requires_grant(self) -> None:
        self.rig.review.review(SUPERVISOR, self.report_id, approve=True)
        with self.assertRaises(PermissionDeniedError):
            self.rig.exports.export(INST_B, self.report_id)

    def test_report_events_recorded(self) -> None:
        self.rig.review.review(SUPERVISOR, self.report_id, approve=True,
                               reason="通过")
        report = self.rig.calculation.get_report(SUPERVISOR, self.report_id)
        kinds = [e["event"] for e in report["events"]]
        self.assertEqual(kinds, ["computed", "reviewed"])


if __name__ == "__main__":
    unittest.main()
