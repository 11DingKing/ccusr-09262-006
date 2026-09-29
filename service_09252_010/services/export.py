"""导出服务：仅复核通过的报告可导出，导出包附换算依据与证据清单及摘要。"""
from __future__ import annotations

from ..domain.errors import NotFoundError, StateError
from ..domain.fingerprint import canonical_json, fingerprint
from ..domain.models import Principal, ReportStatus
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator
from .access import AccessPolicy


class ExportService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    def export(self, principal: Principal, report_id: str) -> dict:
        """生成导出包（JSON 可序列化）。每次导出均留痕，重复导出内容可复算。"""
        with self.db.uow() as uow:
            store = Store(uow.conn)
            report = store.get_report(report_id)
            if report is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            AccessPolicy(store).require(
                principal, report.project_id, "*", "export"
            )
            if report.status is not ReportStatus.REVIEWED:
                raise StateError(
                    f"报告状态为 {report.status.value}，仅复核通过后可导出"
                )

            # 已签发报告以当前修订版本为导出内容；旧版本通过历史接口读取
            current_no = report.current_revision_no or 1
            snap = store.get_revision_history(report_id, current_no)
            if snap is None:
                raise StateError(
                    f"导出失败：签发版本 v{current_no} 历史缺失"
                )
            signed_lines = snap["lines"]
            revision_chain = [
                {"revision_no": h["revision_no"],
                 "change_reason": h["change_reason"],
                 "result_fingerprint": h["result_fingerprint"],
                 "created_at": h["created_at"]}
                for h in store.list_revision_history(report_id)
            ]

            evidence_ids = sorted({
                eid for line in signed_lines for eid in line.get("evidence_ids", [])
            })
            evidence = [
                {
                    "evidence_id": e.id,
                    "kind": e.kind,
                    "uri": e.uri,
                    "sha256": e.sha256,
                    "registered_by": e.registered_by,
                    "registered_at": e.registered_at,
                }
                for e in store.evidence_by_ids(evidence_ids)
            ]
            rules_basis = []
            for key, version_no in sorted(report.pins.get("rules", {}).items()):
                rule = store.get_rule_by_version(key, version_no)
                if rule is None:
                    raise StateError(f"导出失败：固化规则 {key} v{version_no} 缺失")
                rules_basis.append({
                    "rule_key": key,
                    "version_no": version_no,
                    "formula": f"{rule.measure}: {rule.from_caliber}"
                               f" -> {rule.to_caliber}: x * {rule.factor}"
                               f" + {rule.offset}",
                    "signatures": store.list_signatures(rule.id),
                })

            document = {
                "report_id": report.id,
                "project_id": report.project_id,
                "window": [report.window_start, report.window_end],
                "target_caliber": report.target_caliber,
                "data_version_no": report.data_version_no,
                "pins": report.pins,
                "lines": signed_lines,
                "current_revision_no": current_no,
                "revision_history": revision_chain,
                "conversion_basis": rules_basis,
                "evidence": evidence,
                "status": report.status.value,
                "created_by": report.created_by,
                "created_at": report.created_at,
            }
            digest = fingerprint(document)
            export_id = self.ids.new_id("export")
            now = self.clock.now()
            store.add_export(export_id, report_id, principal.institution_id, now,
                             digest)
            history = [
                {"export_id": r["id"], "exported_by": r["exported_by"],
                 "exported_at": r["exported_at"], "digest": r["digest"]}
                for r in store.list_exports(report_id)
            ]
        return {
            "export_id": export_id,
            "digest": digest,
            "canonical_document": canonical_json(document),
            "document": document,
            "export_history": history,
        }
