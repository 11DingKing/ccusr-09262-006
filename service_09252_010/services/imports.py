"""数据导入服务：证据登记、批量导入、迟到数据形成新版本并给出差异。"""
from __future__ import annotations

import re

from ..domain.errors import NotFoundError, ValidationError
from ..domain.models import EvidenceSource, ImportBatch, Observation, Principal
from ..domain.periods import validate_period
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator
from .access import AccessPolicy

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ImportService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    def register_evidence(self, principal: Principal, *, project_id: str,
                          kind: str, uri: str, sha256: str) -> dict:
        """登记证据来源；观测记录必须引用已登记证据。"""
        with self.db.uow() as uow:
            AccessPolicy(Store(uow.conn)).require(principal, project_id, "*", "import")
            if not _SHA256_RE.match(sha256 or ""):
                raise ValidationError("证据 sha256 必须为 64 位小写十六进制")
            evidence = EvidenceSource(
                id=self.ids.new_id("ev"),
                project_id=project_id,
                kind=kind,
                uri=uri,
                sha256=sha256,
                registered_by=principal.institution_id,
                registered_at=self.clock.now(),
            )
            Store(uow.conn).add_evidence(evidence)
        return {"evidence_id": evidence.id}

    def import_batch(self, principal: Principal, project_id: str, *,
                     records: list[dict], reason: str) -> dict:
        """导入一批观测记录，形成新的数据版本并返回与上一版本的差异。

        记录格式：{measure, period, caliber, value|null, evidence_id}
        或撤回记录：{measure, period, caliber, evidence_id, retract: true}
        迟到数据（写入已覆盖期间）同样只追加为新版本，旧版本可重放。
        """
        if not isinstance(records, list) or not records:
            raise ValidationError("导入记录不能为空")
        now = self.clock.now()
        with self.db.uow() as uow:
            store = Store(uow.conn)
            AccessPolicy(store).require(principal, project_id, "*", "import")

            prev_seq = store.latest_batch_seq(project_id)
            prev_snapshot = (
                _effective_map(store.snapshot(project_id, prev_seq))
                if prev_seq is not None
                else {}
            )

            seen: set[tuple[str, str, str]] = set()
            parsed: list[dict] = []
            for i, raw in enumerate(records):
                parsed.append(
                    self._validate_record(store, project_id, raw, i, seen, prev_snapshot)
                )

            seq = (prev_seq or 0) + 1
            batch = ImportBatch(
                id=self.ids.new_id("batch"),
                project_id=project_id,
                seq=seq,
                reason=reason or "",
                created_by=principal.institution_id,
                created_at=now,
            )
            store.add_batch(batch)
            for rec in parsed:
                store.add_observation(
                    Observation(
                        batch_id=batch.id,
                        project_id=project_id,
                        measure=rec["measure"],
                        period=rec["period"],
                        caliber=rec["caliber"],
                        value=rec["value"],
                        retracted=rec["retract"],
                        evidence_id=rec["evidence_id"],
                        institution_id=principal.institution_id,
                        created_at=now,
                    )
                )
            new_snapshot = _effective_map(store.snapshot(project_id, seq))
            diff = _diff(prev_snapshot, new_snapshot)
        return {"version_no": seq, "batch_id": batch.id, "diff": diff}

    def version_diff(self, project_id: str, version_no: int,
                     against: int | None = None) -> dict:
        """查询两个数据版本之间的差异，默认与上一版本比较。"""
        with self.db.read() as conn:
            store = Store(conn)
            base = against if against is not None else version_no - 1
            if base < 0 or version_no < 1:
                raise ValidationError("版本号非法")
            latest = store.latest_batch_seq(project_id)
            if latest is None or version_no > latest or base > latest:
                raise NotFoundError(f"数据版本不存在: 项目 {project_id}")
            old = _effective_map(store.snapshot(project_id, base)) if base >= 1 else {}
            new = _effective_map(store.snapshot(project_id, version_no))
        return {"from": base, "to": version_no, "diff": _diff(old, new)}

    def _validate_record(self, store: Store, project_id: str, raw: dict, index: int,
                         seen: set[tuple[str, str, str]],
                         prev_snapshot: dict) -> dict:
        where = f"记录[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{where}: 必须为对象")
        measure = raw.get("measure")
        period = raw.get("period")
        caliber = raw.get("caliber")
        evidence_id = raw.get("evidence_id")
        if not measure or not isinstance(measure, str):
            raise ValidationError(f"{where}: measure 缺失")
        try:
            validate_period(period)
        except ValidationError as exc:
            raise ValidationError(f"{where}: {exc.message}") from None
        if not caliber or not isinstance(caliber, str):
            raise ValidationError(f"{where}: caliber 缺失")
        evidence = store.get_evidence(evidence_id or "")
        if evidence is None or evidence.project_id != project_id:
            raise ValidationError(f"{where}: 证据 {evidence_id!r} 未登记于项目 {project_id}")

        key = (measure, period, caliber)
        if key in seen:
            raise ValidationError(f"{where}: 同批次内自然键重复 {key}")
        seen.add(key)

        retract = bool(raw.get("retract", False))
        value = raw.get("value")
        if retract:
            if key not in prev_snapshot:
                raise ValidationError(f"{where}: 撤回的自然键在上一版本不存在 {key}")
            value = None
        elif value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"{where}: value 必须为数值或 null（缺失）")
            value = float(value)
        return {
            "measure": measure,
            "period": period,
            "caliber": caliber,
            "value": value,
            "retract": retract,
            "evidence_id": evidence_id,
        }


def _effective_map(observations: list[Observation]) -> dict:
    """有效视图：自然键 -> 记录内容；撤回记录体现为键不存在。"""
    result: dict = {}
    for obs in observations:
        key = (obs.measure, obs.period, obs.caliber)
        if obs.retracted:
            result.pop(key, None)
        else:
            result[key] = {"value": obs.value, "evidence_id": obs.evidence_id}
    return result


def _diff(old: dict, new: dict) -> dict:
    added, changed, retracted = [], [], []
    for key in sorted(new):
        entry = {"measure": key[0], "period": key[1], "caliber": key[2]}
        if key not in old:
            added.append({**entry, "value": new[key]["value"]})
        elif old[key]["value"] != new[key]["value"]:
            changed.append({
                **entry,
                "old_value": old[key]["value"],
                "new_value": new[key]["value"],
            })
    for key in sorted(old):
        if key not in new:
            retracted.append({
                "measure": key[0], "period": key[1], "caliber": key[2],
                "old_value": old[key]["value"],
            })
    return {"added": added, "changed": changed, "retracted": retracted}
