"""组装根：把数据库路径、时钟、标识生成器与各应用服务装配在一起。

数据库路径必须显式注入（环境变量 APP_DB_PATH），默认使用进程临时目录，
绝不回落到源码目录。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .persistence.database import Database
from .ports import SystemClock, SystemIdGenerator
from .services.calibers import CaliberService
from .services.calculation import CalculationService
from .services.export import ExportService
from .services.imports import ImportService
from .services.indicators import IndicatorService
from .services.review import ReviewService


class Container:
    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.environ.get("APP_DB_PATH")
        if not db_path:
            db_path = str(Path(tempfile.gettempdir()) / "service_09252_010" / "app.db")
        self.db = Database(db_path)
        self.clock = SystemClock()
        self.ids = SystemIdGenerator()
        self.indicators = IndicatorService(self.db, self.clock)
        self.imports = ImportService(self.db, self.clock, self.ids)
        self.calibers = CaliberService(self.db, self.clock, self.ids)
        self.calculation = CalculationService(self.db, self.clock, self.ids)
        self.review = ReviewService(self.db, self.clock)
        self.exports = ExportService(self.db, self.clock, self.ids)
