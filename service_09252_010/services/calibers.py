"""口径会签服务：换算规则登记、多方会签、生效与回滚。

- 规则按版本登记，集齐全部指定签署方后生效；
- 生效通过 rule_key_state 指针实现，并发签署在事务内串行，恰好生效一次；
- 回滚把指针拨回历史版本，已被报告固化的规则版本不受影响。
"""
from __future__ import annotations

import sqlite3

from ..domain.conversion import rule_key
from ..domain.errors import ConflictError, NotFoundError, StateError, ValidationError
from ..domain.models import ConversionRule, Principal, RuleStatus
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator

DEFAULT_SIGNATORIES = ("supervisor", "partner_institution", "expert_panel")


class CaliberService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    def create_rule(self, principal: Principal, *, measure: str,
                    from_caliber: str, to_caliber: str, factor: float,
                    offset: float = 0.0,
                    required_signatories: list[str] | None = None) -> dict:
        """登记换算规则新版本（会签中状态）。"""
        if not principal.is_supervisor:
            from ..domain.errors import PermissionDeniedError

            raise PermissionDeniedError("仅主管单位可登记换算规则")
        if from_caliber == to_caliber:
            raise ValidationError("源口径与目标口径相同，无需换算规则")
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            raise ValidationError("factor 必须为数值")
        signatories = tuple(required_signatories or DEFAULT_SIGNATORIES)
        if not signatories or len(set(signatories)) != len(signatories):
            raise ValidationError("会签方列表为空或存在重复")
        key = rule_key(measure, from_caliber, to_caliber)
        with self.db.uow() as uow:
            store = Store(uow.conn)
            version_no = store.next_rule_version(key)
            rule = ConversionRule(
                id=self.ids.new_id("rule"),
                rule_key=key,
                version_no=version_no,
                measure=measure,
                from_caliber=from_caliber,
                to_caliber=to_caliber,
                factor=float(factor),
                offset=float(offset),
                status=RuleStatus.PENDING,
                required_signatories=signatories,
                created_by=principal.institution_id,
                created_at=self.clock.now(),
            )
            store.add_rule(rule)
        return {"rule_id": rule.id, "rule_key": key, "version_no": version_no}

    def sign(self, principal: Principal, rule_id: str, *, signatory: str) -> dict:
        """会签。集齐签署方后规则生效并取代旧生效版本。

        并发安全：整个签署+生效在同一写事务内完成；重复签署由主键约束拦截。
        """
        with self.db.uow() as uow:
            store = Store(uow.conn)
            rule = store.get_rule(rule_id)
            if rule is None:
                raise NotFoundError(f"换算规则不存在: {rule_id}")
            if rule.status is not RuleStatus.PENDING:
                raise StateError(f"规则当前状态为 {rule.status.value}，不可签署")
            if signatory not in rule.required_signatories:
                raise ValidationError(f"签署方 {signatory!r} 不在会签名单中")
            try:
                store.add_signature(rule_id, signatory, self.clock.now())
            except sqlite3.IntegrityError:
                raise ConflictError(f"签署方 {signatory!r} 已签署过该规则") from None

            signed = set(store.list_signatures(rule_id))
            activated = False
            if set(rule.required_signatories) <= signed:
                previous_id = store.get_active_rule_id(rule.rule_key)
                if previous_id is not None:
                    store.set_rule_status(previous_id, RuleStatus.SUPERSEDED)
                store.set_rule_status(rule_id, RuleStatus.ACTIVE)
                store.set_active_rule_id(rule.rule_key, rule_id)
                activated = True
        return {"rule_id": rule_id, "signed": sorted(signed), "activated": activated}

    def rollback(self, principal: Principal, rule_key_str: str, *,
                 to_version: int) -> dict:
        """把规则键的生效指针回滚到历史版本。当前生效版本标记为 rolled_back。"""
        if not principal.is_supervisor:
            from ..domain.errors import PermissionDeniedError

            raise PermissionDeniedError("仅主管单位可回滚换算规则")
        with self.db.uow() as uow:
            store = Store(uow.conn)
            target = store.get_rule_by_version(rule_key_str, to_version)
            if target is None:
                raise NotFoundError(
                    f"规则 {rule_key_str} 不存在版本 v{to_version}"
                )
            if target.status not in (RuleStatus.SUPERSEDED, RuleStatus.ROLLED_BACK):
                raise StateError(
                    f"仅可回滚到曾被取代的历史版本，当前状态 {target.status.value}"
                )
            current_id = store.get_active_rule_id(rule_key_str)
            if current_id is None:
                raise StateError(f"规则键 {rule_key_str} 当前无生效版本")
            if current_id == target.id:
                raise StateError("目标版本已是生效版本")
            store.set_rule_status(current_id, RuleStatus.ROLLED_BACK)
            store.set_rule_status(target.id, RuleStatus.ACTIVE)
            store.set_active_rule_id(rule_key_str, target.id)
        return {"rule_key": rule_key_str, "active_version": to_version}

    def get_rule(self, rule_id: str) -> dict:
        with self.db.read() as conn:
            store = Store(conn)
            rule = store.get_rule(rule_id)
            if rule is None:
                raise NotFoundError(f"换算规则不存在: {rule_id}")
            signatures = store.list_signatures(rule_id)
            active_id = store.get_active_rule_id(rule.rule_key)
        return {
            "rule_id": rule.id,
            "rule_key": rule.rule_key,
            "version_no": rule.version_no,
            "factor": rule.factor,
            "offset": rule.offset,
            "status": rule.status.value,
            "required_signatories": list(rule.required_signatories),
            "signatures": signatures,
            "is_active": active_id == rule.id,
        }

    def list_rules(self) -> list[dict]:
        with self.db.read() as conn:
            store = Store(conn)
            rules = store.list_rules()
            active_ids = {r.rule_key: store.get_active_rule_id(r.rule_key)
                          for r in rules}
        return [
            {
                "rule_id": r.id,
                "rule_key": r.rule_key,
                "version_no": r.version_no,
                "status": r.status.value,
                "factor": r.factor,
                "offset": r.offset,
                "is_active": active_ids.get(r.rule_key) == r.id,
            }
            for r in rules
        ]
