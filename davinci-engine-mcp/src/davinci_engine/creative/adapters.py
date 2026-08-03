"""按明确机制实现的创意素材 Adapter。

这里不扫描采购库、不选择素材，也不执行任意 Fusion/Lua。上游只能传入已经完成
本地化和哈希校验的单个缓存文件；Adapter 再完成部署、应用、读回或渲染验证中的
某一项。是否能自动进入项目仍由五步认证结果和 Compiler Mapping 决定。
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from davinci_engine.analysis.ffmpeg_runtime import FFmpegRuntimeError, stream_summary, verify_render
from davinci_engine.common import sha256_file


DIRECT_MEDIA_MECHANISMS = frozenset({"audio_asset", "image_asset", "video_asset", "video_overlay"})
LUT_MECHANISMS = frozenset({"lut_3d"})
FONT_MECHANISMS = frozenset({"font_file"})
FUSION_EFFECT_MECHANISMS = frozenset({"fusion_effect"})
SUPPORTED_MECHANISMS = DIRECT_MEDIA_MECHANISMS | LUT_MECHANISMS | FONT_MECHANISMS | FUSION_EFFECT_MECHANISMS


class CreativeAdapterError(RuntimeError):
    """能力不满足某个受控 Adapter 合同。"""


@dataclass(frozen=True)
class AdapterPreflight:
    """不写入系统或 Resolve 的静态准入结果。"""

    mechanism: str
    ready_for_live_certification: bool
    details: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class AdapterDeployment:
    """安装型能力的受管部署身份；不等同于认证成功。"""

    mechanism: str
    installed_path: str | None
    installed_relative_path: str | None
    source_hash: str
    deployed_hash: str
    requires_resolve_restart: bool = False

    def to_evidence(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "installed_path": self.installed_path,
            "installed_relative_path": self.installed_relative_path,
            "source_hash": self.source_hash,
            "deployed_hash": self.deployed_hash,
            "requires_resolve_restart": self.requires_resolve_restart,
        }


class CapabilityAdapter(Protocol):
    """每种机制都遵循同一六步职责，但实现不共享未知模板逻辑。"""

    mechanisms: frozenset[str]

    def probe(self, asset_path: Path, constraints: dict[str, Any]) -> AdapterPreflight: ...

    def install_or_deploy(
        self, capability_id: str, asset_path: Path, constraints: dict[str, Any], context: dict[str, Any]
    ) -> AdapterDeployment: ...

    def validate(
        self, asset_path: Path, content_hash: str, constraints: dict[str, Any]
    ) -> AdapterPreflight: ...

    def apply(self, context: dict[str, Any]) -> dict[str, Any]: ...

    def inspect(self, context: dict[str, Any]) -> dict[str, Any]: ...

    def verify_render(self, render_path: Path, *, expected_duration_seconds: float | None = None) -> dict[str, Any]: ...


class DirectMediaAdapter:
    """处理可直接进入 Resolve 媒体池的音频、图片、视频和视频叠加。"""

    mechanisms = DIRECT_MEDIA_MECHANISMS

    def probe(self, asset_path: Path, constraints: dict[str, Any]) -> AdapterPreflight:
        try:
            summary = stream_summary(asset_path)
        except (FFmpegRuntimeError, OSError) as exc:
            return AdapterPreflight("direct_media", False, reason=f"无法探测直接媒体：{exc}")
        mechanism = str(constraints.get("mechanism") or "")
        if mechanism and mechanism not in self.mechanisms:
            return AdapterPreflight(mechanism, False, reason="直接媒体机制与约束不匹配。")
        expected = mechanism or _mechanism_from_summary(summary)
        if expected == "audio_asset" and not summary.get("has_audio"):
            return AdapterPreflight(expected, False, reason="音频能力缺少可解码音频流。")
        if expected != "audio_asset" and not summary.get("has_video"):
            return AdapterPreflight(expected, False, reason="视觉能力缺少可解码视频或图片流。")
        return AdapterPreflight(
            expected,
            True,
            details={"adapter": "direct_media", "summary": summary, "deployment": "not_required"},
        )

    def install_or_deploy(
        self, capability_id: str, asset_path: Path, constraints: dict[str, Any], context: dict[str, Any]
    ) -> AdapterDeployment:
        # 运行时媒体只需由上游本地化到内容寻址缓存，不能复制到全局 Resolve 目录。
        source_hash = sha256_file(asset_path)
        return AdapterDeployment("direct_media", None, None, source_hash, source_hash)

    def validate(self, asset_path: Path, content_hash: str, constraints: dict[str, Any]) -> AdapterPreflight:
        if not asset_path.is_file() or sha256_file(asset_path) != content_hash:
            return AdapterPreflight(str(constraints.get("mechanism") or "direct_media"), False, reason="本地缓存媒体身份不一致。")
        return self.probe(asset_path, constraints)

    def apply(self, context: dict[str, Any]) -> dict[str, Any]:
        """按精确帧和轨道放置已导入媒体；实际读回由 Executor 统一完成。"""

        media_pool = _required_context(context, "media_pool")
        media_item = _required_context(context, "media_item")
        operation = _required_context(context, "operation")
        media_type = int(operation["media_type"])
        payload = {
            "mediaPoolItem": media_item,
            "startFrame": int(operation["start_frame"]),
            "endFrame": int(operation["end_frame"]),
            "mediaType": media_type,
            "trackIndex": int(operation["track_index"]),
            "recordFrame": int(operation["record_frame"]),
        }
        result = media_pool.AppendToTimeline([payload])
        return {"payload": payload, "append_result_type": type(result).__name__}

    def inspect(self, context: dict[str, Any]) -> dict[str, Any]:
        item = _required_context(context, "timeline_item")
        pool_item = item.GetMediaPoolItem()
        path = pool_item.GetClipProperty("File Path") if pool_item else None
        return {"source_path": path, "start_frame": item.GetStart(), "duration_frames": item.GetDuration()}

    def verify_render(self, render_path: Path, *, expected_duration_seconds: float | None = None) -> dict[str, Any]:
        return verify_render(render_path, expected_duration=expected_duration_seconds)


class LutAdapter:
    """仅部署和应用受管 `.cube` LUT，拒绝任意 PowerGrade 或 OpenFX 文件。"""

    mechanisms = LUT_MECHANISMS
    _MANAGED_DIRECTORY = "DavinciMcp"

    def probe(self, asset_path: Path, constraints: dict[str, Any]) -> AdapterPreflight:
        if asset_path.suffix.lower() != ".cube":
            return AdapterPreflight("lut_3d", False, reason="LUT Adapter 只接受 .cube 文件。")
        try:
            normalized, row_count = _normalize_cube(asset_path)
        except CreativeAdapterError as exc:
            return AdapterPreflight("lut_3d", False, reason=str(exc))
        return AdapterPreflight(
            "lut_3d",
            True,
            details={
                "adapter": "lut_3d",
                "numeric_row_count": row_count,
                "source_hash": sha256_file(asset_path),
                "deployed_hash": _sha256_bytes(normalized),
            },
        )

    def install_or_deploy(
        self, capability_id: str, asset_path: Path, constraints: dict[str, Any], context: dict[str, Any]
    ) -> AdapterDeployment:
        if context.get("allow_global_deploy") is not True:
            raise CreativeAdapterError("LUT 部署需要管理员显式授权，不能在普通工作流中自动执行。")
        target_root = Path(context.get("lut_root") or default_resolve_lut_root()).resolve()
        normalized, _ = _normalize_cube(asset_path)
        source_hash = sha256_file(asset_path)
        deployed_hash = _sha256_bytes(normalized)
        managed = target_root / self._MANAGED_DIRECTORY
        managed.mkdir(parents=True, exist_ok=True)
        filename = f"DavinciMcp_{_safe_capability_filename(capability_id)}.cube"
        installed = managed / filename
        if installed.exists() and sha256_file(installed) != deployed_hash:
            raise CreativeAdapterError("受管 LUT 目录存在同能力 ID 的不同内容，拒绝覆盖。")
        if not installed.exists():
            temporary = installed.with_suffix(".cube.tmp")
            temporary.write_bytes(normalized)
            os.replace(temporary, installed)
        relative = f"{self._MANAGED_DIRECTORY}\\{filename}"
        return AdapterDeployment("lut_3d", str(installed), relative, source_hash, deployed_hash, True)

    def validate(self, asset_path: Path, content_hash: str, constraints: dict[str, Any]) -> AdapterPreflight:
        if not asset_path.is_file() or sha256_file(asset_path) != content_hash:
            return AdapterPreflight("lut_3d", False, reason="LUT 本地缓存身份不一致。")
        return self.probe(asset_path, constraints)

    def apply(self, context: dict[str, Any]) -> dict[str, Any]:
        item = _required_context(context, "timeline_item")
        deployment = _required_context(context, "deployment")
        relative = str(deployment["installed_relative_path"])
        if item.SetLUT(1, relative) is False:
            raise CreativeAdapterError("Resolve 拒绝将 LUT 挂载到目标片段。")
        actual = str(item.GetLUT(1) or "")
        if _normalized_relative_path(actual) != _normalized_relative_path(relative):
            raise CreativeAdapterError("Resolve 读回的 LUT 与部署计划不一致。")
        return {"installed_relative_path": relative, "readback_lut": actual}

    def inspect(self, context: dict[str, Any]) -> dict[str, Any]:
        item = _required_context(context, "timeline_item")
        return {"readback_lut": str(item.GetLUT(1) or "")}

    def verify_render(self, render_path: Path, *, expected_duration_seconds: float | None = None) -> dict[str, Any]:
        return verify_render(render_path, expected_duration=expected_duration_seconds)


class FontAdapter:
    """只部署到当前 Windows 用户字体目录；标题映射尚未认证时不自动应用。"""

    mechanisms = FONT_MECHANISMS
    _REGISTRY_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"

    def probe(self, asset_path: Path, constraints: dict[str, Any]) -> AdapterPreflight:
        if asset_path.suffix.lower() not in {".ttf", ".otf"}:
            return AdapterPreflight("font_file", False, reason="字体 Adapter 只接受 .ttf 或 .otf 文件。")
        family = str(constraints.get("font_family") or "").strip()
        style = str(constraints.get("font_style") or "").strip()
        if not family or not style:
            return AdapterPreflight("font_file", False, reason="字体必须提供 Resolve 可选择的内部家族名和样式。")
        return AdapterPreflight(
            "font_file",
            True,
            details={
                "adapter": "font_file",
                "font_family": family,
                "font_style": style,
                # 只有认证渲染确认过的字符集才允许向计划披露，不能由 Text+ 回退字体冒充。
                "cjk_verified": constraints.get("cjk_verified") is True,
            },
        )

    def install_or_deploy(
        self, capability_id: str, asset_path: Path, constraints: dict[str, Any], context: dict[str, Any]
    ) -> AdapterDeployment:
        if context.get("allow_user_font_deploy") is not True:
            raise CreativeAdapterError("字体部署需要管理员显式授权，不能在普通工作流中自动执行。")
        preflight = self.probe(asset_path, constraints)
        if not preflight.ready_for_live_certification:
            raise CreativeAdapterError(preflight.reason or "字体静态预检未通过。")
        if sys.platform != "win32":
            raise CreativeAdapterError("受管字体部署目前只支持 Windows Resolve 工作站。")
        font_root = Path(
            context.get("font_root")
            or Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Microsoft" / "Windows" / "Fonts"
        ).resolve()
        font_root.mkdir(parents=True, exist_ok=True)
        source_hash = sha256_file(asset_path)
        installed = font_root / f"DavinciMcp_{_safe_capability_filename(capability_id)}{asset_path.suffix.lower()}"
        if installed.exists() and sha256_file(installed) != source_hash:
            raise CreativeAdapterError("受管字体目录存在同能力 ID 的不同内容，拒绝覆盖。")
        if not installed.exists():
            temporary = installed.with_suffix(installed.suffix + ".tmp")
            shutil.copyfile(asset_path, temporary)
            os.replace(temporary, installed)
        value_name = f"DavinciMcp {capability_id} ({'OpenType' if installed.suffix.lower() == '.otf' else 'TrueType'})"
        self._register_user_font(value_name, installed)
        self._activate_font_for_session(installed)
        return AdapterDeployment("font_file", str(installed), None, source_hash, source_hash, True)

    def validate(self, asset_path: Path, content_hash: str, constraints: dict[str, Any]) -> AdapterPreflight:
        if not asset_path.is_file() or sha256_file(asset_path) != content_hash:
            return AdapterPreflight("font_file", False, reason="字体本地缓存身份不一致。")
        return self.probe(asset_path, constraints)

    def apply(self, context: dict[str, Any]) -> dict[str, Any]:
        raise CreativeAdapterError("字体已具备部署 Adapter，但 Text+ 标题 Compiler Mapping 尚未认证，拒绝自动应用。")

    def inspect(self, context: dict[str, Any]) -> dict[str, Any]:
        font_manager = context.get("font_manager")
        family = str(context.get("font_family") or "")
        if not font_manager or not family:
            return {"font_discovery": "not_checked"}
        fonts = font_manager.GetFontList() or {}
        return {"font_discovery": "found" if family in fonts else "not_found", "styles": (fonts.get(family) or {})}

    def verify_render(self, render_path: Path, *, expected_duration_seconds: float | None = None) -> dict[str, Any]:
        return verify_render(render_path, expected_duration=expected_duration_seconds)

    @classmethod
    def _register_user_font(cls, value_name: str, installed_path: Path) -> None:
        try:
            import winreg
        except ImportError as exc:  # pragma: no cover - 仅 Windows 运行时进入。
            raise CreativeAdapterError("当前 Python 无法访问 Windows 用户字体注册表。") from exc
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cls._REGISTRY_PATH) as key:
            try:
                existing, _ = winreg.QueryValueEx(key, value_name)
            except FileNotFoundError:
                existing = None
            if existing is not None and os.path.normcase(str(existing)) != os.path.normcase(str(installed_path)):
                raise CreativeAdapterError("同能力 ID 已登记到不同用户字体路径，拒绝覆盖。")
            if existing is None:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, str(installed_path))

    @staticmethod
    def _activate_font_for_session(installed_path: Path) -> None:
        try:
            gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
            add_font = gdi32.AddFontResourceExW
            add_font.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
            add_font.restype = ctypes.c_int
            add_font(str(installed_path), 0, None)
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SendNotifyMessageW(ctypes.c_void_p(0xFFFF), 0x001D, 0, 0)
        except (AttributeError, OSError):
            # 当前会话未能刷新时，后续 Resolve 读回和渲染会要求重启，不把它误判为认证成功。
            return


class FusionEffectAdapter:
    """仅允许静态、单输入的 Fusion Effect `.setting`；标题和转场必须另有 Adapter。"""

    mechanisms = FUSION_EFFECT_MECHANISMS
    _DANGEROUS_TEMPLATE = re.compile(
        r"\b(?:require|dofile|loadfile|load|os\.execute|io\.|package\.|debug\.|comp:|bmd\.|RunScript)\b|"
        r"(?:ButtonControl|ScriptButton)",
        flags=re.IGNORECASE,
    )

    def probe(self, asset_path: Path, constraints: dict[str, Any]) -> AdapterPreflight:
        if asset_path.suffix.lower() != ".setting":
            return AdapterPreflight("fusion_effect", False, reason="Fusion Effect Adapter 只接受 .setting 文件。")
        try:
            analysis = self._analyze_static_template(asset_path)
        except CreativeAdapterError as exc:
            return AdapterPreflight("fusion_effect", False, reason=str(exc))
        return AdapterPreflight("fusion_effect", True, details={"adapter": "fusion_effect", **analysis})

    def install_or_deploy(
        self, capability_id: str, asset_path: Path, constraints: dict[str, Any], context: dict[str, Any]
    ) -> AdapterDeployment:
        if context.get("allow_fusion_deploy") is not True:
            raise CreativeAdapterError("Fusion 模板部署需要管理员显式授权，不能在普通工作流中自动执行。")
        preflight = self.probe(asset_path, constraints)
        if not preflight.ready_for_live_certification:
            raise CreativeAdapterError(preflight.reason or "Fusion 静态预检未通过。")
        root = Path(context.get("fusion_template_root") or default_resolve_fusion_template_root()).resolve()
        managed = root / "Edit" / "Effects" / "DavinciMcp"
        managed.mkdir(parents=True, exist_ok=True)
        installed = managed / f"DavinciMcp_{_safe_capability_filename(capability_id)}.setting"
        source_hash = sha256_file(asset_path)
        if installed.exists() and sha256_file(installed) != source_hash:
            raise CreativeAdapterError("受管 Fusion 目录存在同能力 ID 的不同内容，拒绝覆盖。")
        if not installed.exists():
            temporary = installed.with_suffix(".setting.tmp")
            shutil.copyfile(asset_path, temporary)
            os.replace(temporary, installed)
        relative = "Edit/Effects/DavinciMcp/" + installed.name
        return AdapterDeployment("fusion_effect", str(installed), relative, source_hash, source_hash, True)

    def validate(self, asset_path: Path, content_hash: str, constraints: dict[str, Any]) -> AdapterPreflight:
        if not asset_path.is_file() or sha256_file(asset_path) != content_hash:
            return AdapterPreflight("fusion_effect", False, reason="Fusion 模板本地缓存身份不一致。")
        return self.probe(asset_path, constraints)

    def apply(self, context: dict[str, Any]) -> dict[str, Any]:
        """只导入已部署且哈希一致的静态模板，再连接唯一的 MediaIn/MediaOut。"""

        item = _required_context(context, "timeline_item")
        deployment = _required_context(context, "deployment")
        installed = Path(str(deployment["installed_path"]))
        if not installed.is_file() or sha256_file(installed) != str(deployment["deployed_hash"]):
            raise CreativeAdapterError("受管 Fusion 模板文件不存在或哈希变化。")
        preflight = self.probe(installed, {})
        if not preflight.ready_for_live_certification:
            raise CreativeAdapterError(preflight.reason or "Fusion 模板不再满足安全合同。")
        before = int(item.GetFusionCompCount() or 0)
        imported = item.ImportFusionComp(str(installed))
        if imported is False:
            raise CreativeAdapterError("Resolve 拒绝导入受管 Fusion 模板。")
        after = int(item.GetFusionCompCount() or 0)
        if after <= before:
            raise CreativeAdapterError("Fusion 导入后没有新增可读回应的复合。")
        composition = item.GetFusionCompByIndex(after) or imported
        if not composition:
            raise CreativeAdapterError("Resolve 未读回导入后的 Fusion 复合。")
        tools = _tool_map(composition)
        macro_name, macro = _single_macro(tools)
        media_in_name, media_in = _ensure_fusion_tool(composition, tools, "MediaIn", -2)
        tools = _tool_map(composition)
        _, macro = _single_macro(tools)
        media_out_name, media_out = _ensure_fusion_tool(composition, tools, "MediaOut", 2)
        input_port = str(preflight.details["input_port"])
        output_port = str(preflight.details["output_port"])
        if macro.ConnectInput(input_port, _tool_output(media_in, ("Output", "Output1"))) is False:
            raise CreativeAdapterError("无法连接 Fusion 模板的唯一输入端口。")
        if media_out.ConnectInput("Input", _tool_output(macro, (output_port, "Output", "Output1"))) is False:
            raise CreativeAdapterError("无法连接 Fusion 模板输出到 MediaOut。")
        return {
            "composition_count_before": before,
            "composition_count_after": after,
            "macro_name": macro_name,
            "media_in": media_in_name,
            "media_out": media_out_name,
            "input_port": input_port,
            "output_port": output_port,
        }

    def inspect(self, context: dict[str, Any]) -> dict[str, Any]:
        item = _required_context(context, "timeline_item")
        count = int(item.GetFusionCompCount() or 0)
        names = item.GetFusionCompNameList() or []
        return {"composition_count": count, "composition_names": list(names.values()) if isinstance(names, dict) else list(names)}

    def verify_render(self, render_path: Path, *, expected_duration_seconds: float | None = None) -> dict[str, Any]:
        return verify_render(render_path, expected_duration=expected_duration_seconds)

    def _analyze_static_template(self, asset_path: Path) -> dict[str, Any]:
        try:
            payload = asset_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CreativeAdapterError("Fusion .setting 不是 UTF-8 文本，拒绝作为受管模板。") from exc
        if self._DANGEROUS_TEMPLATE.search(payload):
            raise CreativeAdapterError("Fusion 模板含脚本、按钮或外部执行入口，不能自动部署。")
        if len(payload) > 2 * 1024 * 1024:
            raise CreativeAdapterError("Fusion 模板超过受控静态分析大小上限。")
        macro_count = len(re.findall(r"\bMacroOperator\b", payload))
        # 标题、生成器和转场常含多个输入或 Group 图；不从文本猜测其自动化机制。
        inputs = sorted(set(re.findall(r"\b([A-Za-z][A-Za-z0-9_]*Input\d*)\s*=\s*InstanceInput", payload)))
        outputs = sorted(set(re.findall(r"\b([A-Za-z][A-Za-z0-9_]*Output\d*)\s*=\s*InstanceOutput", payload)))
        if macro_count != 1 or len(inputs) != 1 or len(outputs) != 1:
            raise CreativeAdapterError(
                "Fusion 模板不满足单 Macro、单输入、单输出 Effect 合同；标题、生成器和转场需专用 Adapter。"
            )
        return {
            "template_hash": sha256_file(asset_path),
            "macro_count": macro_count,
            "input_port": inputs[0],
            "output_port": outputs[0],
            "topology": "single_input_effect",
        }


class CreativeAdapterRegistry:
    """明确机制到 Adapter 的固定映射，未知机制一律拒绝。"""

    def __init__(self, adapters: list[CapabilityAdapter] | None = None) -> None:
        self._by_mechanism: dict[str, CapabilityAdapter] = {}
        for adapter in adapters or [DirectMediaAdapter(), LutAdapter(), FontAdapter(), FusionEffectAdapter()]:
            for mechanism in adapter.mechanisms:
                if mechanism in self._by_mechanism:
                    raise ValueError(f"重复 Adapter 机制：{mechanism}")
                self._by_mechanism[mechanism] = adapter

    def get(self, mechanism: str) -> CapabilityAdapter:
        adapter = self._by_mechanism.get(mechanism)
        if adapter is None:
            raise CreativeAdapterError(f"机制 {mechanism} 没有安全 Adapter，不能自动认证或执行。")
        return adapter

    def preflight(self, mechanism: str, asset_path: Path, constraints: dict[str, Any]) -> AdapterPreflight:
        return self.get(mechanism).probe(asset_path, {**constraints, "mechanism": mechanism})

    def mechanisms(self) -> list[str]:
        return sorted(self._by_mechanism)


def default_adapter_registry() -> CreativeAdapterRegistry:
    return CreativeAdapterRegistry()


def default_resolve_lut_root() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\\ProgramData")) / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "LUT"


def default_resolve_fusion_template_root() -> Path:
    return (
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        / "Blackmagic Design"
        / "DaVinci Resolve"
        / "Support"
        / "Fusion"
        / "Templates"
    )


def _normalize_cube(asset_path: Path) -> tuple[bytes, int]:
    """校验 LUT 行数并消除 Resolve 21 对科学计数法的兼容性问题。"""

    try:
        raw = asset_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CreativeAdapterError(".cube 文件不是 UTF-8 文本。") from exc
    size_1d: int | None = None
    size_3d: int | None = None
    numeric_rows = 0
    output: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            output.append(raw_line.rstrip())
            continue
        tokens = line.split()
        directive = tokens[0].upper()
        if directive in {"LUT_1D_SIZE", "LUT_3D_SIZE"}:
            if len(tokens) != 2 or not tokens[1].isdigit() or int(tokens[1]) < 2:
                raise CreativeAdapterError(f"LUT 指令 {directive} 不合法。")
            if directive == "LUT_1D_SIZE":
                size_1d = int(tokens[1])
            else:
                size_3d = int(tokens[1])
            output.append(f"{directive} {tokens[1]}")
            continue
        if directive in {"TITLE", "DOMAIN_MIN", "DOMAIN_MAX", "LUT_1D_INPUT_RANGE", "LUT_3D_INPUT_RANGE"}:
            output.append(raw_line.rstrip())
            continue
        if len(tokens) != 3:
            raise CreativeAdapterError("LUT 数值行必须恰好包含三个通道。")
        try:
            values = [float(token) for token in tokens]
        except ValueError as exc:
            raise CreativeAdapterError("LUT 包含无法解析的数值行。") from exc
        if not all(abs(value) < 1_000_000 for value in values):
            raise CreativeAdapterError("LUT 数值超出受控范围。")
        output.append(" ".join(_format_lut_number(value) for value in values))
        numeric_rows += 1
    if size_1d is None and size_3d is None:
        raise CreativeAdapterError("LUT 未声明 LUT_1D_SIZE 或 LUT_3D_SIZE。")
    expected = (size_1d or 0) + ((size_3d or 0) ** 3 if size_3d else 0)
    if numeric_rows != expected:
        raise CreativeAdapterError(f"LUT 数值行数不匹配：声明需要 {expected} 行，实际为 {numeric_rows} 行。")
    return ("\n".join(output).rstrip() + "\n").encode("utf-8"), numeric_rows


def _format_lut_number(value: float) -> str:
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _mechanism_from_summary(summary: dict[str, Any]) -> str:
    return "video_asset" if summary.get("has_video") else "audio_asset"


def _safe_capability_filename(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", value):
        raise CreativeAdapterError("能力 ID 不适合用作受管部署文件名。")
    return value


def _normalized_relative_path(value: str) -> str:
    return value.replace("\\", "/").strip("/").casefold()


def _sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _required_context(context: dict[str, Any], key: str) -> Any:
    if key not in context or context[key] is None:
        raise CreativeAdapterError(f"Adapter 缺少必需上下文：{key}。")
    return context[key]


def _tool_map(composition: Any) -> dict[str, Any]:
    raw = composition.GetToolList(False) or {}
    if not isinstance(raw, dict) or not raw:
        raise CreativeAdapterError("Fusion 导入后无法读回工具图。")
    result: dict[str, Any] = {}
    for fallback, tool in raw.items():
        name = str((tool.GetAttrs() or {}).get("TOOLS_Name") or fallback)
        if name in result:
            raise CreativeAdapterError("Fusion 工具图出现重复工具名。")
        result[name] = tool
    return result


def _single_macro(tools: dict[str, Any]) -> tuple[str, Any]:
    macros = [
        (name, tool)
        for name, tool in tools.items()
        if str((tool.GetAttrs() or {}).get("TOOLS_RegID") or "").casefold() == "macrooperator"
    ]
    if len(macros) != 1:
        raise CreativeAdapterError("Fusion 图没有唯一 MacroOperator，拒绝猜测连接方式。")
    return macros[0]


def _ensure_fusion_tool(composition: Any, tools: dict[str, Any], registry_id: str, x: int) -> tuple[str, Any]:
    candidates = [
        (name, tool)
        for name, tool in tools.items()
        if str((tool.GetAttrs() or {}).get("TOOLS_RegID") or "").casefold() == registry_id.casefold()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise CreativeAdapterError(f"Fusion 图存在多个 {registry_id}，拒绝自动连接。")
    if composition.AddTool(registry_id, x, 0) is False:
        raise CreativeAdapterError(f"Resolve 无法为 Fusion 模板添加 {registry_id}。")
    refreshed = _tool_map(composition)
    return _ensure_fusion_tool(composition, refreshed, registry_id, x)


def _tool_output(tool: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        value = getattr(tool, name, None)
        if value is not None:
            return value
    outputs = tool.GetOutputList() if callable(getattr(tool, "GetOutputList", None)) else {}
    if isinstance(outputs, dict):
        for name in names:
            if name in outputs:
                return outputs[name]
    raise CreativeAdapterError("Fusion 工具未暴露预期输出端口。")
