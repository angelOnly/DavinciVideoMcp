"""受控 Codex App Server Adapter：专业 Skill 与稀疏抽帧分析都只能只读运行。"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from davinci_app.config import AppConfig
from davinci_app.interfaces.project_evidence import ProjectEvidenceScope


REQUIRED_VIDEO_SKILLS = (
    "video-source-understanding",
    "video-edit-director",
    "video-sound-rhythm-designer",
    "video-visual-designer",
    "video-typography-designer",
    "video-finishing-designer",
)

# App Server 的权限档案由运行时实际列举；此处只使用内置的最低只读档案。
READ_ONLY_PERMISSION_PROFILE = ":read-only"
# 第一版控制一次交给 Codex 的图片数量；密集窗口会均匀保留更多样本，仍不是连续视频。
MAX_FRAME_EVIDENCE_IMAGES = 12


class CodexAppServerError(RuntimeError):
    """Codex App Server 协议、进程、输出或权限合同失败。"""


class _JsonRpcAppServer:
    """最小 JSON-RPC stdio 客户端；不依赖 Codex SDK。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self._incoming: queue.Queue[dict[str, Any]] = queue.Queue()
        self._notifications: list[dict[str, Any]] = []
        self._next_id = 1

    def __enter__(self) -> "_JsonRpcAppServer":
        executable = _resolve_codex_executable(self.config.codex_app_server_command)
        environment = dict(os.environ)
        environment["RUST_LOG"] = "error"
        try:
            self.process = subprocess.Popen(
                [executable, "app-server", "--stdio"],
                cwd=str(self.config.repository_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=environment,
            )
        except OSError as exc:
            raise CodexAppServerError(f"无法启动 Codex App Server：{_safe_error(exc)}") from exc
        assert self.process.stdout is not None
        threading.Thread(target=self._read_stdout, args=(self.process.stdout,), daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "davinci_video_mcp",
                    "title": "DavinciVideoMcp 受控专业运行时",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "optOutNotificationMethods": ["item/agentMessage/delta"],
                    # 新版 App Server 仅在实验 API 已声明时接受 permissions 档案。
                    "experimentalApi": True,
                },
            },
        )
        self.notify("initialized", {})
        return self

    def __exit__(self, *_: Any) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        while True:
            message = self._next_message(self.config.codex_runtime_timeout_seconds)
            if message.get("id") == request_id and "result" in message:
                result = message["result"]
                if not isinstance(result, dict):
                    raise CodexAppServerError(f"{method} 返回不是对象。")
                return result
            if message.get("id") == request_id and "error" in message:
                raise CodexAppServerError(f"Codex App Server {method} 失败：{_error_message(message['error'])}")
            self._handle_or_stash(message)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def wait_for_turn(self, turn_id: str) -> tuple[dict[str, Any], list[str]]:
        messages: list[str] = []
        while True:
            message = self._next_event(self.config.codex_runtime_timeout_seconds)
            method = message.get("method")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method == "item/completed":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    messages.append(item["text"])
            if method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if turn.get("id") == turn_id:
                    if turn.get("status") != "completed":
                        error = turn.get("error")
                        raise CodexAppServerError(f"Codex turn 未完成：{_error_message(error)}")
                    return turn, messages
            if "id" in message and "method" in message:
                self._decline_server_request(message)

    def _read_stdout(self, stream: Any) -> None:
        for raw in stream:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self._incoming.put(payload)

    def _next_message(self, timeout_seconds: int) -> dict[str, Any]:
        try:
            return self._incoming.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise CodexAppServerError("等待 Codex App Server 响应超时。") from exc

    def _next_event(self, timeout_seconds: int) -> dict[str, Any]:
        if self._notifications:
            return self._notifications.pop(0)
        return self._next_message(timeout_seconds)

    def _handle_or_stash(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" in message:
            self._decline_server_request(message)
            return
        self._notifications.append(message)

    def _decline_server_request(self, message: dict[str, Any]) -> None:
        """专业运行时没有外部写入授权；所有非预期请求一律拒绝。"""
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(request_id, int):
            return
        if method == "item/permissions/requestApproval":
            result: dict[str, Any] = {"permissions": [], "scope": "turn"}
        elif method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            result = {"decision": "decline"}
        else:
            result = {"action": "decline", "content": None}
        self._send({"id": request_id, "result": result})

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None or self.process.poll() is not None:
            raise CodexAppServerError("Codex App Server 进程已退出。")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()


class CodexAppServerSkillRuntime:
    """Workflow 唯一可用的专业 Skill Runtime，Skill 无法获得 Resolve 或写入权限。"""

    def __init__(
        self,
        config: AppConfig,
        project_service: Any,
        *,
        session_factory: Callable[[AppConfig], _JsonRpcAppServer] | None = None,
    ) -> None:
        self.config = config
        self.project_service = project_service
        self._session_factory = session_factory or _JsonRpcAppServer

    def health_check(self) -> dict[str, Any]:
        try:
            missing = [name for name in REQUIRED_VIDEO_SKILLS if not self._skill_path(name).exists()]
            if missing:
                raise CodexAppServerError(f"项目专业 Skill 文件缺失：{'、'.join(missing)}")
            with self._session_factory(self.config) as session:
                profiles = session.request("permissionProfile/list", {"cwd": str(self.config.repository_root)})
                if not _read_only_profile_allowed(profiles):
                    raise CodexAppServerError("Codex App Server 未允许受限只读权限档案，不能启动专业 Skill。")
                payload = session.request(
                    "skills/list",
                    {
                        "cwds": [str(self.config.repository_root)],
                        "forceReload": True,
                        "perCwdExtraUserRoots": [
                            {
                                "cwd": str(self.config.repository_root),
                                "extraUserRoots": [str(self.config.repository_root / ".agents" / "skills")],
                            }
                        ],
                    },
                )
            listed = {
                str(skill.get("name"))
                for group in payload.get("data", [])
                if isinstance(group, dict)
                for skill in group.get("skills", [])
                if isinstance(skill, dict) and isinstance(skill.get("name"), str)
            }
            absent = [name for name in REQUIRED_VIDEO_SKILLS if name not in listed]
            return {
                "available": not absent,
                "reason": None if not absent else f"Codex App Server 未发现专业 Skill：{'、'.join(absent)}",
                "skills": sorted(listed & set(REQUIRED_VIDEO_SKILLS)),
                "missing_skills": absent,
                "command": self.config.codex_app_server_command,
            }
        except (CodexAppServerError, OSError) as exc:
            return {"available": False, "reason": _safe_error(exc), "skills": [], "missing_skills": list(REQUIRED_VIDEO_SKILLS)}

    def invoke(self, skill_name: str, *, mode: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        if skill_name not in REQUIRED_VIDEO_SKILLS:
            raise CodexAppServerError(f"不允许调用未登记的专业 Skill：{skill_name}")
        run = payload.get("run")
        if not isinstance(run, dict) or not isinstance(run.get("project_id"), str):
            raise CodexAppServerError("专业 Skill 调用缺少冻结运行的项目身份。")
        schema = skill_output_schema(skill_name, mode)
        scope, local_images = self._scope_and_images(run, payload)
        assignment = _skill_assignment(skill_name, mode, scope)
        with self._session_factory(self.config) as session:
            thread_id = self._ensure_project_thread(session, run["project_id"])
            result = self._run_structured_turn(
                session,
                thread_id=thread_id,
                text=assignment,
                schema=schema,
                local_images=local_images,
                skill_name=skill_name,
                repair=False,
            )
            try:
                return _parse_structured_output(result)
            except CodexAppServerError:
                # 只允许一次格式修复；仍失败应由 Workflow 停止，不能伪造产物。
                repaired = self._run_structured_turn(
                    session,
                    thread_id=thread_id,
                    text="上一次输出不符合 JSON Schema。请不要解释，只按本次 outputSchema 返回一个 JSON 对象。",
                    schema=schema,
                    local_images=local_images,
                    skill_name=skill_name,
                    repair=True,
                )
                return _parse_structured_output(repaired)

    def _scope_and_images(self, run: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[Path, float]]]:
        project_root = self.config.projects_root / str(run["project_id"])
        scope = ProjectEvidenceScope(project_root)
        evidence = payload.get("evidence")
        if isinstance(evidence, dict):
            return scope.snapshot(run, evidence), scope.local_images(evidence)
        # 后续专业步骤只能得到前一阶段的结构化结果，不能借此读取整个项目目录。
        compact = {
            key: value
            for key, value in payload.items()
            if key != "run" and key not in {"work_preview"}
        }
        return {
            "scope": "project_evidence_read_only_v1",
            "project_id": run["project_id"],
            "brief": (run.get("input_snapshot") or {}).get("brief") or {},
            "inputs": _limit_json_value(compact),
            "limitations": ["只使用本回合附加的结构化输入；不得读取原始视频或操作 Resolve。"],
        }, []

    def _ensure_project_thread(self, session: _JsonRpcAppServer, project_id: str) -> str:
        existing = self.project_service.get_codex_thread_id(project_id)
        if existing:
            session.request("thread/resume", {"threadId": existing, "permissions": READ_ONLY_PERMISSION_PROFILE})
            return existing
        params: dict[str, Any] = {
            "cwd": str(self.config.repository_root),
            "approvalPolicy": "never",
            "permissions": READ_ONLY_PERMISSION_PROFILE,
            "serviceName": "davinci_video_mcp",
        }
        if self.config.codex_runtime_model:
            params["model"] = self.config.codex_runtime_model
        started = session.request("thread/start", params)
        thread = started.get("thread") if isinstance(started.get("thread"), dict) else {}
        proposed = thread.get("id")
        if not isinstance(proposed, str) or not proposed:
            raise CodexAppServerError("Codex App Server 未返回 Thread ID。")
        persisted = self.project_service.record_codex_thread_id(project_id, proposed)
        if persisted != proposed:
            session.request("thread/resume", {"threadId": persisted})
        return persisted

    def _run_structured_turn(
        self,
        session: _JsonRpcAppServer,
        *,
        thread_id: str,
        text: str,
        schema: dict[str, Any],
        local_images: list[tuple[Path, float]],
        skill_name: str,
        repair: bool,
    ) -> str:
        inputs: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if not repair and skill_name:
            inputs.append({"type": "skill", "name": skill_name, "path": str(self._skill_path(skill_name))})
        for path, moment in local_images:
            inputs.append({"type": "localImage", "path": str(path)})
            inputs.append({"type": "text", "text": f"上一张图片对应源时间约 {moment:.3f} 秒。"})
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": inputs,
            "cwd": str(self.config.repository_root),
            "approvalPolicy": "never",
            "permissions": READ_ONLY_PERMISSION_PROFILE,
            "outputSchema": schema,
            "summary": "concise",
        }
        if self.config.codex_runtime_model:
            params["model"] = self.config.codex_runtime_model
        response = session.request("turn/start", params)
        turn = response.get("turn") if isinstance(response.get("turn"), dict) else {}
        turn_id = turn.get("id")
        if not isinstance(turn_id, str):
            raise CodexAppServerError("Codex App Server 未返回 Turn ID。")
        _, messages = session.wait_for_turn(turn_id)
        if not messages:
            raise CodexAppServerError("Codex turn 未产生结构化回复。")
        return messages[-1]

    def _skill_path(self, skill_name: str) -> Path:
        return self.config.repository_root / ".agents" / "skills" / skill_name / "SKILL.md"


class CodexFrameEvidenceAnalyzer:
    """仅用 localImage 调用 Codex；不能把抽帧伪装成连续音视频理解。"""

    def __init__(
        self,
        config: AppConfig,
        project_service: Any,
        *,
        session_factory: Callable[[AppConfig], _JsonRpcAppServer] | None = None,
    ) -> None:
        self.config = config
        self.project_service = project_service
        self._runtime = CodexAppServerSkillRuntime(
            config,
            project_service,
            session_factory=session_factory,
        )

    def identity(self) -> dict[str, Any]:
        return {"name": "CodexFrameEvidenceAnalyzer", "version": "codex-sparse-frame-v1"}

    def health_check(self) -> dict[str, Any]:
        return self._runtime.health_check()

    def analyze_frames(
        self,
        image_paths: list[Path],
        *,
        asset_id: str,
        frame_times: list[float],
        transcript_context: list[dict[str, Any]],
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if not project_id:
            raise CodexAppServerError("Codex 抽帧分析必须绑定项目，不能创建游离业务状态。")
        if not image_paths or len(image_paths) != len(frame_times):
            raise CodexAppServerError("Codex 抽帧分析缺少图片或时间映射。")
        if len(image_paths) > MAX_FRAME_EVIDENCE_IMAGES:
            # 密集窗口不能只看开头；均匀保留整个请求范围内的更多代表帧。
            indexes = [
                round(index * (len(image_paths) - 1) / (MAX_FRAME_EVIDENCE_IMAGES - 1))
                for index in range(MAX_FRAME_EVIDENCE_IMAGES)
            ]
            image_paths = [image_paths[index] for index in indexes]
            frame_times = [frame_times[index] for index in indexes]
        for path in image_paths:
            if not path.exists():
                raise CodexAppServerError("Codex 抽帧分析引用的图片不存在。")
        schema = _frame_schema()
        prompt = (
            "仅依据附加的稀疏抽帧生成补充视觉证据。不要声称看过原始视频、帧间运动、音频或完整连续画面。"
            f"素材={asset_id}；图片顺序对应源时间秒={json.dumps(frame_times)}；"
            f"转写候选仅作参考={json.dumps(transcript_context, ensure_ascii=False)}。"
            "每个 observation 必须引用图片的时间；涉及动作、精确边界、微表情或小文字时 needs_dense_review 必须为 true。"
        )
        with self._runtime._session_factory(self.config) as session:
            thread_id = self._runtime._ensure_project_thread(session, project_id)
            result = self._runtime._run_structured_turn(
                session,
                thread_id=thread_id,
                text=prompt,
                schema=schema,
                local_images=list(zip(image_paths, frame_times)),
                skill_name="",
                repair=False,
            )
            try:
                parsed = _parse_structured_output(result)
            except CodexAppServerError:
                repaired = self._runtime._run_structured_turn(
                    session,
                    thread_id=thread_id,
                    text="上一次输出格式错误。请只按 outputSchema 返回 JSON，不要解释。",
                    schema=schema,
                    local_images=list(zip(image_paths, frame_times)),
                    skill_name="",
                    repair=True,
                )
                parsed = _parse_structured_output(repaired)
        return {
            "generator": "CodexFrameEvidenceAnalyzer",
            "analysis_mode": "sparse_frame_only",
            "asset_id": asset_id,
            "frame_times": frame_times,
            "observations": parsed.get("observations", []),
            "limitations": ["只分析已附加的稀疏图片，不能代替 Gemini 的连续音视频证据。"],
            "analyzer": self.identity(),
        }

    def review_render_frames(
        self,
        image_paths: list[Path],
        *,
        stage: str,
        frame_times: list[float],
        transcript_context: list[dict[str, Any]],
        context: dict[str, Any],
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """以抽帧和 FunASR 文本复核渲染；结果显式保留非连续声画限制。"""
        if stage not in {"work_preview", "candidate"}:
            raise CodexAppServerError("抽帧渲染复核阶段不合法。")
        if not project_id:
            raise CodexAppServerError("Codex 渲染复核必须绑定项目，不能创建游离业务状态。")
        if not image_paths or len(image_paths) != len(frame_times):
            raise CodexAppServerError("Codex 渲染复核缺少图片或时间映射。")
        if len(image_paths) > MAX_FRAME_EVIDENCE_IMAGES:
            indexes = [
                round(index * (len(image_paths) - 1) / (MAX_FRAME_EVIDENCE_IMAGES - 1))
                for index in range(MAX_FRAME_EVIDENCE_IMAGES)
            ]
            image_paths = [image_paths[index] for index in indexes]
            frame_times = [frame_times[index] for index in indexes]
        for path in image_paths:
            if not path.exists():
                raise CodexAppServerError("Codex 渲染复核引用的图片不存在。")
        ready_key = "ready_for_finishing" if stage == "work_preview" else "ready_for_candidate"
        schema = _render_frame_review_schema()
        prompt = (
            "你正在复核真实渲染的代表抽帧与 FunASR 转写。只报告所给证据能支持的可观察问题；"
            "不能声称听到了未转写的声音、看到了帧间连续动作或完整视频。"
            f"阶段={stage}；抽帧对应渲染时间秒={json.dumps(frame_times)}；"
            f"转写候选={json.dumps(transcript_context, ensure_ascii=False)}；"
            f"当前上下文摘要={json.dumps(_limit_json_value(context, 20_000), ensure_ascii=False)}。"
            f"如果提供的帧和转写中没有阻止问题，且限制已被披露，才将 {ready_key} 设为 true。"
        )
        with self._runtime._session_factory(self.config) as session:
            thread_id = self._runtime._ensure_project_thread(session, project_id)
            result = self._runtime._run_structured_turn(
                session,
                thread_id=thread_id,
                text=prompt,
                schema=schema,
                local_images=list(zip(image_paths, frame_times)),
                skill_name="",
                repair=False,
            )
            try:
                parsed = _parse_structured_output(result)
            except CodexAppServerError:
                repaired = self._runtime._run_structured_turn(
                    session,
                    thread_id=thread_id,
                    text="上一次输出格式错误。请只按 outputSchema 返回 JSON，不要解释。",
                    schema=schema,
                    local_images=list(zip(image_paths, frame_times)),
                    skill_name="",
                    repair=True,
                )
                parsed = _parse_structured_output(repaired)
        return {
            "generator": "CodexFrameEvidenceAnalyzer",
            "review_basis": "codex_frames_plus_funasr_transcript_v1",
            "stage": stage,
            "frame_times": frame_times,
            "observations": parsed.get("observations", []),
            "blocking_issues": parsed.get("blocking_issues", []),
            "ready_for_finishing": parsed.get("ready_for_finishing") is True,
            "ready_for_candidate": parsed.get("ready_for_candidate") is True,
            "limitations": [
                "仅复核已附加抽帧与 FunASR 文本，不能证明帧间连续动作、非语言声音或完整声画关系。"
            ],
            "analyzer": self.identity(),
        }


def skill_output_schema(skill_name: str, mode: str | None) -> dict[str, Any]:
    """每个专业职责只开放当前阶段所需的结构化字段。"""
    if skill_name == "video-source-understanding":
        return _closed_object(
            {
                "semantic_units": _array(
                    _closed_object(
                        {
                            "asset_id": {"type": "string"},
                            "start_seconds": {"type": "number"},
                            "end_seconds": {"type": "number"},
                            "observable_facts": _string_array(),
                            "interpretation": {"type": "string"},
                            "evidence_references": _string_array(),
                            "context_needed": _string_array(),
                            "safe_use_notes": _string_array(),
                            "uncertainty": {"type": "string"},
                        }
                    )
                ),
                "relationships": _array(
                    _closed_object(
                        {
                            "source_asset_id": {"type": "string"},
                            "target_asset_id": {"type": "string"},
                            "relationship": {"type": "string"},
                            "description": {"type": "string"},
                            "evidence_references": _string_array(),
                            "uncertainty": {"type": "string"},
                        }
                    )
                ),
                "evidence_gaps": _array(
                    _closed_object(
                        {
                            "asset_id": {"type": "string"},
                            "start_seconds": {"type": "number"},
                            "end_seconds": {"type": "number"},
                            "required_modalities": _string_array(),
                            "reason": {"type": "string"},
                        }
                    )
                ),
                "unknowns": _array(
                    _closed_object(
                        {
                            "asset_id": {"type": ["string", "null"]},
                            "start_seconds": {"type": ["number", "null"]},
                            "end_seconds": {"type": ["number", "null"]},
                            "question": {"type": "string"},
                            "reason": {"type": "string"},
                        }
                    )
                ),
            }
        )
    if skill_name == "video-edit-director" and mode == "direction":
        return _closed_object(
            {
                "audience_promise": {"type": "string"},
                "structure": _array(
                    _closed_object(
                        {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "asset_ids": _string_array(),
                            "target_duration_seconds": {"type": "number"},
                            "evidence_references": _string_array(),
                            "risk": {"type": "string"},
                        }
                    )
                ),
                "selection_criteria": _string_array(),
                "style_rules": _string_array(),
                "specialist_requests": _array(
                    _closed_object(
                        {
                            "skill": {"type": "string"},
                            "reason": {"type": "string"},
                            "required_decisions": _string_array(),
                        }
                    )
                ),
                "risks": _array(_closed_object({"risk": {"type": "string"}, "mitigation": {"type": "string"}})),
            }
        )
    if skill_name == "video-edit-director" and mode == "finalize":
        return _closed_object(
            {
                "capability_requests": _capability_requests_schema(),
                "execution": _closed_object(
                    {
                        "kind": {"type": "string"},
                        "timeline_fps": {"type": "number"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "clips": _array(
                            _closed_object(
                                {
                                    "asset_id": {"type": "string"},
                                    "source_in_seconds": {"type": "number"},
                                    "source_out_seconds": {"type": "number"},
                                    "record_frame": {"type": "integer"},
                                    "video_track": {"type": "integer"},
                                    "audio_track": {"type": "integer"},
                                    "include_audio": {"type": "boolean"},
                                }
                            )
                        ),
                        # 严格 Schema 需让每个对象字段固定；不适用字段返回 null，
                        # Compiler 会先清理 null 再按具体 kind 做精确校验。
                        "operations": _array(
                            _closed_object(
                                {
                                    "kind": {"type": "string"},
                                    "capability_id": {"type": "string"},
                                    "record_frame": {"type": ["integer", "null"]},
                                    "duration_seconds": {"type": ["number", "null"]},
                                    "source_in_seconds": {"type": ["number", "null"]},
                                    "video_track": {"type": ["integer", "null"]},
                                    "audio_track": {"type": ["integer", "null"]},
                                    "target_clip_index": {"type": ["integer", "null"]},
                                }
                            )
                        ),
                    }
                ),
                "rationale": {"type": "string"},
                "unresolved": _unresolved_schema(),
            }
        )
    if skill_name in {
        "video-sound-rhythm-designer",
        "video-visual-designer",
        "video-typography-designer",
    }:
        return _closed_object(
            {
                "segments": _array(
                    _closed_object(
                        {
                            "asset_id": {"type": "string"},
                            "start_seconds": {"type": "number"},
                            "end_seconds": {"type": "number"},
                            "purpose": {"type": "string"},
                            "recommendation": {"type": "string"},
                            "constraints": _string_array(),
                            "evidence_references": _string_array(),
                        }
                    )
                ),
                "capability_requests": _capability_requests_schema(),
                "unresolved": _unresolved_schema(),
            }
        )
    if skill_name == "video-finishing-designer":
        return _closed_object(
            {
                "adjustments": _array(
                    _closed_object(
                        {
                            "asset_id": {"type": "string"},
                            "start_seconds": {"type": "number"},
                            "end_seconds": {"type": "number"},
                            "discipline": {"type": "string"},
                            "intent": {"type": "string"},
                            "constraints": _string_array(),
                        }
                    )
                ),
                "execution_changes": _array(
                    _closed_object(
                        {
                            "kind": {"type": "string"},
                            "asset_id": {"type": "string"},
                            "start_seconds": {"type": "number"},
                            "end_seconds": {"type": "number"},
                            "summary": {"type": "string"},
                        }
                    )
                ),
                "ready_for_candidate": {"type": "boolean"},
                "unresolved": _unresolved_schema(),
            }
        )
    raise CodexAppServerError(f"{skill_name}/{mode or 'default'} 没有允许的输出 Schema。")


def _frame_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "frame_time_seconds": {"type": "number"},
                        "visual_observation": {"type": "string"},
                        "uncertainty": {"type": "string"},
                        "needs_dense_review": {"type": "boolean"},
                    },
                    "required": ["frame_time_seconds", "visual_observation", "uncertainty", "needs_dense_review"],
                    "additionalProperties": False,
                },
            }
        },
        ["observations"],
    )


def _render_frame_review_schema() -> dict[str, Any]:
    """抽帧复核只允许报告可见问题与受限的阶段结论。"""
    return _closed_object(
        {
            "observations": _array(
                _closed_object(
                    {
                        "frame_time_seconds": {"type": "number"},
                        "observation": {"type": "string"},
                        "uncertainty": {"type": "string"},
                    }
                )
            ),
            "blocking_issues": _string_array(),
            "ready_for_finishing": {"type": "boolean"},
            "ready_for_candidate": {"type": "boolean"},
        }
    )


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _closed_object(properties: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 严格 JSON Schema 要求每一层对象封闭且全部字段明确必填。"""
    return _object_schema(properties, list(properties))


def _array(item_schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item_schema}


def _string_array() -> dict[str, Any]:
    return _array({"type": "string"})


def _capability_requests_schema() -> dict[str, Any]:
    return _array(
        _closed_object(
            {
                "capability_id": {"type": "string"},
                "category": {"type": "string"},
                "purpose": {"type": "string"},
            }
        )
    )


def _unresolved_schema() -> dict[str, Any]:
    return _array(
        _closed_object(
            {
                "asset_id": {"type": ["string", "null"]},
                "start_seconds": {"type": ["number", "null"]},
                "end_seconds": {"type": ["number", "null"]},
                "issue": {"type": "string"},
                "required_action": {"type": "string"},
            }
        )
    )


def _skill_assignment(skill_name: str, mode: str | None, scope: dict[str, Any]) -> str:
    mode_text = f"，模式为 {mode}" if mode else ""
    return (
        f"${skill_name} 请执行该专业职责{mode_text}。"
        "只能使用以下 project_evidence_read_only 快照和附加图片；不得调用 Resolve、不得修改项目状态、"
        "不得声明未经认证的创意资源可用，也不得假称已直接观看原始视频。"
        "若证据不足，请在输出中保留未知或最小 evidence_gaps。"
        f"\n证据快照：{json.dumps(_limit_json_value(scope), ensure_ascii=False)}"
    )


def _read_only_profile_allowed(payload: dict[str, Any]) -> bool:
    """只接受 App Server 明确允许的内置只读档案，不能回退为可写 sandbox。"""
    profiles = payload.get("data") if isinstance(payload, dict) else None
    return any(
        isinstance(profile, dict)
        and profile.get("id") == READ_ONLY_PERMISSION_PROFILE
        and profile.get("allowed") is True
        for profile in (profiles or [])
    )


def _parse_structured_output(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(candidate)
    except (ValueError, IndexError) as exc:
        raise CodexAppServerError("Codex 未返回可解析的 JSON 结构化产物。") from exc
    if not isinstance(value, dict):
        raise CodexAppServerError("Codex 结构化产物必须是对象。")
    return value


def _limit_json_value(value: Any, maximum_characters: int = 100_000) -> Any:
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) <= maximum_characters:
        return value
    # 保证 prompt 始终是合法 JSON；超长证据必须通过更小的证据范围补充，而不是整库塞给模型。
    return {
        "truncated": True,
        "reason": "证据快照超过受控上下文上限，请请求带明确时间范围的补充证据。",
        "preview": raw[:maximum_characters],
    }


def _resolve_codex_executable(configured: str) -> str:
    raw = Path(configured)
    if raw.is_absolute() and raw.exists():
        return str(raw)
    for candidate in (configured, f"{configured}.exe", f"{configured}.cmd"):
        found = shutil.which(candidate)
        if found:
            return found
    raise CodexAppServerError(f"未找到 Codex App Server 命令：{configured}")


def _error_message(value: Any) -> str:
    if isinstance(value, dict):
        return _safe_error(RuntimeError(str(value.get("message") or value)))
    return _safe_error(RuntimeError(str(value)))


def _safe_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:500]
