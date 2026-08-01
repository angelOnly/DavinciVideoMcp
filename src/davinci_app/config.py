"""本机配置与运行环境合同。敏感值只读取，不记录或回传。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


EXPECTED_ENVIRONMENT = "unofficial-davinci-mcp-win"
EXPECTED_PYTHON = (3, 10, 20)


class RuntimeEnvironmentError(RuntimeError):
    """当前解释器不是产品允许使用的既有 Conda 环境。"""


@dataclass(frozen=True)
class AppConfig:
    repository_root: Path
    workspace_root: Path
    data_root: Path
    projects_root: Path
    creative_cache_root: Path
    product_database: Path
    creative_catalog_database: Path
    expected_conda_environment: str
    expected_python_version: tuple[int, int, int]
    multimodal_model: str
    multimodal_base_url: str | None
    funasr_manifest: Path
    creative_raw_root: Path
    creative_certified_root: Path
    resolve_workspace_project: str

    @classmethod
    def from_environment(cls, repository_root: Path | None = None) -> "AppConfig":
        root = repository_root or Path(__file__).resolve().parents[2]
        root = root.resolve()
        workspace = _configured_path("WORKSPACE_ROOT", root / "workspace", root)
        data_root = _configured_path("APP_DATA_ROOT", workspace / "data", root)
        cache_root = _configured_path("CREATIVE_CACHE_ROOT", workspace / "creative-cache", root)
        return cls(
            repository_root=root,
            workspace_root=workspace,
            data_root=data_root,
            projects_root=workspace / "projects",
            creative_cache_root=cache_root,
            product_database=data_root / "product.db",
            creative_catalog_database=data_root / "creative_catalog.db",
            expected_conda_environment=os.environ.get(
                "EXPECTED_CONDA_ENV", EXPECTED_ENVIRONMENT
            ),
            expected_python_version=_parse_python_version(
                os.environ.get("EXPECTED_PYTHON_VERSION", ".".join(map(str, EXPECTED_PYTHON)))
            ),
            multimodal_model=os.environ.get("MULTIMODAL_MODEL", "gemini-3.5-flash"),
            multimodal_base_url=os.environ.get("MULTIMODAL_BASE_URL") or None,
            funasr_manifest=_configured_path(
                "FUNASR_MANIFEST", root / "models" / "manifest.yaml", root
            ),
            creative_raw_root=Path(
                os.environ.get("CREATIVE_RAW_ROOT", r"C:\\Users\\13222\\Nextcloud\\达芬奇素材")
            ),
            creative_certified_root=Path(
                os.environ.get(
                    "CREATIVE_CERTIFIED_ROOT", r"C:\\Users\\13222\\Nextcloud\\达芬奇认证素材库"
                )
            ),
            resolve_workspace_project=os.environ.get("RESOLVE_WORKSPACE_PROJECT", "DavinciMcp_Workspace"),
        )

    def ensure_directories(self) -> None:
        for directory in (
            self.workspace_root,
            self.data_root,
            self.projects_root,
            self.creative_cache_root / "objects",
        ):
            directory.mkdir(parents=True, exist_ok=True)


def _configured_path(variable: str, default: Path, repository_root: Path) -> Path:
    raw = os.environ.get(variable)
    candidate = Path(raw) if raw else default
    if not candidate.is_absolute():
        candidate = repository_root / candidate
    return candidate.resolve()


def _parse_python_version(value: str) -> tuple[int, int, int]:
    try:
        parsed = tuple(int(item) for item in value.split("."))
    except ValueError as exc:
        raise RuntimeEnvironmentError("EXPECTED_PYTHON_VERSION 必须是 major.minor.patch") from exc
    if len(parsed) != 3:
        raise RuntimeEnvironmentError("EXPECTED_PYTHON_VERSION 必须是 major.minor.patch")
    return parsed  # type: ignore[return-value]


def assert_supported_runtime(config: AppConfig) -> None:
    """严格阻止脚本在错误环境中启动，不创建任何替代环境。"""
    active_environment = os.environ.get("CONDA_DEFAULT_ENV")
    actual_version = sys.version_info[:3]
    errors: list[str] = []
    if active_environment != config.expected_conda_environment:
        errors.append(
            f"CONDA_DEFAULT_ENV 应为 {config.expected_conda_environment!r}，实际为 {active_environment!r}"
        )
    if actual_version != config.expected_python_version:
        expected = ".".join(map(str, config.expected_python_version))
        actual = ".".join(map(str, actual_version))
        errors.append(f"Python 应为 {expected}，实际为 {actual}")
    if errors:
        message = "；".join(errors)
        raise RuntimeEnvironmentError(
            f"运行环境不符合合同：{message}。请执行 conda activate {config.expected_conda_environment}。"
        )


def runtime_report(config: AppConfig) -> dict[str, str | bool]:
    return {
        "expected_conda_environment": config.expected_conda_environment,
        "active_conda_environment": os.environ.get("CONDA_DEFAULT_ENV", ""),
        "python_executable": sys.executable,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "matches_contract": (
            os.environ.get("CONDA_DEFAULT_ENV") == config.expected_conda_environment
            and sys.version_info[:3] == config.expected_python_version
        ),
    }
