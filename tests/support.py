"""测试支撑：确定性端口实现、服务装配与业务种子数据。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from service_09252_010.domain.models import Grant, Principal
from service_09252_010.persistence.database import Database
from service_09252_010.persistence.store import Store
from service_09252_010.services.access import grant_access
from service_09252_010.services.calibers import CaliberService
from service_09252_010.services.calculation import CalculationService
from service_09252_010.services.export import ExportService
from service_09252_010.services.imports import ImportService
from service_09252_010.services.indicators import IndicatorService
from service_09252_010.services.review import ReviewService
from service_09252_010.services.revisions import RevisionService

SUPERVISOR = Principal(institution_id="主管单位", role="supervisor")
INST_A = Principal(institution_id="机构A", role="officer")
INST_B = Principal(institution_id="机构B", role="officer")

PROJECT = "proj-ivc-01"
TARGET = "CN-STD"


class FixedClock:
    """确定性时钟：每次调用前进 1 秒。"""

    def __init__(self) -> None:
        self._base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._ticks = 0

    def now(self) -> str:
        self._ticks += 1
        return (self._base + timedelta(seconds=self._ticks)).isoformat(
            timespec="microseconds"
        )


class SeqIds:
    """确定性标识生成器。"""

    def __init__(self) -> None:
        self._n = 0

    def new_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:04d}"


class Rig:
    """一套围绕临时数据库装配好的服务。数据库文件位于系统临时目录。"""

    def __init__(self, tmpdir: str) -> None:
        self.db = Database(os.path.join(tmpdir, "test.db"))
        self.clock = FixedClock()
        self.ids = SeqIds()
        self.indicators = IndicatorService(self.db, self.clock)
        self.imports = ImportService(self.db, self.clock, self.ids)
        self.calibers = CaliberService(self.db, self.clock, self.ids)
        self.calculation = CalculationService(self.db, self.clock, self.ids)
        self.review = ReviewService(self.db, self.clock, self.ids)
        self.revisions = RevisionService(self.db, self.clock, self.ids,
                                         self.calculation)
        self.exports = ExportService(self.db, self.clock, self.ids)

    def grant(self, institution: str, project: str = PROJECT,
              category: str = "*", permission: str = "view") -> None:
        with self.db.uow() as uow:
            grant_access(uow.conn, Grant(institution, project, category,
                                         permission))

    def grants_for(self, institution: str) -> list[Grant]:
        with self.db.read() as conn:
            return Store(conn).list_grants(institution)


class RigTestCase(unittest.TestCase):
    """每个用例一份全新数据库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="svc09252-test-")
        self.rig = Rig(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- 常用种子 ----
    def seed_indicators(self, missing_policy: str = "skip") -> None:
        self.rig.indicators.register(
            SUPERVISOR, code="enrollment_total", name="招生总数",
            category="招生", unit="人",
            formula={"type": "sum", "measure": "enrollment_count"},
            missing_policy=missing_policy,
        )
        self.rig.indicators.register(
            SUPERVISOR, code="employment_rate", name="就业率",
            category="就业", unit="%",
            formula={"type": "ratio", "numerator": "employed_count",
                     "denominator": "graduate_count", "scale": 100},
            missing_policy=missing_policy,
        )
        self.rig.indicators.register(
            SUPERVISOR, code="trained_teachers", name="师资培养数",
            category="师资培养", unit="人",
            formula={"type": "sum", "measure": "trained_teacher_count"},
            missing_policy=missing_policy,
        )

    def seed_evidence(self, suffix: str = "a") -> str:
        return self.rig.imports.register_evidence(
            SUPERVISOR, project_id=PROJECT, kind="统计年报",
            uri=f"s3://evidence/{suffix}.pdf", sha256="f" * 64,
        )["evidence_id"]

    def seed_rule(self, factor: float = 1.0, offset: float = 0.0,
                  measure: str = "enrollment_count",
                  from_caliber: str = "DE-DUAL") -> str:
        """登记并完成会签一条换算规则，返回 rule_id。"""
        rule_id = self.rig.calibers.create_rule(
            SUPERVISOR, measure=measure, from_caliber=from_caliber,
            to_caliber=TARGET, factor=factor, offset=offset,
        )["rule_id"]
        for signatory in ("supervisor", "partner_institution", "expert_panel"):
            self.rig.calibers.sign(SUPERVISOR, rule_id, signatory=signatory)
        return rule_id

    def seed_import(self, records: list[dict], evidence_id: str,
                    reason: str = "首批数据") -> dict:
        return self.rig.imports.import_batch(
            SUPERVISOR, PROJECT, records=records, reason=reason,
        )

    def calculate(self, key: str = "idem-1", window: tuple[str, str] =
                  ("2024-01", "2024-12"), principal: Principal = INST_A) -> str:
        """提交并执行计算，返回 report_id。"""
        self.rig.calculation.submit(
            principal, project_id=PROJECT, window_start=window[0],
            window_end=window[1], target_caliber=TARGET,
            idempotency_key=key,
        )
        with self.rig.db.read() as conn:
            task = Store(conn).task_by_key(key)
            assert task is not None
            task_id = task.id
        result = self.rig.calculation.run(task_id)
        return result["report_id"]

    def grant_inst_a_all(self) -> None:
        for permission in ("import", "view", "calculate", "review", "export"):
            self.rig.grant(INST_A.institution_id, permission=permission)
