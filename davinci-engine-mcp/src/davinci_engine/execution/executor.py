"""将已校验计划写入隔离 Resolve 项目并进行真实读回。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from davinci_engine.analysis.ffmpeg_runtime import stream_summary, verify_render
from davinci_engine.common import sha256_file
from davinci_engine.creative.adapters import DIRECT_MEDIA_MECHANISMS, default_adapter_registry
from davinci_engine.execution.journal import EngineJournal
from davinci_engine.execution.plan import ResolveExecutionPlan
from davinci_engine.execution.validator import validate
from davinci_engine.resolve.connection import ResolveConnection


class ExecutionFailed(RuntimeError):
    pass


class EngineExecutor:
    def __init__(self, connection: ResolveConnection, journal: EngineJournal) -> None:
        self.connection = connection
        self.journal = journal
        self.adapter_registry = default_adapter_registry()

    def preview(self, plan: ResolveExecutionPlan) -> dict[str, Any]:
        validation = validate(plan)
        return {
            "state": "succeeded" if validation["valid"] else "failed",
            "plan_digest": plan.digest,
            "validation": validation,
            "changes": {
                "project": plan.project_name,
                "timeline": plan.timeline_name,
                "video_clips": len(plan.clips),
                "audio_clips": sum(clip.include_audio for clip in plan.clips),
                "creative_operations": len(plan.creative_operations),
                "render_path": str(plan.render_file()),
            },
        }

    def execute(self, plan: ResolveExecutionPlan, operation_id: str, execution_permit: str) -> dict[str, Any]:
        if execution_permit != plan.digest:
            return _failed("permit_mismatch", "执行许可与预览摘要不匹配，必须重新预览。")
        existing = self.journal.get(operation_id)
        if existing:
            if existing["plan_digest"] != plan.digest:
                return _failed("operation_conflict", "同一 operation_id 对应不同计划，拒绝写入。")
            if existing["status"] == "succeeded":
                return existing["response"]
            return {
                "state": "outcome_unknown",
                "operation_id": operation_id,
                "message": "该写入操作已发送但没有确定结果；必须先调用 reconcile_operation。",
            }
        validation = validate(plan)
        if not validation["valid"]:
            return {"state": "failed", "operation_id": operation_id, "validation": validation}
        self.journal.create(operation_id, plan.digest, plan.to_dict())
        try:
            project = self._prepare_project(plan)
            creative_media_paths = [
                operation.path()
                for operation in plan.creative_operations
                if operation.mechanism in DIRECT_MEDIA_MECHANISMS
            ]
            media_items = self._import_media(project, plan, additional_paths=creative_media_paths)
            timeline, clips_already_placed = self._create_timeline(project, plan, media_items)
            required_video_tracks, required_audio_tracks = self._required_track_counts(plan)
            self._ensure_tracks(timeline, "video", required_video_tracks)
            self._ensure_tracks(timeline, "audio", required_audio_tracks)
            media_pool = project.GetMediaPool()
            if not clips_already_placed:
                for clip in plan.clips:
                    item = media_items[os.path.normcase(os.path.realpath(clip.path()))]
                    self._append_clip(media_pool, item, clip, plan, timeline)
            creative_readback = self._apply_creative_operations(media_pool, timeline, plan, media_items)
            readback = self._readback(timeline)
            if readback["video_item_count"] < len(plan.clips):
                raise ExecutionFailed("写后读回的视频片段数少于计划，已拒绝将操作标记为成功。")
            self.connection.require_project_manager().SaveProject()
            response = {
                "state": "succeeded",
                "operation_id": operation_id,
                "plan_digest": plan.digest,
                "timeline": readback,
                "creative_operations": creative_readback,
            }
            self.journal.update(operation_id, "succeeded", response)
            return response
        except BaseException as exc:
            response = {
                "state": "outcome_unknown",
                "operation_id": operation_id,
                "plan_digest": plan.digest,
                "message": f"Resolve 写入期间出现异常：{type(exc).__name__}: {exc}",
            }
            self.journal.update(operation_id, "outcome_unknown", response)
            return response

    def render(self, plan: ResolveExecutionPlan, operation_id: str, execution_permit: str) -> dict[str, Any]:
        if execution_permit != plan.digest:
            return _failed("permit_mismatch", "渲染许可与执行计划摘要不匹配。")
        existing = self.journal.get(operation_id)
        if existing:
            if existing["plan_digest"] != plan.digest:
                return _failed("operation_conflict", "同一渲染 operation_id 对应不同计划。")
            if existing["status"] == "succeeded":
                return existing["response"]
            return {
                "state": "outcome_unknown",
                "operation_id": operation_id,
                "message": "该渲染操作状态未知，必须先对账，禁止直接重试。",
            }
        output = plan.render_file()
        if output.exists():
            return _failed("render_already_exists", "目标渲染文件已存在，拒绝覆盖。")
        self.journal.create(operation_id, plan.digest, plan.to_dict())
        try:
            project, timeline = self._load_expected_timeline(plan)
            output.parent.mkdir(parents=True, exist_ok=True)
            self._configure_h264_render(project, timeline, output)
            job_id = project.AddRenderJob()
            if not job_id:
                raise ExecutionFailed("Resolve 未创建渲染任务。")
            # Resolve 21 的 Python Bridge 在成功启动后可能返回 None；后续状态读回
            # 才是渲染是否真正开始/完成的权威依据。
            if project.StartRendering(job_id) is False:
                raise ExecutionFailed("Resolve 未启动渲染任务。")
            self._wait_for_render(project, job_id)
            actual_output = self._find_render_output(output)
            if actual_output != output:
                actual_output.replace(output)
            verification = verify_render(output, expected_duration=plan.expected_duration_seconds)
            if not verification["valid"]:
                raise ExecutionFailed(f"渲染文件技术验证失败：{verification['errors']}")
            response = {
                "state": "succeeded",
                "operation_id": operation_id,
                "plan_digest": plan.digest,
                "render_job_id": job_id,
                "output_path": str(output),
                "output_hash": sha256_file(output),
                "verification": verification,
            }
            self.journal.update(operation_id, "succeeded", response)
            return response
        except BaseException as exc:
            response = {
                "state": "outcome_unknown",
                "operation_id": operation_id,
                "plan_digest": plan.digest,
                "message": f"Resolve 渲染期间出现异常：{type(exc).__name__}: {exc}",
            }
            self.journal.update(operation_id, "outcome_unknown", response)
            return response

    def reconcile(self, operation_id: str) -> dict[str, Any]:
        record = self.journal.get(operation_id)
        if not record:
            return _failed("operation_not_found", "没有找到该 operation_id 的 Engine Journal 记录。")
        if record["status"] == "succeeded":
            return {"state": "succeeded", "operation_id": operation_id, "evidence": record["response"]}
        plan = ResolveExecutionPlan.from_dict(record["plan"])
        evidence: dict[str, Any] = {"journal_status": record["status"], "project": None, "timeline": None}
        try:
            self.connection.connect()
            manager = self.connection.require_project_manager()
            manager.LoadProject(plan.project_name)
            self.connection.refresh()
            project = self.connection.project
            if project and project.GetName() == plan.project_name:
                evidence["project"] = plan.project_name
                for index in range(1, int(project.GetTimelineCount() or 0) + 1):
                    timeline = project.GetTimelineByIndex(index)
                    if timeline and timeline.GetName() == plan.timeline_name:
                        evidence["timeline"] = self.connection.describe_timeline(timeline)
                        break
        except BaseException as exc:
            evidence["readback_error"] = f"{type(exc).__name__}: {exc}"
        # 存在时间线不足以证明所有写入完成，保持未知状态，禁止盲目重发。
        return {
            "state": "outcome_unknown",
            "operation_id": operation_id,
            "message": "只读对账未能证明所有预期副作用；请人工检查或建立更精细的读回合同。",
            "evidence": evidence,
        }

    def _prepare_project(self, plan: ResolveExecutionPlan) -> Any:
        self.connection.connect()
        manager = self.connection.require_project_manager()
        # Resolve 21 的 LoadProject 返回值可能为 None，即使它已成功切换当前项目；
        # 因此只以刷新后的实际 CurrentProject 作为真相，不能凭返回值判断。
        manager.GotoRootFolder()
        manager.LoadProject(plan.project_name)
        self.connection.refresh()
        project = self.connection.project
        creation_result = None
        close_result = None
        if not project or project.GetName() != plan.project_name:
            # Resolve 21 在已有项目打开时偶发拒绝加载受管项目。只关闭本系统此前创建的
            # 隔离项目，或刚启动时没有任何内容的默认 Untitled Project；绝不关闭用户项目。
            can_close_current = bool(
                project
                and (
                    project.GetName().startswith("DavinciMcp_")
                    or self._is_disposable_untitled_project(project)
                )
            )
            if can_close_current:
                if project.GetName().startswith("DavinciMcp_"):
                    manager.SaveProject()
                close_result = manager.CloseProject(project)
                time.sleep(0.2)
                self.connection.refresh()
                manager = self.connection.require_project_manager()
                manager.GotoRootFolder()
                manager.LoadProject(plan.project_name)
                self.connection.refresh()
                project = self.connection.project
            if project and project.GetName() == plan.project_name:
                # 关闭前项目后，目标项目已被成功加载，无需再创建。
                pass
            else:
                try:
                    target_exists = plan.project_name in set(manager.GetProjectListInCurrentFolder() or [])
                except BaseException:
                    target_exists = False
                if target_exists and project and not can_close_current:
                    raise ExecutionFailed(
                        "目标受管项目已经存在，但当前打开的是非受管用户项目；"
                        "Engine 不会自动关闭它。"
                    )
                creation_result = manager.CreateProject(plan.project_name)
                # 某些 Resolve 版本创建后不自动切换当前项目，必须显式读回加载。
                manager.GotoRootFolder()
                manager.LoadProject(plan.project_name)
                self.connection.refresh()
                project = self.connection.project
        if not project or project.GetName() != plan.project_name:
            current_name = project.GetName() if project else None
            raise ExecutionFailed(
                "无法创建或加载隔离 Resolve 项目。"
                f" 当前项目={current_name!r}，CloseProject 返回={close_result!r}，"
                f"CreateProject 返回={creation_result!r}。"
            )
        self._set_project_settings(project, plan)
        # 先持久化此前中断运行留下的受管项目状态。否则 Resolve 21 可能在前一次
        # 创建时间线的响应丢失后继续拒绝后续建线；这里不重发旧操作，只保存已读到的状态。
        manager.SaveProject()
        # 部分 Resolve 21 工作站仅在 Edit 页面接受带 trackIndex/recordFrame 的精确
        # AppendToTimeline。页面切换发生在已确认的受管项目内，随后仍以片段读回为准。
        self._ensure_edit_page()
        if self._find_timeline(project, plan.timeline_name):
            raise ExecutionFailed("目标工作时间线已存在，拒绝覆盖。")
        return project

    @staticmethod
    def _is_disposable_untitled_project(project: Any) -> bool:
        """仅识别启动时空白的默认项目；判断异常时一律按用户项目保护。"""
        try:
            if project.GetName() != "Untitled Project" or int(project.GetTimelineCount() or 0) != 0:
                return False
            pool = project.GetMediaPool()
            root = pool.GetRootFolder()
            return not (root.GetClipList() or []) and not (root.GetSubFolderList() or [])
        except BaseException:
            return False

    def _ensure_edit_page(self) -> None:
        """加载受管项目后确认 Edit 页面真正可用，再进行任何媒体池写操作。"""
        try:
            open_result = self.connection.resolve.OpenPage("edit")
        except BaseException as exc:
            raise ExecutionFailed(f"无法切换到 Resolve Edit 页面：{type(exc).__name__}: {exc}") from exc
        deadline = time.monotonic() + 3.0
        while True:
            page = self.connection.resolve.GetCurrentPage()
            if page == "edit":
                return
            if time.monotonic() >= deadline:
                raise ExecutionFailed(
                    "Resolve 未进入 Edit 页面；"
                    f"OpenPage 返回={type(open_result).__name__}，当前页面={page!r}。"
                )
            time.sleep(0.1)

    def _create_timeline(
        self, project: Any, plan: ResolveExecutionPlan, media_items: dict[str, Any]
    ) -> tuple[Any, bool]:
        pool = project.GetMediaPool()
        # Resolve 21 的部分 Python Bridge 会在写入实际成功后返回 None。对写操作的
        # 返回值只作诊断，必须先按稳定名称读回，确认未创建后才允许走不同的回退路径。
        empty_result = pool.CreateEmptyTimeline(plan.timeline_name)
        timeline = self._named_timeline(empty_result, plan.timeline_name) or self._wait_for_timeline(
            project, plan.timeline_name
        )
        if timeline is not None:
            return self._activate_timeline(project, timeline, plan.timeline_name), False
        # 部分 Resolve 21 工作站会拒绝空时间线；回退到同一受控媒体池的结构化建线。
        # 仍只使用计划内的源范围，且之后按读回结果验证，不把该回退当作任意自动剪辑。
        payloads = []
        for clip in plan.clips:
            item = media_items[os.path.normcase(os.path.realpath(clip.path()))]
            summary = stream_summary(clip.path())
            source_fps = float(summary.get("fps") or plan.timeline_fps)
            payloads.append(
                {
                    "mediaPoolItem": item,
                    "startFrame": max(0, int(round(clip.source_in_seconds * source_fps))),
                    "endFrame": max(1, int(round(clip.source_out_seconds * source_fps))),
                }
            )
        structured_result = pool.CreateTimelineFromClips(plan.timeline_name, payloads)
        timeline = self._named_timeline(structured_result, plan.timeline_name) or self._wait_for_timeline(
            project, plan.timeline_name
        )
        clips_placed_by_structured_creation = timeline is not None
        if timeline is None:
            # 个别 Resolve 21 会同时拒绝两种建线 API，但仍允许复制一个已验证为空的
            # 受管时间线。只接受 run_ 前缀、无声画片段的模板；绝不复制用户时间线或
            # 已有候选版本，因此不会把旧内容带入新运行。
            template = self._find_empty_managed_timeline(project, excluded_name=plan.timeline_name)
            duplicate_result = template.DuplicateTimeline(plan.timeline_name) if template is not None else None
            timeline = self._named_timeline(duplicate_result, plan.timeline_name) or self._wait_for_timeline(
                project, plan.timeline_name
            )
        if timeline is None:
            raise ExecutionFailed(
                "Resolve 未创建工作时间线（空时间线、结构化建线与空模板复制均失败）。"
                f"空建线返回={type(empty_result).__name__}，"
                f"结构化建线返回={type(structured_result).__name__}。"
            )
        # 结构化建线已经按计划放置素材；空模板复制则需要后续精确 AppendToTimeline。
        return self._activate_timeline(project, timeline, plan.timeline_name), clips_placed_by_structured_creation

    def _load_expected_timeline(self, plan: ResolveExecutionPlan) -> tuple[Any, Any]:
        self.connection.connect()
        manager = self.connection.require_project_manager()
        manager.LoadProject(plan.project_name)
        self.connection.refresh()
        project = self.connection.project
        if not project or project.GetName() != plan.project_name:
            raise ExecutionFailed("渲染前读取到错误的 Resolve 项目。")
        timeline = self._find_timeline(project, plan.timeline_name)
        if not timeline:
            raise ExecutionFailed("渲染前找不到计划指定的工作时间线。")
        if not project.SetCurrentTimeline(timeline):
            raise ExecutionFailed("无法激活计划指定的工作时间线。")
        return project, timeline

    @staticmethod
    def _set_project_settings(project: Any, plan: ResolveExecutionPlan) -> None:
        settings = {
            "timelineFrameRate": f"{plan.timeline_fps:g}",
            "timelineResolutionWidth": str(plan.width),
            "timelineResolutionHeight": str(plan.height),
        }
        for key, value in settings.items():
            result = project.SetSetting(key, value)
            if result is False:
                raise ExecutionFailed(f"无法设置 Resolve 项目参数 {key}={value}。")

    @staticmethod
    def _find_timeline(project: Any, name: str) -> Any | None:
        for index in range(1, int(project.GetTimelineCount() or 0) + 1):
            timeline = project.GetTimelineByIndex(index)
            if timeline and timeline.GetName() == name:
                return timeline
        return None

    @staticmethod
    def _named_timeline(candidate: Any, expected_name: str) -> Any | None:
        """仅接受读回名称与计划一致的时间线，避免静默写入当前用户时间线。"""
        if candidate is None:
            return None
        try:
            return candidate if candidate.GetName() == expected_name else None
        except BaseException:
            return None

    def _wait_for_timeline(self, project: Any, name: str, timeout_seconds: float = 2.0) -> Any | None:
        """在一次写请求后短暂轮询只读 API，不重发可能已生效的请求。"""
        deadline = time.monotonic() + timeout_seconds
        while True:
            timeline = self._find_timeline(project, name)
            if timeline is not None:
                return timeline
            try:
                current = project.GetCurrentTimeline()
            except BaseException:
                current = None
            timeline = self._named_timeline(current, name)
            if timeline is not None:
                return timeline
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    def _activate_timeline(self, project: Any, timeline: Any, expected_name: str) -> Any:
        """以当前时间线的实际读回确认激活结果，不依赖 SetCurrentTimeline 返回值。"""
        project.SetCurrentTimeline(timeline)
        deadline = time.monotonic() + 2.0
        while True:
            current = self._named_timeline(project.GetCurrentTimeline(), expected_name)
            if current is not None:
                return current
            if time.monotonic() >= deadline:
                raise ExecutionFailed(f"Resolve 未能激活工作时间线：{expected_name}。")
            time.sleep(0.1)

    def _find_empty_managed_timeline(self, project: Any, excluded_name: str) -> Any | None:
        """只选择经实际读回为空的 Engine 工作时间线作为兼容性复制模板。"""
        for index in range(1, int(project.GetTimelineCount() or 0) + 1):
            timeline = project.GetTimelineByIndex(index)
            if not timeline:
                continue
            try:
                if timeline.GetName() == excluded_name or not timeline.GetName().startswith("run_"):
                    continue
                for kind in ("video", "audio"):
                    for track in range(1, int(timeline.GetTrackCount(kind) or 0) + 1):
                        if timeline.GetItemListInTrack(kind, track):
                            break
                    else:
                        continue
                    break
                else:
                    return timeline
            except BaseException:
                # 无法确认模板为空时宁可不用，避免污染新时间线。
                continue
        return None

    @staticmethod
    def _walk_pool_items(folder: Any) -> Iterable[Any]:
        for item in folder.GetClipList() or []:
            yield item
        for child in folder.GetSubFolderList() or []:
            yield from EngineExecutor._walk_pool_items(child)

    def _import_media(
        self, project: Any, plan: ResolveExecutionPlan, *, additional_paths: list[Path] | None = None
    ) -> dict[str, Any]:
        pool = project.GetMediaPool()
        unique_paths = list(
            dict.fromkeys([*(str(clip.path()) for clip in plan.clips), *(str(path) for path in additional_paths or [])])
        )
        root_folder = pool.GetRootFolder()
        # 每个运行使用独立受管 Bin，避免依赖 UI 选中的位置，也避免时间线对象和媒体
        # 混在 Master 根目录。Folder 返回值异常时同样按名称读回，而不盲目重复创建。
        folder_name = f"__davinci_mcp_{plan.run_id[:16]}"
        created_folder = pool.AddSubFolder(root_folder, folder_name)
        target_folder = self._named_folder(created_folder, folder_name) or self._wait_for_folder(
            root_folder, folder_name
        )
        if target_folder is None:
            raise ExecutionFailed(f"Resolve 未创建受管素材 Bin：{folder_name}。")
        pool.SetCurrentFolder(target_folder)
        deadline = time.monotonic() + 2.0
        while self._named_folder(pool.GetCurrentFolder(), folder_name) is None:
            if time.monotonic() >= deadline:
                raise ExecutionFailed(f"Resolve 未切换到受管素材 Bin：{folder_name}。")
            time.sleep(0.1)
        # MediaStorage 是 Resolve 官方用于从已挂载卷导入当前 Bin 的接口。首期只选用
        # 一种导入机制，避免外部写响应不确定时对同一路径进行盲目重复 ImportMedia。
        media_storage = self.connection.resolve.GetMediaStorage()
        if not media_storage:
            raise ExecutionFailed("Resolve 未提供 MediaStorage，无法导入计划素材。")
        imported = media_storage.AddItemListToMediaPool(unique_paths) or []
        required = {os.path.normcase(os.path.realpath(path)): path for path in unique_paths}
        mapping: dict[str, Any] = {}
        # ImportMedia 的同步返回同样不可靠。请求只发送一次，随后只读媒体池直到
        # 全部素材出现或确认超时，避免“返回空列表”时盲目重复导入。
        deadline = time.monotonic() + 3.0
        while True:
            for item in list(imported) + list(self._walk_pool_items(root_folder)):
                try:
                    source_path = item.GetClipProperty("File Path")
                except BaseException:
                    continue
                key = os.path.normcase(os.path.realpath(source_path)) if source_path else ""
                if key in required:
                    mapping[key] = item
            if len(mapping) == len(required) or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        missing = sorted(set(required) - set(mapping))
        if missing:
            raise ExecutionFailed(
                f"Resolve 媒体池未能导入 {len(missing)} 个计划素材；"
                f"MediaStorage.AddItemListToMediaPool 返回 {len(imported)} 个项目。"
            )
        return mapping

    @staticmethod
    def _named_folder(candidate: Any, expected_name: str) -> Any | None:
        if candidate is None:
            return None
        try:
            return candidate if candidate.GetName() == expected_name else None
        except BaseException:
            return None

    def _wait_for_folder(self, parent: Any, name: str, timeout_seconds: float = 2.0) -> Any | None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                for folder in parent.GetSubFolderList() or []:
                    if self._named_folder(folder, name) is not None:
                        return folder
            except BaseException:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    @staticmethod
    def _ensure_tracks(timeline: Any, kind: str, required: int) -> None:
        current = int(timeline.GetTrackCount(kind) or 0)
        while current < required:
            result = timeline.AddTrack(kind)
            deadline = time.monotonic() + 2.0
            expected_count = current + 1
            while int(timeline.GetTrackCount(kind) or 0) < expected_count:
                if time.monotonic() >= deadline:
                    raise ExecutionFailed(
                        f"无法创建 {kind} 轨道 {expected_count}；"
                        f"AddTrack 返回={type(result).__name__}。"
                    )
                time.sleep(0.1)
            current = expected_count
        for index in range(1, current + 1):
            timeline.SetTrackName(kind, index, f"DavinciMcp {kind.title()} {index}")

    def _append_clip(
        self, media_pool: Any, media_item: Any, clip: Any, plan: ResolveExecutionPlan, timeline: Any
    ) -> Any:
        summary = stream_summary(clip.path())
        source_fps = float(summary.get("fps") or plan.timeline_fps)
        start_frame = max(0, int(round(clip.source_in_seconds * source_fps)))
        end_frame = max(start_frame + 1, int(round(clip.source_out_seconds * source_fps)))
        timeline_start_frame = int(timeline.GetStartFrame())
        video_payload = {
            "mediaPoolItem": media_item,
            "startFrame": start_frame,
            "endFrame": end_frame,
            "mediaType": 1,
            "trackIndex": clip.video_track,
            # Resolve 的 recordFrame 是绝对时间线帧；Plan 中的值保持相对节目起点。
            "recordFrame": int(timeline_start_frame) + clip.record_frame,
        }
        video_result = media_pool.AppendToTimeline([video_payload])
        video_item = self._require_placed_item(
            timeline,
            "video",
            clip.video_track,
            video_payload["recordFrame"],
            clip.path(),
            clip.asset_id,
            video_result,
        )
        if clip.include_audio and summary.get("has_audio"):
            audio_payload = {
                "mediaPoolItem": media_item,
                "startFrame": start_frame,
                "endFrame": end_frame,
                "mediaType": 2,
                "trackIndex": clip.audio_track,
                "recordFrame": int(timeline_start_frame) + clip.record_frame,
            }
            audio_result = media_pool.AppendToTimeline([audio_payload])
            self._require_placed_item(
                timeline,
                "audio",
                clip.audio_track,
                audio_payload["recordFrame"],
                clip.path(),
                clip.asset_id,
                audio_result,
            )
        return video_item

    @staticmethod
    def _require_placed_item(
        timeline: Any,
        kind: str,
        track: int,
        record_frame: int,
        expected_path: Path,
        asset_id: str,
        append_result: Any,
    ) -> Any:
        """写后按轨道、目标帧与媒体身份读回，API 返回值不能单独构成成功证据。"""
        expected = os.path.normcase(os.path.realpath(expected_path))
        deadline = time.monotonic() + 2.0
        while True:
            for item in timeline.GetItemListInTrack(kind, track) or []:
                try:
                    pool_item = item.GetMediaPoolItem()
                    source_path = pool_item.GetClipProperty("File Path") if pool_item else None
                    actual = os.path.normcase(os.path.realpath(source_path)) if source_path else ""
                    starts_at_expected_frame = int(item.GetStart()) == record_frame
                except BaseException:
                    continue
                if starts_at_expected_frame and actual == expected:
                    return item
            if time.monotonic() >= deadline:
                raise ExecutionFailed(
                    f"无法读回已放置的{kind}片段 {asset_id}；"
                    f"目标轨道={track}，目标帧={record_frame}，"
                    f"AppendToTimeline 返回={type(append_result).__name__}。"
                )
            time.sleep(0.1)

    @staticmethod
    def _required_track_counts(plan: ResolveExecutionPlan) -> tuple[int, int]:
        """创意直接媒体可以占用额外轨道，但不能降低源片段的既有轨道要求。"""

        video_tracks = [clip.video_track for clip in plan.clips]
        audio_tracks = [clip.audio_track for clip in plan.clips]
        for operation in plan.creative_operations:
            parameters = operation.parameters
            if operation.mechanism in {"image_asset", "video_asset", "video_overlay"}:
                video_tracks.append(int(parameters["video_track"]))
            elif operation.mechanism == "audio_asset":
                audio_tracks.append(int(parameters["audio_track"]))
        return max(video_tracks), max(audio_tracks)

    def _apply_creative_operations(
        self,
        media_pool: Any,
        timeline: Any,
        plan: ResolveExecutionPlan,
        media_items: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """只执行已通过计划校验的具体机制；未知能力绝不在这里猜测执行方式。"""

        readbacks: list[dict[str, Any]] = []
        timeline_start = int(timeline.GetStartFrame())
        for index, operation in enumerate(plan.creative_operations):
            adapter = self.adapter_registry.get(operation.mechanism)
            parameters = operation.parameters
            if operation.mechanism in DIRECT_MEDIA_MECHANISMS:
                path = operation.path()
                summary = stream_summary(path)
                source_fps = float(summary.get("fps") or plan.timeline_fps)
                source_in = float(parameters.get("source_in_seconds", 0))
                duration = float(parameters["duration_seconds"])
                start_frame = max(0, int(round(source_in * source_fps)))
                end_frame = max(start_frame + 1, int(round((source_in + duration) * source_fps)))
                kind = "audio" if operation.mechanism == "audio_asset" else "video"
                track = int(parameters["audio_track" if kind == "audio" else "video_track"])
                record_frame = timeline_start + int(parameters["record_frame"])
                item = media_items[os.path.normcase(os.path.realpath(path))]
                applied = adapter.apply(
                    {
                        "media_pool": media_pool,
                        "media_item": item,
                        "operation": {
                            "media_type": 2 if kind == "audio" else 1,
                            "start_frame": start_frame,
                            "end_frame": end_frame,
                            "track_index": track,
                            "record_frame": record_frame,
                        },
                    }
                )
                timeline_item = self._require_placed_item(
                    timeline,
                    kind,
                    track,
                    record_frame,
                    path,
                    operation.capability_id,
                    applied["append_result_type"],
                )
                readbacks.append(
                    {
                        "index": index,
                        "capability_id": operation.capability_id,
                        "mechanism": operation.mechanism,
                        "apply": applied,
                        "readback": adapter.inspect({"timeline_item": timeline_item}),
                    }
                )
                continue
            if operation.mechanism == "lut_3d":
                target_clip = plan.clips[int(parameters["target_clip_index"])]
                target_item = self._require_placed_item(
                    timeline,
                    "video",
                    target_clip.video_track,
                    timeline_start + target_clip.record_frame,
                    target_clip.path(),
                    target_clip.asset_id,
                    None,
                )
                applied = adapter.apply(
                    {
                        "timeline_item": target_item,
                        "deployment": {"installed_relative_path": parameters["installed_relative_path"]},
                    }
                )
                readbacks.append(
                    {
                        "index": index,
                        "capability_id": operation.capability_id,
                        "mechanism": operation.mechanism,
                        "apply": applied,
                        "readback": adapter.inspect({"timeline_item": target_item}),
                    }
                )
                continue
            raise ExecutionFailed(f"创意操作 {operation.capability_id} 缺少受支持的执行 Mapping。")

    @staticmethod
    def _readback(timeline: Any) -> dict[str, Any]:
        video_items = []
        audio_items = []
        for kind, target in (("video", video_items), ("audio", audio_items)):
            for track in range(1, int(timeline.GetTrackCount(kind) or 0) + 1):
                for item in timeline.GetItemListInTrack(kind, track) or []:
                    try:
                        pool_item = item.GetMediaPoolItem()
                        source = pool_item.GetClipProperty("File Path") if pool_item else None
                    except BaseException:
                        source = None
                    target.append(
                        {
                            "track": track,
                            "start_frame": item.GetStart(),
                            "duration_frames": item.GetDuration(),
                            "source_path": source,
                        }
                    )
        return {
            "name": timeline.GetName(),
            "start_frame": timeline.GetStartFrame(),
            "video_item_count": len(video_items),
            "audio_item_count": len(audio_items),
            "video_items": video_items,
            "audio_items": audio_items,
        }

    @staticmethod
    def _configure_h264_render(project: Any, timeline: Any, output: Path) -> None:
        formats = project.GetRenderFormats() or {}
        # Resolve 返回的是“显示名称 -> API 标识符”；SetCurrent... 只能接收后者。
        selected_format = next(
            (
                str(format_id)
                for format_name, format_id in dict(formats).items()
                if str(format_name).lower() == "mp4" or str(format_id).lower() == "mp4"
            ),
            None,
        )
        if not selected_format:
            raise ExecutionFailed("当前 Resolve 不提供 MP4 渲染格式。")
        codecs = project.GetRenderCodecs(selected_format) or {}
        selected_codec = next(
            (
                str(codec_id)
                for codec_name, codec_id in dict(codecs).items()
                if "264" in f"{codec_name} {codec_id}".lower()
            ),
            None,
        )
        if not selected_codec:
            raise ExecutionFailed("当前 Resolve 不提供 H.264 渲染编码。")
        set_result = project.SetCurrentRenderFormatAndCodec(selected_format, selected_codec)
        if set_result is False:
            raise ExecutionFailed("当前 Resolve 无法选择 H.264 MP4 渲染编码。")
        get_current = getattr(project, "GetCurrentRenderFormatAndCodec", None)
        if callable(get_current):
            current = get_current()
            if not isinstance(current, dict) or (
                str(current.get("format", "")).lower() != selected_format.lower()
                or str(current.get("codec", "")).lower() != selected_codec.lower()
            ):
                raise ExecutionFailed(
                    "Resolve 未读回预期的 MP4/H.264 渲染编码："
                    f"请求 {selected_format}/{selected_codec}，实际 {current!r}。"
                )
        elif set_result is None:
            raise ExecutionFailed("Resolve 未确认渲染编码设置，且当前版本不支持读回。")
        settings = {
            "TargetDir": str(output.parent),
            "CustomName": output.stem,
            "SelectAllFrames": True,
            "ExportVideo": True,
            "ExportAudio": True,
        }
        # 空返回由 AddRenderJob 与最终输出文件的读回继续验证，False 才是明确拒绝。
        if project.SetRenderSettings(settings) is False:
            raise ExecutionFailed("Resolve 拒绝渲染设置。")

    @staticmethod
    def _wait_for_render(project: Any, job_id: str, timeout_seconds: int = 1800) -> None:
        deadline = time.monotonic() + timeout_seconds
        while project.IsRenderingInProgress():
            if time.monotonic() >= deadline:
                raise ExecutionFailed("等待 Resolve 渲染超时。")
            time.sleep(0.5)
        status = project.GetRenderJobStatus(job_id) or {}
        status_text = str(status.get("JobStatus", "")).lower()
        # Resolve 会随界面语言返回状态文本；完成百分比与中英文完成状态均可作为
        # 渲染结束的只读证据，避免把已完成任务误判为结果未知。
        completed = float(status.get("CompletionPercentage") or 0) >= 100
        successful_status = any(token in status_text for token in ("complete", "done", "success", "完成"))
        if status_text and not (completed or successful_status):
            raise ExecutionFailed(f"Resolve 渲染任务未成功完成：{status}")

    @staticmethod
    def _find_render_output(expected: Path) -> Path:
        if expected.exists() and expected.stat().st_size:
            return expected
        candidates = sorted(
            (path for path in expected.parent.glob(f"{expected.stem}*") if path.is_file() and path.stat().st_size),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise ExecutionFailed("Resolve 渲染结束后未找到输出文件。")
        return candidates[0]


def _failed(code: str, message: str) -> dict[str, Any]:
    return {"state": "failed", "error": {"code": code, "message": message}}
