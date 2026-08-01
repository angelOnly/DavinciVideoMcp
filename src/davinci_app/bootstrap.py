"""唯一真实依赖组装入口；核心模块不直接实例化外部 Adapter。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from davinci_app.config import AppConfig, assert_supported_runtime
from davinci_app.execution.compiler import TestCutCompiler
from davinci_app.media.validation import UploadValidator
from davinci_app.persistence import ProductDatabase
from davinci_app.project.service import ProjectService
from davinci_app.workflow.queue import TaskQueue
from davinci_app.workflow.worker import WorkflowWorker


@dataclass(frozen=True)
class ApplicationContainer:
    config: AppConfig
    database: ProductDatabase
    projects: ProjectService
    tasks: TaskQueue
    worker: WorkflowWorker


def bootstrap(repository_root: Path | None = None) -> ApplicationContainer:
    config = AppConfig.from_environment(repository_root)
    assert_supported_runtime(config)
    config.ensure_directories()
    database = ProductDatabase(config.product_database)
    database.initialize()
    _initialize_creative_catalog(config.creative_catalog_database)
    projects = ProjectService(config, database, UploadValidator())
    tasks = TaskQueue(database)
    worker = WorkflowWorker(projects, tasks, compiler=TestCutCompiler(config.resolve_workspace_project))
    return ApplicationContainer(config, database, projects, tasks, worker)


def _initialize_creative_catalog(path: Path) -> None:
    """先创建可重建的空目录数据库，不将任何媒体内容写入其中。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS catalog_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
