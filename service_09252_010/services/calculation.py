"""计算服务：幂等提交、按断点恢复执行、版本固化与复算核对。

任务四步均在独立事务中落检查点，崩溃后再次调用从首个无检查点的步骤继续；
最终报告以 task_id 唯一约束兜底并发，重复执行收敛为同一份报告。
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

from ..domain.conversion import convert_rows, rule_key
from ..domain.errors import (
    ConflictError,
    NotFoundError,
    StateError,
    ValidationError,
)
from ..domain.fingerprint import fingerprint
from ..domain.formulas import evaluate, formula_measures, validate_formula
from ..domain.models import (
    CALCULATION_STEPS,
    ComputationTask,
    Principal,
    Report,
    ReportStatus,
    SnapshotRow,
    TaskStatus,
)
from ..domain.periods import iter_periods, period_key
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator
from .access import AccessPolicy

STEP_SNAPSHOT, STEP_CONVERT, STEP_AGGREGATE, STEP_PERSIST = CALCULATION_STEPS

_ABSENT = object()  # 期间槽位尚无记录（区别于已记录的缺失值 None）


class CalculationService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    # ---- 提交（幂等）----
    def submit(self, principal: Principal, *, project_id: str, window_start: str,
               window_end: str, target_caliber: str,
               idempotency_key: str) -> dict:
        """登记计算任务。同一幂等键重复提交：参数一致则返回既有任务。"""
        periods = iter_periods(window_start, window_end)
        if not target_caliber:
            raise ValidationError("target_caliber 缺失")
        request_fingerprint = fingerprint({
            "project_id": project_id,
            "window": [window_start, window_end],
            "target_caliber": target_caliber,
        })
        with self.db.uow() as uow:
            store = Store(uow.conn)
            AccessPolicy(store).require(principal, project_id, "*", "calculate")
            existing = store.task_by_key(idempotency_key)
            now = self.clock.now()
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise ConflictError(
                        "幂等键已被不同参数占用",
                        detail={"existing_task_id": existing.id},
                    )
                task = existing
            else:
                task = ComputationTask(
                    id=self.ids.new_id("task"),
                    idempotency_key=idempotency_key,
                    project_id=project_id,
                    window_start=window_start,
                    window_end=window_end,
                    target_caliber=target_caliber,
                    request_fingerprint=request_fingerprint,
                    status=TaskStatus.PENDING,
                    current_step=None,
                    report_id=None,
                    error=None,
                    created_by=principal.institution_id,
                    created_at=now,
                    updated_at=now,
                )
                try:
                    store.insert_task(task)
                except sqlite3.IntegrityError:
                    # 并发提交同键：另一事务抢先提交，收敛到既有任务
                    existing = store.task_by_key(idempotency_key)
                    assert existing is not None
                    if existing.request_fingerprint != request_fingerprint:
                        raise ConflictError(
                            "幂等键已被不同参数占用",
                            detail={"existing_task_id": existing.id},
                        ) from None
                    task = existing
        return {"task_id": task.id, "status": task.status.value,
                "already_exists": existing is not None}

    # ---- 执行/恢复 ----
    def run(self, task_id: str) -> dict:
        """执行任务；已完成直接返回，崩溃或失败则从断点继续。

        若断点固化的数据版本已落后于最新版本（期间补了迟到数据），
        旧断点全部作废，从头重算，避免跨版本串用中间结果。
        """
        with self.db.read() as conn:
            task = Store(conn).task_by_id(task_id)
        if task is None:
            raise NotFoundError(f"计算任务不存在: {task_id}")
        if task.status is TaskStatus.DONE:
            return {"task_id": task_id, "status": "done",
                    "report_id": task.report_id, "resumed": False}

        self._invalidate_stale_checkpoints(task_id, task.project_id)
        resumed = task.status in (TaskStatus.RUNNING, TaskStatus.FAILED)
        for step in CALCULATION_STEPS:
            self._run_step(task_id, step)
        with self.db.read() as conn:
            done = Store(conn).task_by_id(task_id)
        assert done is not None and done.report_id is not None
        return {"task_id": task_id, "status": done.status.value,
                "report_id": done.report_id, "resumed": resumed}

    def _invalidate_stale_checkpoints(self, task_id: str, project_id: str) -> None:
        with self.db.read() as conn:
            store = Store(conn)
            checkpoints = store.load_checkpoints(task_id)
            latest_seq = store.latest_batch_seq(project_id)
        snap = checkpoints.get(STEP_SNAPSHOT)
        if snap is not None and latest_seq is not None \
                and snap["data_version_no"] != latest_seq:
            with self.db.uow() as uow:
                Store(uow.conn).delete_checkpoints(task_id)

    def _run_step(self, task_id: str, step: str) -> None:
        failure: Exception | None = None
        with self.db.uow() as uow:
            store = Store(uow.conn)
            task = store.task_by_id(task_id)
            assert task is not None
            checkpoints = store.load_checkpoints(task_id)

            if task.status is TaskStatus.DONE:
                return
            if step in checkpoints and step != STEP_PERSIST:
                return  # 该步骤此前已完成，断点恢复时跳过
            if step == STEP_PERSIST and task.report_id is not None:
                return

            running = replace(task, status=TaskStatus.RUNNING, current_step=step,
                              updated_at=self.clock.now())
            store.update_task(running)
            try:
                if step == STEP_PERSIST:
                    report = self._persist_report(store, running, checkpoints)
                    finished = replace(
                        running, status=TaskStatus.DONE, current_step=None,
                        report_id=report.id, error=None,
                        updated_at=self.clock.now(),
                    )
                    store.update_task(finished)
                    store.add_report_event(report.id, "computed",
                                           running.created_by, None,
                                           self.clock.now())
                else:
                    payload = self._execute_step(store, running, step,
                                                 checkpoints)
                    store.save_checkpoint(task_id, step, payload,
                                          self.clock.now())
            except Exception as exc:
                # 先让当前事务回滚释放写锁，退出后再另起事务记录失败断点
                failure = exc
        if failure is not None:
            self._mark_failed(task_id, step, failure)
            raise failure

    def _mark_failed(self, task_id: str, step: str, exc: Exception) -> None:
        with self.db.uow() as uow:
            store = Store(uow.conn)
            task = store.task_by_id(task_id)
            if task is None or task.status is TaskStatus.DONE:
                return
            store.update_task(replace(
                task, status=TaskStatus.FAILED, current_step=step,
                error=f"{type(exc).__name__}: {exc}",
                updated_at=self.clock.now(),
            ))

    def _execute_step(self, store: Store, task: ComputationTask, step: str,
                      checkpoints: dict[str, dict]) -> dict:
        if step == STEP_SNAPSHOT:
            return self._step_snapshot(store, task)
        if step == STEP_CONVERT:
            return self._step_convert(store, task, checkpoints[STEP_SNAPSHOT])
        if step == STEP_AGGREGATE:
            return self._step_aggregate(store, task, checkpoints)
        return {}

    def _step_snapshot(self, store: Store, task: ComputationTask) -> dict:
        seq = store.latest_batch_seq(task.project_id)
        if seq is None:
            raise ValidationError("项目尚无任何数据版本，无法计算")
        lo, hi = period_key(task.window_start), period_key(task.window_end)
        rows = []
        for obs in store.snapshot(task.project_id, seq):
            if obs.retracted or not (lo <= period_key(obs.period) <= hi):
                continue
            rows.append({
                "measure": obs.measure,
                "period": obs.period,
                "caliber": obs.caliber,
                "value": obs.value,
                "evidence_id": obs.evidence_id,
            })
        return {"data_version_no": seq, "rows": rows}

    def _step_convert(self, store: Store, task: ComputationTask,
                      snapshot_payload: dict) -> dict:
        active = {r.rule_key: r for r in store.active_rules()}
        rules_params = {key: (r.factor, r.offset) for key, r in active.items()}
        rows = [SnapshotRow(**r) for r in snapshot_payload["rows"]]
        converted, missing_keys = convert_rows(
            rows, rules_params, task.target_caliber
        )
        if missing_keys:
            raise ValidationError(
                "缺少口径换算规则，已停在换算断点",
                detail={"missing_rules": sorted(missing_keys)},
            )
        rules_used: dict[str, int] = {}
        for original in rows:
            if original.caliber == task.target_caliber:
                continue
            key = rule_key(original.measure, original.caliber, task.target_caliber)
            rule = active.get(key)
            if rule is not None:
                rules_used[key] = rule.version_no
        return {
            "rows": [
                {"measure": r.measure, "period": r.period,
                 "caliber": r.caliber, "value": r.value,
                 "evidence_id": r.evidence_id}
                for r in converted
            ],
            "rules_used": rules_used,
        }

    def _step_aggregate(self, store: Store, task: ComputationTask,
                        checkpoints: dict[str, dict]) -> dict:
        snap = checkpoints[STEP_SNAPSHOT]
        conv = checkpoints[STEP_CONVERT]
        periods = iter_periods(task.window_start, task.window_end)

        values: dict[str, dict[str, float | None]] = {}
        evidence_by_measure: dict[str, set[str]] = {}
        for r in conv["rows"]:
            slot = values.setdefault(r["measure"], {})
            existing = slot.get(r["period"], _ABSENT)
            if existing is _ABSENT or existing is None:
                # 缺失（None）不具约束力，可被其他口径换算出的实值取代
                slot[r["period"]] = r["value"]
            elif r["value"] is None:
                pass  # 迟到缺失记录不覆盖已有实值
            elif existing != r["value"]:
                raise ValidationError(
                    f"度量 {r['measure']} 在 {r['period']} 存在多个口径换算结果，"
                    "数据口径不唯一",
                )
            if r.get("evidence_id"):
                evidence_by_measure.setdefault(r["measure"], set()).add(
                    r["evidence_id"]
                )

        lines: list[dict] = []
        indicator_pins: dict[str, int] = {}
        for indicator in store.list_indicators():
            version = store.latest_indicator_version(indicator.code)
            assert version is not None
            validate_formula(version.formula)
            needed = formula_measures(version.formula)
            scoped = {m: values.get(m, {}) for m in needed}
            result = evaluate(version.formula, version.missing_policy,
                              periods, scoped)
            indicator_pins[indicator.code] = version.version_no
            evidence_ids = sorted({
                eid for m in needed for eid in evidence_by_measure.get(m, set())
            })
            lines.append({
                "code": indicator.code,
                "name": indicator.name,
                "category": indicator.category,
                "unit": indicator.unit,
                "version_no": version.version_no,
                "value": result.value,
                "covered_periods": list(result.covered_periods),
                "missing_periods": list(result.missing_periods),
                "notes": list(result.notes),
                "evidence_ids": evidence_ids,
            })

        pins = {
            "data_version": snap["data_version_no"],
            "indicators": indicator_pins,
            "rules": conv["rules_used"],
        }
        input_payload = {
            "project_id": task.project_id,
            "window": [task.window_start, task.window_end],
            "target_caliber": task.target_caliber,
            "pins": pins,
            "snapshot": snap["rows"],
        }
        return {
            "lines": lines,
            "pins": pins,
            "input_fingerprint": fingerprint(input_payload),
        }

    def _persist_report(self, store: Store, task: ComputationTask,
                        checkpoints: dict[str, dict]) -> Report:
        agg = checkpoints[STEP_AGGREGATE]
        snap = checkpoints[STEP_SNAPSHOT]
        try:
            report_id = self.ids.new_id("report")
            report = Report(
                id=report_id,
                project_id=task.project_id,
                window_start=task.window_start,
                window_end=task.window_end,
                target_caliber=task.target_caliber,
                data_version_no=snap["data_version_no"],
                pins=agg["pins"],
                lines=agg["lines"],
                input_fingerprint=agg["input_fingerprint"],
                result_fingerprint=fingerprint(agg["lines"]),
                status=ReportStatus.COMPUTED,
                created_by=task.created_by,
                created_at=self.clock.now(),
                task_id=task.id,
            )
            store.add_report(report)
        except sqlite3.IntegrityError:
            # 并发重复执行：报告已由另一执行落库，收敛到同一份。
            existing = store.task_by_id(task.id)
            assert existing is not None and existing.report_id is not None
            report = store.get_report(existing.report_id)
            assert report is not None
        return report

    # ---- 查询与复算 ----
    def get_task(self, task_id: str) -> dict:
        with self.db.read() as conn:
            task = Store(conn).task_by_id(task_id)
            if task is None:
                raise NotFoundError(f"计算任务不存在: {task_id}")
            checkpoints = Store(conn).load_checkpoints(task_id)
        return {
            "task_id": task.id,
            "idempotency_key": task.idempotency_key,
            "project_id": task.project_id,
            "window": [task.window_start, task.window_end],
            "target_caliber": task.target_caliber,
            "status": task.status.value,
            "current_step": task.current_step,
            "report_id": task.report_id,
            "error": task.error,
            "completed_steps": list(checkpoints),
            "created_by": task.created_by,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    def get_report(self, principal: Principal, report_id: str) -> dict:
        with self.db.read() as conn:
            store = Store(conn)
            report = store.get_report(report_id)
            if report is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            policy = AccessPolicy(store)
            categories = sorted({line["category"] for line in report.lines})
            visible = set(
                policy.granted_categories(principal, report.project_id,
                                          "view", categories)
            )
            events = store.list_report_events(report_id)
        visible_lines = [l for l in report.lines if l["category"] in visible]
        redacted = sorted(c for c in categories if c not in visible)
        return self._report_dict(report, visible_lines, redacted, events)

    def list_reports(self, principal: Principal, project_id: str) -> list[dict]:
        with self.db.read() as conn:
            store = Store(conn)
            reports = store.list_reports(project_id)
            policy = AccessPolicy(store)
            result = []
            for report in reports:
                categories = sorted({l["category"] for l in report.lines})
                visible = set(policy.granted_categories(
                    principal, project_id, "view", categories))
                lines = [l for l in report.lines if l["category"] in visible]
                redacted = sorted(c for c in categories if c not in visible)
                result.append(self._report_dict(report, lines, redacted, []))
        return result

    @staticmethod
    def _report_dict(report: Report, lines: list[dict], redacted: list[str],
                     events: list[dict]) -> dict:
        return {
            "report_id": report.id,
            "project_id": report.project_id,
            "window": [report.window_start, report.window_end],
            "target_caliber": report.target_caliber,
            "data_version_no": report.data_version_no,
            "pins": report.pins,
            "lines": lines,
            "input_fingerprint": report.input_fingerprint,
            "result_fingerprint": report.result_fingerprint,
            "status": report.status.value,
            "created_by": report.created_by,
            "created_at": report.created_at,
            "redacted_categories": redacted,
            "events": events,
        }

    def reverify(self, principal: Principal, report_id: str) -> dict:
        """按报告固化的版本（指标/规则/数据）复算，核对结果指纹。

        指标定义更新、规则回滚或迟到数据均不影响复算——全部输入按 pins 取版本。
        """
        with self.db.read() as conn:
            store = Store(conn)
            report = store.get_report(report_id)
            if report is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            AccessPolicy(store).require(principal, report.project_id, "*", "view")
            periods = iter_periods(report.window_start, report.window_end)
            lo, hi = period_key(report.window_start), period_key(report.window_end)

            rows: list[SnapshotRow] = []
            for obs in store.snapshot(report.project_id, report.data_version_no):
                if obs.retracted or not (lo <= period_key(obs.period) <= hi):
                    continue
                rows.append(SnapshotRow(
                    measure=obs.measure, period=obs.period, caliber=obs.caliber,
                    value=obs.value, evidence_id=obs.evidence_id,
                ))
            rules_params: dict[str, tuple[float, float]] = {}
            for key, ver in report.pins["rules"].items():
                r = store.get_rule_by_version(key, ver)
                if r is None:
                    raise StateError(f"复算失败：规则 {key} v{ver} 已不存在")
                rules_params[key] = (r.factor, r.offset)
            converted, missing = convert_rows(
                rows, rules_params, report.target_caliber
            )
            if missing:
                raise StateError(f"复算失败：缺少固化规则 {missing}")

            values: dict[str, dict[str, float | None]] = {}
            for r in converted:
                values.setdefault(r.measure, {})[r.period] = r.value

            lines: list[dict] = []
            for line in report.lines:
                version = store.get_indicator_version(
                    line["code"], report.pins["indicators"][line["code"]]
                )
                if version is None:
                    raise StateError(
                        f"复算失败：指标 {line['code']} v{line['version_no']} 已不存在"
                    )
                needed = formula_measures(version.formula)
                scoped = {m: values.get(m, {}) for m in needed}
                result = evaluate(version.formula, version.missing_policy,
                                  periods, scoped)
                lines.append({**line,
                              "value": result.value,
                              "covered_periods": list(result.covered_periods),
                              "missing_periods": list(result.missing_periods),
                              "notes": list(result.notes)})
            input_payload = {
                "project_id": report.project_id,
                "window": [report.window_start, report.window_end],
                "target_caliber": report.target_caliber,
                "pins": report.pins,
                "snapshot": [
                    {"measure": r.measure, "period": r.period,
                     "caliber": r.caliber, "value": r.value,
                     "evidence_id": r.evidence_id}
                    for r in rows
                ],
            }
            input_fp = fingerprint(input_payload)
            result_fp = fingerprint(lines)
        return {
            "report_id": report_id,
            "input_match": input_fp == report.input_fingerprint,
            "result_match": result_fp == report.result_fingerprint,
            "stored_result_fingerprint": report.result_fingerprint,
            "recomputed_result_fingerprint": result_fp,
        }
