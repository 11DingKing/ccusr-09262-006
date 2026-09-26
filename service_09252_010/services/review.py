"""复核服务：报告定稿前的独立复核，通过后方可导出，驳回为终态。"""
from __future__ import annotations

from ..domain.errors import NotFoundError, PermissionDeniedError, StateError
from ..domain.models import Principal, ReportStatus
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock
from .access import AccessPolicy


class ReviewService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

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

            new_status = ReportStatus.REVIEWED if approve else ReportStatus.REJECTED
            store.set_report_status(report_id, new_status)
            store.add_report_event(
                report_id,
                "reviewed" if approve else "rejected",
                principal.institution_id,
                reason or None,
                self.clock.now(),
            )
        return {"report_id": report_id, "status": new_status.value}
