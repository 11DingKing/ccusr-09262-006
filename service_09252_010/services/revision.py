"""已签报告修订服务。

业务对象为“已签报告修订”，核心约束：

- 已签发（复核通过）报告的更正**绝不原地改写**：每次更正都是一条新修订，
  批准后签发版本号 +1，旧版本完整行快照连同更正理由永久保留在
  report_revision_history 中，读者可同时看到旧签发内容与更正理由；
- 修订申请与批准均做 Python 层版本校验（乐观锁）：申请须携带所基于的
  expected_version，批准时再次比对当前签发版本；若期间已有别的更正生效，
  返回明确的 409 冲突响应（detail 给出 base/current），旧申请不会覆盖新版本；
- 审批独立性：批准/驳回人不得是更正申请人。
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

from ..domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from ..domain.fingerprint import fingerprint
from ..domain.models import (
    Principal,
    Report,
    ReportRevision,
    ReportStatus,
    RevisionStatus,
)
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator
from .access import AccessPolicy


class RevisionService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    # ---- 更正申请 ----
    def request_correction(self, principal: Principal, report_id: str, *,
                           expected_version: int, lines: list[dict],
                           reason: str) -> dict:
        """对已签发报告提交更正申请（不立即生效，须独立审批）。"""
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version 必须为正整数签发版本号")
        if not isinstance(lines, list) or not lines:
            raise ValidationError("更正内容 lines 必须为非空列表")
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("更正申请必须给出 correction_reason")
        self._validate_lines(lines)

        with self.db.uow() as uow:
            store = Store(uow.conn)
            report = self._load_signed(store, report_id)
            AccessPolicy(store).require(
                principal, report.project_id, "*", "calculate"
            )
            # Python 乐观锁：申请基于的版本必须仍是当前签发版本
            self._check_version(
                expected_version, report.current_revision_no, stage="申请"
            )
            if store.pending_report_revision(report_id) is not None:
                raise StateError("该报告已有待审批的更正申请，请先结案")
            self._validate_codes(store, report, lines)

            revision = ReportRevision(
                id=self.ids.new_id("revision"),
                report_id=report_id,
                base_revision_no=report.current_revision_no,
                lines=lines,
                correction_reason=reason,
                status=RevisionStatus.PENDING,
                requested_by=principal.institution_id,
                requested_at=self.clock.now(),
            )
            store.add_report_revision(revision)
            store.add_report_event(
                report_id, "correction_requested",
                principal.institution_id, reason, revision.requested_at,
            )
        return self._revision_dict(revision)

    # ---- 审批 ----
    def decide(self, principal: Principal, revision_id: str, *,
               approve: bool, expected_version: int | None = None,
               reason: str = "") -> dict:
        """批准则生成新签发版本；驳回则结案不产生新版本。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            revision = store.get_report_revision(revision_id)
            if revision is None:
                raise NotFoundError(f"修订申请不存在: {revision_id}")
            report = store.get_report(revision.report_id)
            if report is None:
                raise NotFoundError(f"报告不存在: {revision.report_id}")
            AccessPolicy(store).require(
                principal, report.project_id, "*", "review"
            )
            if revision.status is not RevisionStatus.PENDING:
                raise StateError(
                    f"修订申请已结案（{revision.status.value}），不可再次审批"
                )
            if revision.requested_by == principal.institution_id:
                raise PermissionDeniedError("审批人不得是更正申请人本人")

            # Python 乐观锁：申请提交后若已有别的更正生效，当前版本必然
            # 前移，本申请基于的旧版本不得再写入——给出明确冲突响应。
            current_no = report.current_revision_no
            self._check_version(revision.base_revision_no, current_no,
                                stage="审批")
            if expected_version is not None:
                self._check_version(expected_version, current_no,
                                    stage="审批")

            now = self.clock.now()
            if approve:
                new_no = current_no + 1
                new_fingerprint = fingerprint(revision.lines)
                try:
                    store.add_revision_history(
                        report.id, new_no, revision.lines, new_fingerprint,
                        revision.correction_reason, revision.id,
                        principal.institution_id, now,
                    )
                except sqlite3.IntegrityError:
                    # 并发审批：新版本已被另一事务写入，冲突响应而非覆盖
                    raise ConflictError(
                        "签发版本已被并发更正推进，请基于最新版本重新申请",
                        detail={"base": revision.base_revision_no,
                                "current": new_no},
                    ) from None
                # 仅推进“当前版本”指针；reports 原签发行保持不可变，
                # 旧签发快照完整保留在历史表中，当前内容由历史表解析。
                store.set_current_revision_no(report.id, new_no)
                revision = replace(
                    revision, status=RevisionStatus.APPROVED,
                    new_revision_no=new_no,
                    decided_by=principal.institution_id, decided_at=now,
                    decision_reason=(reason or None),
                )
                store.set_revision_decision(revision)
                store.add_report_event(
                    report.id, "revision_approved",
                    principal.institution_id,
                    f"v{new_no}: {revision.correction_reason}", now,
                )
                result_no = new_no
            else:
                revision = replace(
                    revision, status=RevisionStatus.REJECTED,
                    decided_by=principal.institution_id, decided_at=now,
                    decision_reason=(reason.strip() or None),
                )
                store.set_revision_decision(revision)
                store.add_report_event(
                    report.id, "revision_rejected",
                    principal.institution_id, reason or None, now,
                )
                result_no = current_no
        return {"revision_id": revision.id, "report_id": revision.report_id,
                "status": revision.status.value,
                "current_revision_no": result_no}

    # ---- 查询 ----
    def list_requests(self, principal: Principal, report_id: str) -> dict:
        with self.db.read() as conn:
            store = Store(conn)
            report = store.get_report(report_id)
            if report is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            AccessPolicy(store).require(
                principal, report.project_id, "*", "view"
            )
            revisions = store.list_report_revisions(report_id)
        return {"report_id": report_id,
                "current_revision_no": report.current_revision_no,
                "requests": [self._revision_dict(r) for r in revisions]}

    def history(self, principal: Principal, report_id: str,
                revision_no: int | None = None) -> dict:
        """读取签发历史：可取指定旧版本，也可列出全部版本（含更正理由）。"""
        with self.db.read() as conn:
            store = Store(conn)
            report = store.get_report(report_id)
            if report is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            policy = AccessPolicy(store)
            policy.require(principal, report.project_id, "*", "view")

            def filter_lines(lines: list[dict]) -> tuple[list[dict], list[str]]:
                categories = sorted({l["category"] for l in lines})
                visible = set(policy.granted_categories(
                    principal, report.project_id, "view", categories))
                return ([l for l in lines if l["category"] in visible],
                        sorted(c for c in categories if c not in visible))

            if revision_no is not None:
                snap = store.get_revision_history(report_id, revision_no)
                if snap is None:
                    raise NotFoundError(
                        f"报告 {report_id} 不存在签发版本 v{revision_no}"
                    )
                lines, redacted = filter_lines(snap["lines"])
                return {"report_id": report_id,
                        "current_revision_no": report.current_revision_no,
                        "version": self._history_dict(snap, lines, redacted)}
            versions = []
            for snap in store.list_revision_history(report_id):
                lines, redacted = filter_lines(snap["lines"])
                versions.append(self._history_dict(snap, lines, redacted))
        return {"report_id": report_id,
                "current_revision_no": report.current_revision_no,
                "versions": versions}

    # ---- 辅助 ----
    @staticmethod
    def _load_signed(store: Store, report_id: str) -> Report:
        report = store.get_report(report_id)
        if report is None:
            raise NotFoundError(f"报告不存在: {report_id}")
        if report.status is not ReportStatus.REVIEWED:
            raise StateError(
                f"报告状态为 {report.status.value}，仅已签发报告可申请更正"
            )
        if report.current_revision_no is None:
            raise StateError("已签发报告缺少版本指针，数据异常")
        return report

    @staticmethod
    def _check_version(expected: int, current: int | None, *, stage: str) -> None:
        """Python 层乐观锁版本校验：不一致即明确冲突，绝不静默覆盖。"""
        if expected != current:
            raise ConflictError(
                f"{stage}基于的签发版本 v{expected} 已过期，当前为 v{current}；"
                "请基于最新版本重新操作",
                detail={"expected": expected, "current": current},
            )

    @staticmethod
    def _validate_lines(lines: list[dict]) -> None:
        for line in lines:
            if not isinstance(line, dict) or "code" not in line or "value" not in line:
                raise ValidationError(
                    "每个更正行必须包含 code 与 value"
                )
            value = line["value"]
            if value is not None and not isinstance(value, (int, float)):
                raise ValidationError("更正行 value 必须为数值或 null")

    @staticmethod
    def _validate_codes(store: Store, report: Report, lines: list[dict]) -> None:
        current = store.get_revision_history(
            report.id, report.current_revision_no
        )
        assert current is not None
        old_codes = [l["code"] for l in current["lines"]]
        new_codes = [l["code"] for l in lines]
        if sorted(new_codes) != sorted(old_codes) or len(set(new_codes)) != len(new_codes):
            raise ValidationError(
                "更正行的指标 code 集合必须与当前签发版本一致",
                detail={"signed_codes": old_codes, "submitted_codes": new_codes},
            )

    @staticmethod
    def _revision_dict(rev: ReportRevision) -> dict:
        return {
            "revision_id": rev.id,
            "report_id": rev.report_id,
            "base_revision_no": rev.base_revision_no,
            "new_revision_no": rev.new_revision_no,
            "correction_reason": rev.correction_reason,
            "status": rev.status.value,
            "requested_by": rev.requested_by,
            "requested_at": rev.requested_at,
            "decided_by": rev.decided_by,
            "decided_at": rev.decided_at,
            "decision_reason": rev.decision_reason,
        }

    @staticmethod
    def _history_dict(snap: dict, lines: list[dict],
                      redacted: list[str]) -> dict:
        return {
            "revision_no": snap["revision_no"],
            "lines": lines,
            "result_fingerprint": snap["result_fingerprint"],
            "change_reason": snap["change_reason"],
            "source_revision_id": snap["source_revision_id"],
            "created_by": snap["created_by"],
            "created_at": snap["created_at"],
            "redacted_categories": redacted,
        }
