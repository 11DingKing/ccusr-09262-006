"""已签报告修订服务：更正必须生成新修订，旧签发内容与更正理由同时留痕。

版本校验采用应用层（Python）乐观并发：
- 申请与签发均携带 expected_version（缺省视为当前版本），
  与库内 reports.revision_version 不一致即抛 ConflictError（HTTP 409）；
- 写事务统一 BEGIN IMMEDIATE 串行化，作为并发兜底。

签发历史全部保留在 SQLite 的 report_revisions 表：
复核通过时原始签发固化为 v0，每次更正签发追加一行新修订；
reports 行只保存“当前生效内容”，读者可同时读取旧签发内容、新内容与更正理由。
"""
from __future__ import annotations

from ..domain.errors import (
    ConflictError,
    NotFoundError,
    StateError,
    ValidationError,
)
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
from .calculation import CalculationService


class RevisionService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator,
                 calculation: CalculationService) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids
        self.calculation = calculation

    def request_revision(self, principal: Principal, report_id: str, *,
                         reason: str, expected_version: int | None = None,
                         pins: dict | None = None) -> dict:
        """对已签发报告提交更正申请，生成一个 proposed 新修订（尚未生效）。

        pins 为 None 时按当前最新数据/指标/规则版本重算；
        也可显式指定 pins（例如指定迟到数据形成的新数据版本）。
        """
        if not reason or not reason.strip():
            raise ValidationError("更正申请必须提供 correction_reason")
        with self.db.uow() as uow:
            store = Store(uow.conn)
            report = self._load_signed(store, report_id)
            AccessPolicy(store).require(
                principal, report.project_id, "*", "calculate"
            )
            self._check_version(report, expected_version)

            recomputed = self.calculation.recompute(
                store, project_id=report.project_id,
                window_start=report.window_start, window_end=report.window_end,
                target_caliber=report.target_caliber, pins=pins,
            )
            if recomputed["result_fingerprint"] == report.result_fingerprint \
                    and recomputed["pins"] == report.pins:
                raise ValidationError("更正内容与当前签发内容一致，无需生成新修订")

            revisions = store.list_revisions(report_id)
            revision_no = max([0] + [r.revision_no for r in revisions]) + 1
            now = self.clock.now()
            revision = ReportRevision(
                id=self.ids.new_id("revision"),
                report_id=report_id,
                revision_no=revision_no,
                status=RevisionStatus.PROPOSED,
                pins=recomputed["pins"],
                lines=recomputed["lines"],
                data_version_no=recomputed["data_version_no"],
                input_fingerprint=recomputed["input_fingerprint"],
                result_fingerprint=recomputed["result_fingerprint"],
                correction_reason=reason.strip(),
                requested_by=principal.institution_id,
                requested_at=now,
                base_version=report.revision_version,
            )
            store.add_revision(revision)
            store.add_report_event(
                report_id, "revision_requested", principal.institution_id,
                reason.strip(), now,
            )
        return self._revision_dict(revision)

    def issue_revision(self, principal: Principal, report_id: str,
                       revision_no: int, *,
                       expected_version: int | None = None) -> dict:
        """签发修订：新内容提升为当前报告内容，旧内容保留在修订历史中。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            report = self._load_signed(store, report_id)
            AccessPolicy(store).require(
                principal, report.project_id, "*", "review"
            )
            self._check_version(report, expected_version)

            revision = store.get_revision(report_id, revision_no)
            if revision is None:
                raise NotFoundError(
                    f"修订不存在: 报告 {report_id} 修订号 {revision_no}"
                )
            if revision.status is RevisionStatus.ISSUED:
                raise StateError(f"修订 {revision_no} 已签发，不可重复签发")
            if revision.revision_no <= report.revision_version:
                # 期间已有更新的修订签发：旧申请被超越
                raise ConflictError(
                    f"修订 {revision_no} 已被更新的签发修订超越",
                    detail={"current_version": report.revision_version},
                )

            now = self.clock.now()
            store.apply_revision(report_id, revision)
            store.mark_revision_issued(revision.id,
                                       principal.institution_id, now)
            issued = ReportRevision(
                **{**revision.__dict__,
                   "status": RevisionStatus.ISSUED,
                   "issued_by": principal.institution_id,
                   "issued_at": now}
            )
            store.add_report_event(
                report_id, "revision_issued", principal.institution_id,
                revision.correction_reason, now,
            )
        return self._revision_dict(issued, current=True)

    def list_revisions(self, principal: Principal, report_id: str) -> dict:
        """签发历史：原始签发（v0）与全部修订，读者可对照旧内容与更正理由。"""
        with self.db.read() as conn:
            store = Store(conn)
            report = store.get_report(report_id)
            if report is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            AccessPolicy(store).require(
                principal, report.project_id, "*", "view"
            )
            revisions = store.list_revisions(report_id)
            events = store.list_report_events(report_id)

        history: list[dict] = []
        if not revisions and report.status is ReportStatus.REVIEWED:
            # 兼容历史数据：复核通过早于修订留痕机制
            history.append(self._original_dict(report))
        for revision in revisions:
            history.append(self._revision_dict(
                revision, current=revision.revision_no == report.revision_version
            ))
        return {
            "report_id": report_id,
            "current_version": report.revision_version,
            "history": history,
            "events": events,
        }

    def get_version(self, principal: Principal, report_id: str,
                    version: int) -> dict:
        """读取指定签发版本的内容（0 为原始签发），旧版始终可读。"""
        with self.db.read() as conn:
            store = Store(conn)
            report = store.get_report(report_id)
            if report is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            AccessPolicy(store).require(
                principal, report.project_id, "*", "view"
            )
            revision = store.get_revision(report_id, version)
            if revision is None and version == 0 \
                    and report.revision_version == 0 \
                    and report.status is ReportStatus.REVIEWED:
                # 历史数据：尚无修订留痕，当前内容即原始签发
                return self._original_dict(report)
        if revision is None or revision.status is not RevisionStatus.ISSUED:
            raise NotFoundError(f"已签发版本不存在: 报告 {report_id} v{version}")
        return self._revision_dict(revision)

    @staticmethod
    def _load_signed(store: Store, report_id: str) -> Report:
        report = store.get_report(report_id)
        if report is None:
            raise NotFoundError(f"报告不存在: {report_id}")
        if report.status is not ReportStatus.REVIEWED:
            raise StateError(
                f"报告状态为 {report.status.value}，仅已签发（复核通过）"
                "的报告可申请更正"
            )
        return report

    @staticmethod
    def _check_version(report: Report, expected_version: int | None) -> None:
        """Python 应用层乐观版本校验：不符即清晰冲突响应。"""
        if expected_version is None:
            return
        if expected_version != report.revision_version:
            raise ConflictError(
                "报告修订版本已变化，请基于最新版本重新提交",
                detail={
                    "expected_version": expected_version,
                    "current_version": report.revision_version,
                },
            )

    @staticmethod
    def _original_dict(report: Report) -> dict:
        return {
            "revision_id": None,
            "version": 0,
            "status": RevisionStatus.ISSUED.value,
            "report_id": report.id,
            "data_version_no": report.data_version_no,
            "pins": report.pins,
            "lines": report.lines,
            "input_fingerprint": report.input_fingerprint,
            "result_fingerprint": report.result_fingerprint,
            "correction_reason": None,
            "requested_by": None,
            "requested_at": None,
            "issued_by": report.created_by,
            "issued_at": report.created_at,
            "base_version": None,
            "current": report.revision_version == 0,
        }

    @staticmethod
    def _revision_dict(revision: ReportRevision, *,
                       current: bool = False) -> dict:
        return {
            "revision_id": revision.id,
            "version": revision.revision_no,
            "status": revision.status.value,
            "report_id": revision.report_id,
            "data_version_no": revision.data_version_no,
            "pins": revision.pins,
            "lines": revision.lines,
            "input_fingerprint": revision.input_fingerprint,
            "result_fingerprint": revision.result_fingerprint,
            "correction_reason": revision.correction_reason,
            "requested_by": revision.requested_by,
            "requested_at": revision.requested_at,
            "issued_by": revision.issued_by,
            "issued_at": revision.issued_at,
            "base_version": revision.base_version,
            "current": current,
        }
