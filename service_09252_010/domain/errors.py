"""领域错误类型。

所有可预期的业务失败都抛出 DomainError 子类，接口层据此映射 HTTP 状态码。
"""
from __future__ import annotations


class DomainError(Exception):
    """业务错误基类。code 用于接口层稳定识别。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class PermissionDeniedError(DomainError):
    code = "permission_denied"
    http_status = 403


class StateError(DomainError):
    """聚合状态不允许当前操作（如对已定稿报告再计算）。"""

    code = "state_error"
    http_status = 409


class MissingDataError(DomainError):
    """缺失值策略为 fail 时，窗口内数据不足。"""

    code = "missing_data"
    http_status = 422
