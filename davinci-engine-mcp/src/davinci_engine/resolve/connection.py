"""唯一 Resolve 连接所有者；其它 Engine 模块不得自行导入原生 API。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from davinci_engine.resolve.bootstrap import probe_import


class ResolveDisconnected(RuntimeError):
    pass


@dataclass
class ResolveConnection:
    resolve: Any | None = None
    project_manager: Any | None = None
    project: Any | None = None
    media_pool: Any | None = None

    def status(self) -> dict[str, Any]:
        probe = probe_import()
        return {
            "safe_to_import": bool(probe.get("safe")),
            "connected": bool(probe.get("connected")),
            "reason": probe.get("reason"),
            "bootstrap": probe.get("bootstrap"),
        }

    def execution_readiness(self) -> dict[str, Any]:
        """读取真正可执行的 UI 状态；仅连接成功不足以允许写入。"""
        result = self.status()
        if not result["connected"]:
            result["ui_ready"] = False
            result["current_page"] = None
            return result
        try:
            self.connect()
            page = self.resolve.GetCurrentPage()
            project = self.project
        except BaseException as exc:
            result["ui_ready"] = False
            result["ready_for_execution"] = False
            result["current_page"] = None
            result["reason"] = f"Resolve UI 状态不可读取：{type(exc).__name__}: {exc}"
            return result
        result["current_page"] = page
        result["current_project"] = project.GetName() if project else None
        result["project_manager_open"] = project is None
        blank_untitled_project = False
        if project and project.GetName() == "Untitled Project":
            try:
                pool = project.GetMediaPool()
                root = pool.GetRootFolder()
                blank_untitled_project = (
                    int(project.GetTimelineCount() or 0) == 0
                    and not (root.GetClipList() or [])
                    and not (root.GetSubFolderList() or [])
                )
            except BaseException:
                blank_untitled_project = False
        result["blank_untitled_project"] = blank_untitled_project
        result["ui_ready"] = page in {"media", "cut", "edit", "fusion", "color", "fairlight", "deliver"}
        # Resolve 重启后通常停在“项目管理器”。此时还没有项目页面，但 Engine 可以
        # 安全加载自身的受管项目；只有已加载项目且页面不可用才阻止后续写入。
        result["ready_for_execution"] = bool(
            result["project_manager_open"] or result["blank_untitled_project"] or result["ui_ready"]
        )
        if not result["ready_for_execution"]:
            result["reason"] = "Resolve 当前没有可用工作页面；请关闭模态窗口并打开项目页面后重试。"
        return result

    def connect(self) -> None:
        status = self.status()
        if not status["safe_to_import"]:
            raise ResolveDisconnected(status["reason"] or "Resolve 原生模块无法安全导入。")
        if not status["connected"]:
            raise ResolveDisconnected(
                "Resolve 未连接。请确认 Resolve Studio 正在运行，且“外部脚本使用”已设为 Local。"
            )
        try:
            import DaVinciResolveScript as resolve_script  # type: ignore[import-not-found]

            self.resolve = resolve_script.scriptapp("Resolve")
        except BaseException as exc:
            raise ResolveDisconnected(f"无法连接 Resolve：{type(exc).__name__}: {exc}") from exc
        if not self.resolve:
            raise ResolveDisconnected("Resolve 未返回可用脚本对象。")
        self.refresh()

    def refresh(self) -> None:
        if not self.resolve:
            self.connect()
            return
        self.project_manager = self.resolve.GetProjectManager()
        self.project = self.project_manager.GetCurrentProject() if self.project_manager else None
        self.media_pool = self.project.GetMediaPool() if self.project else None

    def require_project_manager(self) -> Any:
        if not self.project_manager:
            self.connect()
        if not self.project_manager:
            raise ResolveDisconnected("无法取得 Resolve ProjectManager。")
        return self.project_manager

    @staticmethod
    def describe_timeline(timeline: Any) -> dict[str, Any]:
        tracks: dict[str, int] = {}
        for kind in ("video", "audio", "subtitle"):
            try:
                tracks[kind] = int(timeline.GetTrackCount(kind))
            except BaseException:
                tracks[kind] = 0
        return {"name": timeline.GetName(), "start_frame": timeline.GetStartFrame(), "tracks": tracks}

    def inspect(self) -> dict[str, Any]:
        self.connect()
        project = self.project
        manager = self.require_project_manager()
        try:
            visible_projects = list(manager.GetProjectListInCurrentFolder() or [])
        except BaseException:
            visible_projects = []
        try:
            project_attributes = manager.GetProjectAttributesInCurrentFolder() or {}
        except BaseException:
            project_attributes = {}
        managed_projects = sorted(name for name in visible_projects if str(name).startswith("DavinciMcp_"))
        if not project:
            return {"connected": True, "project": None, "timelines": [], "managed_projects": managed_projects}
        timelines = []
        for index in range(1, int(project.GetTimelineCount() or 0) + 1):
            timeline = project.GetTimelineByIndex(index)
            if timeline:
                timelines.append(self.describe_timeline(timeline))
        current = project.GetCurrentTimeline()
        media_pool = project.GetMediaPool()
        # Resolve 版本和授权会影响可用封装格式/编码；仅在检查接口中读回，
        # 供执行器选择真实可用的渲染组合，绝不在这里改变项目设置。
        render_capabilities: dict[str, Any] = {"formats": {}, "current": None}
        try:
            formats = project.GetRenderFormats() or {}
            for format_id, format_name in dict(formats).items():
                try:
                    codecs = project.GetRenderCodecs(format_id) or {}
                    render_capabilities["formats"][str(format_id)] = {
                        "name": str(format_name),
                        "codecs": {str(codec_id): str(codec_name) for codec_id, codec_name in dict(codecs).items()},
                    }
                except BaseException as exc:
                    render_capabilities["formats"][str(format_id)] = {
                        "name": str(format_name),
                        "codecs_error": f"{type(exc).__name__}: {exc}",
                    }
            get_current_render = getattr(project, "GetCurrentRenderFormatAndCodec", None)
            if callable(get_current_render):
                render_capabilities["current"] = get_current_render()
        except BaseException as exc:
            render_capabilities["error"] = f"{type(exc).__name__}: {exc}"
        mounted_volumes: list[str] = []
        try:
            media_storage = self.resolve.GetMediaStorage()
            mounted_volumes = list(media_storage.GetMountedVolumeList() or []) if media_storage else []
        except BaseException:
            pass
        media_items: list[dict[str, Any]] = []
        current_folder_name = None
        try:
            current_folder = media_pool.GetCurrentFolder()
            current_folder_name = current_folder.GetName() if current_folder else None
            root_folder = media_pool.GetRootFolder()
            for item in root_folder.GetClipList() or []:
                if len(media_items) >= 20:
                    break
                try:
                    media_items.append(
                        {
                            "name": item.GetName(),
                            "file_path": item.GetClipProperty("File Path"),
                            "media_id": item.GetMediaId(),
                        }
                    )
                except BaseException:
                    media_items.append({"name": "<无法读取媒体属性>"})
        except BaseException:
            pass
        return {
            "connected": True,
            "product_name": self.resolve.GetProductName(),
            "version": self.resolve.GetVersionString(),
            "page": self.resolve.GetCurrentPage(),
            "project": {"name": project.GetName(), "current_timeline": current.GetName() if current else None},
            "timelines": timelines,
            "managed_projects": managed_projects,
            "managed_project_attributes": {
                name: project_attributes.get(name, {}) for name in managed_projects
            },
            "media_pool": {
                "current_folder": current_folder_name,
                "root_items": media_items,
                "mounted_volumes": mounted_volumes,
            },
            "render_capabilities": render_capabilities,
        }
