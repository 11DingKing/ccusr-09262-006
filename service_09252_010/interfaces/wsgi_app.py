"""WSGI JSON API。

认证（边界约定，便于替换为真实 SSO）：
- X-Institution-Id：机构标识，必填
- X-Role：supervisor 为主管单位，缺省为 officer（机构）

幂等：POST /tasks 读取 Idempotency-Key 头或请求体 idempotency_key。
"""
from __future__ import annotations

import json
import traceback
from typing import Callable
from urllib.parse import parse_qs
from wsgiref.util import shift_path_info

from ..domain.errors import DomainError
from ..domain.models import Grant, Principal
from ..persistence.store import Store
from ..container import Container

Json = dict | list | str | int | float | bool | None
Handler = Callable[[Principal, dict, "Context"], tuple[int, Json]]


class Context:
    def __init__(self, env: dict, container: Container) -> None:
        self.env = env
        self.container = container
        self.match: dict[str, str] = {}

    def body(self) -> dict:
        if not hasattr(self, "_body"):
            length = int(self.env.get("CONTENT_LENGTH") or 0)
            raw = self.env["wsgi.input"].read(length) if length else b""
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                from ..domain.errors import ValidationError

                raise ValidationError(f"请求体不是合法 JSON: {exc}") from None
            if not isinstance(parsed, dict):
                from ..domain.errors import ValidationError

                raise ValidationError("请求体必须为 JSON 对象")
            self._body = parsed
        return self._body

    def query(self, name: str) -> str | None:
        values = parse_qs(self.env.get("QUERY_STRING", "")).get(name)
        return values[0] if values else None


class Application:
    def __init__(self, container: Container | None = None) -> None:
        self.container = container or Container()
        self.routes: list[tuple[str, tuple[str, ...], Handler]] = [
            ("POST", ("indicators",), self._register_indicator),
            ("GET", ("indicators",), self._list_indicators),
            ("POST", ("indicators", "{code}", "versions"), self._indicator_version),
            ("GET", ("indicators", "{code}"), self._get_indicator),
            ("POST", ("evidence",), self._register_evidence),
            ("POST", ("projects", "{pid}", "imports"), self._import_batch),
            ("GET", ("projects", "{pid}", "versions", "{ver}", "diff"),
             self._version_diff),
            ("POST", ("rules",), self._create_rule),
            ("GET", ("rules",), self._list_rules),
            ("POST", ("rules", "{rule_id}", "signatures"), self._sign_rule),
            ("POST", ("rules", "rollback",), self._rollback_rule),
            ("GET", ("rules", "{rule_id}"), self._get_rule),
            ("POST", ("tasks",), self._submit_task),
            ("POST", ("tasks", "{task_id}", "run"), self._run_task),
            ("GET", ("tasks", "{task_id}"), self._get_task),
            ("GET", ("reports",), self._list_reports),
            ("POST", ("reports", "{report_id}", "review"), self._review_report),
            ("POST", ("reports", "{report_id}", "revisions"),
             self._request_revision),
            ("GET", ("reports", "{report_id}", "revisions"),
             self._list_revisions),
            ("GET", ("reports", "{report_id}", "history"),
             self._revision_history),
            ("GET", ("reports", "{report_id}", "history", "{ver}"),
             self._revision_history_version),
            ("POST", ("revisions", "{revision_id}", "decision"),
             self._decide_revision),
            ("POST", ("reports", "{report_id}", "reverify"), self._reverify),
            ("POST", ("reports", "{report_id}", "exports"), self._export_report),
            ("GET", ("reports", "{report_id}"), self._get_report),
            ("POST", ("grants",), self._create_grant),
        ]

    def __call__(self, env: dict, start_response) -> list[bytes]:
        try:
            principal = self._principal(env)
            ctx = Context(env, self.container)
            status, payload = self._dispatch(env, principal, ctx)
        except DomainError as exc:
            status, payload = exc.http_status, {
                "error": exc.code,
                "message": exc.message,
                "detail": exc.detail,
            }
        except Exception as exc:  # noqa: BLE001 — 边界兜底，避免堆栈外泄
            traceback.print_exc()
            status, payload = 500, {
                "error": "internal_error",
                "message": str(exc),
            }
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        start_response(
            f"{status} {_STATUS_TEXTS.get(status, 'Unknown')}",
            [("Content-Type", "application/json; charset=utf-8"),
             ("Content-Length", str(len(body)))],
        )
        return [body]

    @staticmethod
    def _header(env: dict, name: str) -> str | None:
        """读取头并还原 UTF-8（CGI 服务器按 Latin-1 解码原始字节）。"""
        raw = env.get(name)
        if raw is None:
            return None
        try:
            return raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return raw

    @classmethod
    def _principal(cls, env: dict) -> Principal:
        from ..domain.errors import PermissionDeniedError, ValidationError

        institution = cls._header(env, "HTTP_X_INSTITUTION_ID")
        if not institution:
            raise PermissionDeniedError("缺少 X-Institution-Id 头")
        role = env.get("HTTP_X_ROLE", "officer")
        if role not in ("supervisor", "officer"):
            raise ValidationError("X-Role 仅支持 supervisor/officer")
        return Principal(institution_id=institution, role=role)

    def _dispatch(self, env: dict, principal: Principal, ctx: Context):
        method = env["REQUEST_METHOD"].upper()
        segments: list[str] = []
        while True:
            seg = shift_path_info(env)
            if seg is None:
                break
            segments.append(seg)
        for route_method, route_segments, handler in self.routes:
            if route_method != method or len(route_segments) != len(segments):
                continue
            match: dict[str, str] = {}
            for actual, expected in zip(segments, route_segments):
                if expected.startswith("{") and expected.endswith("}"):
                    match[expected[1:-1]] = actual
                elif actual != expected:
                    break
            else:
                ctx.match = match
                return handler(principal, ctx.body(), ctx)
        raise _not_found(f"无此路由: {method} /{'/'.join(segments)}")

    # ---- 指标 ----
    def _register_indicator(self, p: Principal, body: dict, ctx: Context):
        result = ctx.container.indicators.register(
            p, code=body["code"], name=body["name"], category=body["category"],
            unit=body.get("unit", ""), formula=body["formula"],
            missing_policy=body.get("missing_policy", "skip"),
        )
        return 201, result

    def _indicator_version(self, p: Principal, body: dict, ctx: Context):
        result = ctx.container.indicators.add_version(
            p, ctx.match["code"], formula=body["formula"],
            missing_policy=body.get("missing_policy", "skip"),
        )
        return 201, result

    def _get_indicator(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.indicators.get(ctx.match["code"])

    def _list_indicators(self, p: Principal, body: dict, ctx: Context):
        with ctx.container.db.read() as conn:
            data = [
                {"code": i.code, "name": i.name, "category": i.category,
                 "unit": i.unit}
                for i in Store(conn).list_indicators()
            ]
        return 200, {"indicators": data}

    # ---- 证据 / 导入 ----
    def _register_evidence(self, p: Principal, body: dict, ctx: Context):
        result = ctx.container.imports.register_evidence(
            p, project_id=body["project_id"], kind=body["kind"],
            uri=body["uri"], sha256=body["sha256"],
        )
        return 201, result

    def _import_batch(self, p: Principal, body: dict, ctx: Context):
        result = ctx.container.imports.import_batch(
            p, ctx.match["pid"], records=body["records"],
            reason=body.get("reason", ""),
        )
        return 201, result

    def _version_diff(self, p: Principal, body: dict, ctx: Context):
        against_raw = ctx.query("against")
        against = int(against_raw) if against_raw else None
        result = ctx.container.imports.version_diff(
            ctx.match["pid"], int(ctx.match["ver"]), against
        )
        return 200, result

    # ---- 规则会签 ----
    def _create_rule(self, p: Principal, body: dict, ctx: Context):
        result = ctx.container.calibers.create_rule(
            p, measure=body["measure"], from_caliber=body["from_caliber"],
            to_caliber=body["to_caliber"], factor=body["factor"],
            offset=body.get("offset", 0.0),
            required_signatories=body.get("required_signatories"),
        )
        return 201, result

    def _list_rules(self, p: Principal, body: dict, ctx: Context):
        return 200, {"rules": ctx.container.calibers.list_rules()}

    def _sign_rule(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.calibers.sign(
            p, ctx.match["rule_id"], signatory=body["signatory"]
        )

    def _rollback_rule(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.calibers.rollback(
            p, body["rule_key"], to_version=int(body["to_version"])
        )

    def _get_rule(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.calibers.get_rule(ctx.match["rule_id"])

    # ---- 计算 ----
    def _submit_task(self, p: Principal, body: dict, ctx: Context):
        key = self._header(ctx.env, "HTTP_IDEMPOTENCY_KEY") or body.get("idempotency_key")
        if not key:
            from ..domain.errors import ValidationError

            raise ValidationError("计算请求必须提供 idempotency_key")
        result = ctx.container.calculation.submit(
            p, project_id=body["project_id"], window_start=body["window_start"],
            window_end=body["window_end"], target_caliber=body["target_caliber"],
            idempotency_key=key,
        )
        return 201, result

    def _run_task(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.calculation.run(ctx.match["task_id"])

    def _get_task(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.calculation.get_task(ctx.match["task_id"])

    # ---- 报告 / 复核 / 导出 ----
    def _list_reports(self, p: Principal, body: dict, ctx: Context):
        from ..domain.errors import ValidationError

        project_id = ctx.query("project_id")
        if not project_id:
            raise ValidationError("列表查询需提供 project_id 查询参数")
        return 200, {"reports": ctx.container.calculation.list_reports(
            p, project_id)}

    def _get_report(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.calculation.get_report(p, ctx.match["report_id"])

    def _review_report(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.review.review(
            p, ctx.match["report_id"], approve=bool(body["approve"]),
            reason=body.get("reason", ""),
        )

    # ---- 已签报告修订 ----
    def _request_revision(self, p: Principal, body: dict, ctx: Context):
        from ..domain.errors import ValidationError

        if "expected_version" not in body:
            raise ValidationError(
                "更正申请必须携带 expected_version（所基于的签发版本号）"
            )
        result = ctx.container.revisions.request_correction(
            p, ctx.match["report_id"],
            expected_version=int(body["expected_version"]),
            lines=body["lines"], reason=body.get("reason", ""),
        )
        return 201, result

    def _list_revisions(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.revisions.list_requests(
            p, ctx.match["report_id"]
        )

    def _revision_history(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.revisions.history(
            p, ctx.match["report_id"]
        )

    def _revision_history_version(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.revisions.history(
            p, ctx.match["report_id"], int(ctx.match["ver"])
        )

    def _decide_revision(self, p: Principal, body: dict, ctx: Context):
        expected = body.get("expected_version")
        return 200, ctx.container.revisions.decide(
            p, ctx.match["revision_id"], approve=bool(body["approve"]),
            expected_version=int(expected) if expected is not None else None,
            reason=body.get("reason", ""),
        )

    def _reverify(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.calculation.reverify(
            p, ctx.match["report_id"]
        )

    def _export_report(self, p: Principal, body: dict, ctx: Context):
        return 200, ctx.container.exports.export(p, ctx.match["report_id"])

    # ---- 授权管理 ----
    def _create_grant(self, p: Principal, body: dict, ctx: Context):
        from ..domain.errors import PermissionDeniedError, ValidationError
        from ..services.access import PERMISSIONS, grant_access

        if not p.is_supervisor:
            raise PermissionDeniedError("仅主管单位可配置授权")
        permission = body.get("permission")
        if permission not in PERMISSIONS:
            raise ValidationError("permission 非法")
        with ctx.container.db.uow() as uow:
            grant_access(
                uow.conn,
                Grant(body["institution_id"], body["project_id"],
                      body.get("category", "*"), permission),
            )
        return 201, {"granted": True}


def _not_found(message: str):
    from ..domain.errors import NotFoundError

    return NotFoundError(message)


_STATUS_TEXTS = {
    200: "OK", 201: "Created", 400: "Bad Request", 403: "Forbidden",
    404: "Not Found", 409: "Conflict", 422: "Unprocessable Entity",
    500: "Internal Server Error",
}


def make_app(db_path: str | None = None) -> Application:
    """WSGI 工厂。"""
    return Application(Container(db_path))
