"""复核服务：报告定稿前的独立复核，通过后方可导出，驳回为终态。

复核通过即“签发”：原始签发内容快照为修订链的 v0（存于 report_revisions），
此后任何更正都只能新增修订，旧签发内容始终可被读取。
"""
from __future__ import annotations

from ..domain.errors import NotFoundError, PermissionDeniedError, StateError
from ..domain.models import (
    Principal,
    ReportRevision,
    ReportStatus,
    RevisionStatus,
)
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator
from .access import AccessPolicy


class ReviewService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    def review(self, principal: Principal, report_id: str, *, approve: bool,
               reason: str = "") -> dict:
        """复核通过或驳回。复核人不得是报告的原计算人（独立性要求）。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            report = store.get_report(report_id)
            if report is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            AccessPolicy(store).require(
                principal, report.project_id, "*", "review"
            )
            if report.created_by == principal.institution_id:
                raise PermissionDeniedError("复核人不得是报告的原计算人")
            if report.status is ReportStatus.REVIEWED:
                raise StateError("报告已复核通过，为不可变终态")
            if report.status is ReportStatus.REJECTED:
                raise StateError("报告已被驳回，为不可变终态")

            now = self.clock.now()
            new_status = ReportStatus.REVIEWED if approve else ReportStatus.REJECTED
            store.set_report_status(report_id, new_status)
            if approve:
                # 原始签发固化为 v0；后续更正只追加修订，不覆盖此快照
                store.add_revision(ReportRevision(
                    id=self.ids.new_id("revision"),
                    report_id=report_id,
                    revision_no=0,
                    status=RevisionStatus.ISSUED,
                    pins=report.pins,
                    lines=report.lines,
                    data_version_no=report.data_version_no,
                    input_fingerprint=report.input_fingerprint,
                    result_fingerprint=report.result_fingerprint,
                    correction_reason=None,
                    issued_by=principal.institution_id,
                    issued_at=now,
                ))
            store.add_report_event(
                report_id,
                "reviewed" if approve else "rejected",
                principal.institution_id,
                reason or None,
                now,
            )
        return {"report_id": report_id, "status": new_status.value}
