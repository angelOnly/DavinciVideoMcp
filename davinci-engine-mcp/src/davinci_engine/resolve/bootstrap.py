"""安全配置 Resolve Scripting Runtime；原生导入前必须先完成子进程探测。"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BootstrapStatus:
    available: bool
    program_directory: str
    scripting_directory: str
    modules_directory: str
    script_library: str
    python_home: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def configure() -> BootstrapStatus:
    """只设置本进程及其子进程环境，不连接、更不写入 Resolve。"""
    program_directory = Path(os.environ.get("RESOLVE_PROGRAM_DIR", r"C:\Program Files\Blackmagic Design\DaVinci Resolve"))
    scripting_directory = Path(
        os.environ.get(
            "RESOLVE_SCRIPT_API",
            r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
        )
    )
    modules_directory = Path(os.environ.get("RESOLVE_SCRIPT_PATH", scripting_directory / "Modules"))
    script_library = Path(os.environ.get("RESOLVE_SCRIPT_LIB", program_directory / "fusionscript.dll"))
    # Conda 环境必须使用 sys.prefix，而不是 Anaconda base 的 sys.base_prefix。
    python_home = Path(sys.prefix).resolve()
    required = [program_directory, scripting_directory, modules_directory, script_library]
    missing = [str(item) for item in required if not item.exists()]
    if missing:
        return BootstrapStatus(
            available=False,
            program_directory=str(program_directory),
            scripting_directory=str(scripting_directory),
            modules_directory=str(modules_directory),
            script_library=str(script_library),
            python_home=str(python_home),
            reason=f"未找到 Resolve Scripting 运行时：{'; '.join(missing)}",
        )
    os.environ["RESOLVE_SCRIPT_API"] = str(scripting_directory)
    os.environ["RESOLVE_SCRIPT_LIB"] = str(script_library)
    os.environ["FUSION_PYTHON3_HOME"] = str(python_home)
    current_python_path = os.environ.get("PYTHONPATH", "")
    if str(modules_directory) not in current_python_path.split(os.pathsep):
        os.environ["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(modules_directory), current_python_path) if item
        )
    if str(modules_directory) not in sys.path:
        sys.path.insert(0, str(modules_directory))
    if os.name == "nt":
        for directory in (python_home, python_home / "Library" / "bin", program_directory):
            if directory.exists():
                try:
                    os.add_dll_directory(str(directory))
                except OSError:
                    pass
        for candidate in (python_home / "python3.dll", python_home / "python310.dll"):
            if candidate.exists():
                try:
                    ctypes.WinDLL(str(candidate))
                    break
                except OSError:
                    continue
    return BootstrapStatus(
        available=True,
        program_directory=str(program_directory),
        scripting_directory=str(scripting_directory),
        modules_directory=str(modules_directory),
        script_library=str(script_library),
        python_home=str(python_home),
    )


def probe_import(timeout_seconds: int = 60) -> dict[str, Any]:
    """在一次性子进程中导入 fusionscript，避免原生崩溃拖垮 Engine。"""
    configured = configure()
    if not configured.available:
        return {"safe": False, "connected": False, "bootstrap": configured.to_dict(), "reason": configured.reason}
    completed: subprocess.CompletedProcess[str]
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "davinci_engine.resolve.probe"],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "safe": False,
            "connected": False,
            "bootstrap": configured.to_dict(),
            "reason": f"Resolve 原生模块安全探测失败：{exc}",
        }
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        payload = {"safe": False, "connected": False, "reason": completed.stderr.strip()[-500:] or "探测进程异常退出"}
    payload["bootstrap"] = configured.to_dict()
    return payload

