"""指标定义服务：登记与版本化更新。旧版本永远保留，历史报告不被改写。"""
from __future__ import annotations

from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..domain.formulas import validate_formula
from ..domain.models import Indicator, IndicatorVersion, MissingPolicy, Principal
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock


class IndicatorService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def register(self, principal: Principal, *, code: str, name: str,
                 category: str, unit: str, formula: dict,
                 missing_policy: str = "skip") -> dict:
        """登记新指标（首个版本为 v1）。仅主管单位可登记。"""
        if not principal.is_supervisor:
            from ..domain.errors import PermissionDeniedError

            raise PermissionDeniedError("仅主管单位可登记指标定义")
        validate_formula(formula)
        policy = self._parse_policy(missing_policy)
        with self.db.uow() as uow:
            store = Store(uow.conn)
            if store.get_indicator(code) is not None:
                raise ConflictError(f"指标已存在: {code}")
            now = self.clock.now()
            store.add_indicator(Indicator(code, name, category, unit, now))
            store.add_indicator_version(
                IndicatorVersion(code, 1, formula, policy, now)
            )
        return {"code": code, "version_no": 1}

    def add_version(self, principal: Principal, code: str, *, formula: dict,
                    missing_policy: str = "skip") -> dict:
        """登记指标的新版本；旧版本与既有报告不受影响。"""
        if not principal.is_supervisor:
            from ..domain.errors import PermissionDeniedError

            raise PermissionDeniedError("仅主管单位可更新指标定义")
        validate_formula(formula)
        policy = self._parse_policy(missing_policy)
        with self.db.uow() as uow:
            store = Store(uow.conn)
            if store.get_indicator(code) is None:
                raise NotFoundError(f"指标不存在: {code}")
            latest = store.latest_indicator_version(code)
            assert latest is not None
            version_no = latest.version_no + 1
            store.add_indicator_version(
                IndicatorVersion(code, version_no, formula, policy, self.clock.now())
            )
        return {"code": code, "version_no": version_no}

    def get(self, code: str) -> dict:
        with self.db.read() as conn:
            store = Store(conn)
            indicator = store.get_indicator(code)
            if indicator is None:
                raise NotFoundError(f"指标不存在: {code}")
            versions = store.list_indicator_versions(code)
        return {
            "code": indicator.code,
            "name": indicator.name,
            "category": indicator.category,
            "unit": indicator.unit,
            "versions": [
                {
                    "version_no": v.version_no,
                    "formula": v.formula,
                    "missing_policy": v.missing_policy.value,
                    "created_at": v.created_at,
                }
                for v in versions
            ],
        }

    @staticmethod
    def _parse_policy(raw: str) -> MissingPolicy:
        try:
            return MissingPolicy(raw)
        except ValueError:
            raise ValidationError(
                f"未知缺失值策略: {raw!r}，可选 skip/zero/fail"
            ) from None
