"""唯一真实依赖组装入口；核心模块不直接实例化外部 Adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from davinci_app.adapters.codex_app_server import CodexAppServerSkillRuntime, CodexFrameEvidenceAnalyzer
from davinci_app.adapters.funasr_transcriber import FunASRTranscriberAdapter
from davinci_app.adapters.openai_multimodal import OpenAICompatibleMultimodalAdapter
from davinci_app.config import AppConfig, assert_supported_runtime
from davinci_app.creative.catalog import CreativeCatalog
from davinci_app.editorial.pipeline import ProfessionalPipeline
from davinci_app.execution.compiler import EngineSmokeCompiler
from davinci_app.execution.professional_compiler import ProfessionalExecutionCompiler
from davinci_app.media.evidence import EvidenceBuilder, MediaEvidenceRuntime
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
    creative_catalog = CreativeCatalog(
        config.creative_catalog_database,
        config.creative_certified_root,
        config.creative_cache_root,
    )
    creative_catalog.initialize()
    projects = ProjectService(config, database, UploadValidator())
    tasks = TaskQueue(database)
    evidence_builder = EvidenceBuilder()
    # 外部实现只在这里组装；核心工作流仍只依赖 Port 合同。
    transcriber = FunASRTranscriberAdapter(config)
    multimodal = OpenAICompatibleMultimodalAdapter(config)
    skill_runtime = CodexAppServerSkillRuntime(config, projects)
    frame_analyzer = CodexFrameEvidenceAnalyzer(config, projects)
    professional_pipeline = ProfessionalPipeline(
        MediaEvidenceRuntime(config, transcriber, multimodal, frame_analyzer, evidence_builder=evidence_builder),
        skill_runtime,
        creative_catalog,
    )
    worker = WorkflowWorker(
        projects,
        tasks,
        smoke_compiler=EngineSmokeCompiler(config.resolve_workspace_project),
        professional_pipeline=professional_pipeline,
        professional_compiler=ProfessionalExecutionCompiler(config),
        evidence_builder=evidence_builder,
    )
    return ApplicationContainer(config, database, projects, tasks, worker)
