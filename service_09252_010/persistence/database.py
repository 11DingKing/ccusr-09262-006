"""SQLite 连接与工作单元。

运行数据库路径由调用方注入（环境变量或测试临时目录），
绝不写入源码目录。写事务统一使用 BEGIN IMMEDIATE 以避免锁升级死锁。
"""
from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS indicators (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS indicator_versions (
    code TEXT NOT NULL REFERENCES indicators(code),
    version_no INTEGER NOT NULL,
    formula_json TEXT NOT NULL,
    missing_policy TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (code, version_no)
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, seq)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    project_id TEXT NOT NULL,
    measure TEXT NOT NULL,
    period TEXT NOT NULL,
    caliber TEXT NOT NULL,
    value REAL,
    retracted INTEGER NOT NULL DEFAULT 0,
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    institution_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_key
    ON observations (project_id, measure, period, caliber);
CREATE TABLE IF NOT EXISTS conversion_rules (
    id TEXT PRIMARY KEY,
    rule_key TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    measure TEXT NOT NULL,
    from_caliber TEXT NOT NULL,
    to_caliber TEXT NOT NULL,
    factor REAL NOT NULL,
    offset REAL NOT NULL,
    status TEXT NOT NULL,
    required_signatories_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (rule_key, version_no)
);
CREATE TABLE IF NOT EXISTS rule_signatures (
    rule_id TEXT NOT NULL REFERENCES conversion_rules(id),
    signatory TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, signatory)
);
CREATE TABLE IF NOT EXISTS rule_key_state (
    rule_key TEXT PRIMARY KEY,
    active_rule_id TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    target_caliber TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    current_step TEXT,
    report_id TEXT,
    error TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    step TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, step)
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    target_caliber TEXT NOT NULL,
    data_version_no INTEGER NOT NULL,
    pins_json TEXT NOT NULL,
    lines_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    result_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    revision_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS report_revisions (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id),
    revision_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    pins_json TEXT NOT NULL,
    lines_json TEXT NOT NULL,
    data_version_no INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    result_fingerprint TEXT NOT NULL,
    correction_reason TEXT,
    requested_by TEXT,
    requested_at TEXT,
    issued_by TEXT,
    issued_at TEXT,
    base_version INTEGER,
    UNIQUE (report_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_report_revisions_report
    ON report_revisions (report_id, revision_no);
CREATE TABLE IF NOT EXISTS report_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL REFERENCES reports(id),
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    institution_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    category TEXT NOT NULL,
    permission TEXT NOT NULL,
    PRIMARY KEY (institution_id, project_id, category, permission)
);
CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id),
    exported_by TEXT NOT NULL,
    exported_at TEXT NOT NULL,
    digest TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开连接并确保模式存在。':memory:' 或文件路径均可。"""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # 轻量迁移：旧库 reports 表补修订版本列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reports)")}
    if "revision_version" not in cols:
        conn.execute(
            "ALTER TABLE reports ADD COLUMN revision_version INTEGER NOT NULL DEFAULT 0"
        )
    return conn


class UnitOfWork(AbstractContextManager):
    """一次写事务；嵌套读取请直接使用 Store 的只读连接。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> "UnitOfWork":
        self.conn = connect(self._path)
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        assert self.conn is not None
        try:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        finally:
            self.conn.close()
            self.conn = None
        return False


class Database:
    """数据库句柄工厂：服务层依赖此类型，路径在组装期注入。"""

    def __init__(self, path: str) -> None:
        self.path = path

    def uow(self) -> UnitOfWork:
        return UnitOfWork(self.path)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path)
        try:
            yield conn
        finally:
            conn.close()
