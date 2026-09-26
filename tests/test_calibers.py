"""口径会签：多方签署生效、重复签署拦截、并发签发恰好生效一次、规则回滚。"""
from __future__ import annotations

import threading
import unittest

from service_09252_010.domain.errors import (
    ConflictError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from service_09252_010.domain.models import Principal
from support import INST_A, PROJECT, SUPERVISOR, RigTestCase, TARGET


class CaliberSignTests(RigTestCase):
    def _create(self, factor: float = 1.0, offset: float = 0.0) -> str:
        return self.rig.calibers.create_rule(
            SUPERVISOR, measure="enrollment_count", from_caliber="DE-DUAL",
            to_caliber=TARGET, factor=factor, offset=offset,
        )["rule_id"]

    def test_quorum_activates_rule(self) -> None:
        rule_id = self._create(factor=2.0)
        first = self.rig.calibers.sign(SUPERVISOR, rule_id, signatory="supervisor")
        self.assertFalse(first["activated"])
        self.rig.calibers.sign(SUPERVISOR, rule_id, signatory="expert_panel")
        last = self.rig.calibers.sign(
            SUPERVISOR, rule_id, signatory="partner_institution"
        )
        self.assertTrue(last["activated"])
        rule = self.rig.calibers.get_rule(rule_id)
        self.assertEqual(rule["status"], "active")
        self.assertTrue(rule["is_active"])
        self.assertEqual(sorted(rule["signatures"]),
                         ["expert_panel", "partner_institution", "supervisor"])

    def test_duplicate_signature_conflict(self) -> None:
        rule_id = self._create()
        self.rig.calibers.sign(SUPERVISOR, rule_id, signatory="supervisor")
        with self.assertRaises(ConflictError):
            self.rig.calibers.sign(SUPERVISOR, rule_id, signatory="supervisor")

    def test_unknown_signatory_rejected(self) -> None:
        rule_id = self._create()
        with self.assertRaises(ValidationError):
            self.rig.calibers.sign(SUPERVISOR, rule_id, signatory="stranger")

    def test_sign_after_activation_rejected(self) -> None:
        rule_id = self._create()
        for s in ("supervisor", "partner_institution", "expert_panel"):
            self.rig.calibers.sign(SUPERVISOR, rule_id, signatory=s)
        with self.assertRaises(StateError):
            self.rig.calibers.sign(SUPERVISOR, rule_id, signatory="supervisor")

    def test_concurrent_signing_activates_exactly_once(self) -> None:
        """三方同时会签：全部记录、恰好生效一次、无重复激活。"""
        rule_id = self._create(factor=1.5)
        barrier = threading.Barrier(3)
        outcomes: list[dict] = []
        errors: list[Exception] = []

        def sign(signatory: str) -> None:
            try:
                barrier.wait(timeout=10)
                outcomes.append(
                    self.rig.calibers.sign(SUPERVISOR, rule_id,
                                           signatory=signatory)
                )
            except Exception as exc:  # noqa: BLE001 — 收集后统一断言
                errors.append(exc)

        threads = [
            threading.Thread(target=sign, args=(s,))
            for s in ("supervisor", "partner_institution", "expert_panel")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 3)
        self.assertEqual(sum(1 for o in outcomes if o["activated"]), 1)
        rule = self.rig.calibers.get_rule(rule_id)
        self.assertEqual(rule["status"], "active")
        self.assertEqual(len(rule["signatures"]), 3)

    def test_concurrent_same_signatory_one_conflicts(self) -> None:
        rule_id = self._create()
        barrier = threading.Barrier(2)
        results: list[object] = []

        def sign() -> None:
            barrier.wait(timeout=10)
            try:
                results.append(
                    self.rig.calibers.sign(SUPERVISOR, rule_id,
                                           signatory="supervisor")
                )
            except ConflictError as exc:
                results.append(exc)

        threads = [threading.Thread(target=sign) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(sum(isinstance(r, ConflictError) for r in results), 1)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)

    def test_create_rule_requires_supervisor(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.rig.calibers.create_rule(
                INST_A, measure="m", from_caliber="A", to_caliber=TARGET,
                factor=1.0,
            )


class RuleRollbackTests(RigTestCase):
    def _activate(self, factor: float) -> str:
        rule_id = self.rig.calibers.create_rule(
            SUPERVISOR, measure="enrollment_count", from_caliber="DE-DUAL",
            to_caliber=TARGET, factor=factor,
        )["rule_id"]
        for s in ("supervisor", "partner_institution", "expert_panel"):
            self.rig.calibers.sign(SUPERVISOR, rule_id, signatory=s)
        return rule_id

    def test_new_version_supersedes_and_rollback_restores(self) -> None:
        v1 = self._activate(factor=2.0)
        v2 = self._activate(factor=3.0)
        rules = {r["rule_id"]: r for r in self.rig.calibers.list_rules()}
        self.assertEqual(rules[v1]["status"], "superseded")
        self.assertEqual(rules[v2]["status"], "active")
        self.assertTrue(rules[v2]["is_active"])

        result = self.rig.calibers.rollback(
            SUPERVISOR, "enrollment_count|DE-DUAL|" + TARGET, to_version=1
        )
        self.assertEqual(result["active_version"], 1)
        rules = {r["rule_id"]: r for r in self.rig.calibers.list_rules()}
        self.assertEqual(rules[v1]["status"], "active")
        self.assertTrue(rules[v1]["is_active"])
        self.assertEqual(rules[v2]["status"], "rolled_back")

    def test_rollback_to_non_superseded_rejected(self) -> None:
        v1 = self._activate(factor=2.0)
        with self.assertRaises(StateError):
            self.rig.calibers.rollback(
                SUPERVISOR, "enrollment_count|DE-DUAL|" + TARGET, to_version=1
            )

    def test_rollback_unknown_version(self) -> None:
        self._activate(factor=2.0)
        from service_09252_010.domain.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.rig.calibers.rollback(
                SUPERVISOR, "enrollment_count|DE-DUAL|" + TARGET, to_version=9
            )


if __name__ == "__main__":
    unittest.main()
