"""授权粒度：机构只能在被授权的项目 × 指标类别 × 权限范围内操作。"""
from __future__ import annotations

import sqlite3

from ..domain.errors import PermissionDeniedError
from ..domain.models import Grant, Principal
from ..persistence.store import Store

PERMISSIONS = ("import", "view", "calculate", "review", "export")


class AccessPolicy:
    """主管单位（supervisor）拥有全部范围；机构按授权记录判定。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    def check(self, principal: Principal, project_id: str, category: str,
              permission: str) -> bool:
        if principal.is_supervisor:
            return True
        return self.store.has_grant(
            principal.institution_id, project_id, category, permission
        )

    def require(self, principal: Principal, project_id: str, category: str,
                permission: str) -> None:
        if not self.check(principal, project_id, category, permission):
            raise PermissionDeniedError(
                f"机构 {principal.institution_id} 未获授权："
                f"项目={project_id} 类别={category} 权限={permission}"
            )

    def granted_categories(self, principal: Principal, project_id: str,
                           permission: str, all_categories: list[str]) -> list[str]:
        """返回主体在某项目下可见的指标类别集合。"""
        if principal.is_supervisor:
            return sorted(set(all_categories))
        granted = [
            c for c in set(all_categories)
            if self.store.has_grant(principal.institution_id, project_id, c, permission)
        ]
        return sorted(granted)


def grant_access(conn: sqlite3.Connection, grant: Grant) -> None:
    Store(conn).add_grant(grant)
