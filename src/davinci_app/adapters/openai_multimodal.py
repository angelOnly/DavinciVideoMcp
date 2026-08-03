"""OpenAI-compatible 多模态 Adapter；供应商协议细节只留在此处。"""

from __future__ import annotations

import base64
import json
import mimetypes
import tempfile
import wave
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from davinci_app.common import digest_json, run_command
from davinci_app.config import AppConfig
from davinci_app.media.probe import ffmpeg_path


class MultimodalAdapterError(RuntimeError):
    """端点、协议或结构化输出不满足本项目的媒体证据合同。"""


HttpTransport = Callable[[str, dict[str, Any], int, dict[str, str]], dict[str, Any]]


class OpenAICompatibleMultimodalAdapter:
    """把已授权的分析代理提交到 OpenAI-compatible chat completions 端点。"""

    VERSION = "openai-compatible-multimodal-v1"

    def __init__(self, config: AppConfig, *, transport: HttpTransport | None = None) -> None:
        self.config = config
        self._transport = transport or _post_json
        self._health_cache: dict[str, Any] | None = None

    def identity(self) -> dict[str, Any]:
        return {
            "name": "OpenAICompatibleMultimodalAdapter",
            "version": self.VERSION,
            "model": self.config.multimodal_model,
            # 不输出 API Key；端点仅用于审计分析器身份。
            "base_url": self.config.multimodal_base_url,
            "configuration_hash": digest_json(
                {
                    "version": self.VERSION,
                    "model": self.config.multimodal_model,
                    "base_url": self.config.multimodal_base_url,
                    "max_segment_seconds": self.config.multimodal_max_segment_seconds,
                }
            ),
        }

    def health_check(self) -> dict[str, Any]:
        """逐项实际请求文本、图片、WAV、带声 MP4 与 JSON，不根据模型名猜能力。"""
        if self._health_cache is not None:
            return self._health_cache
        if not self.config.multimodal_base_url or not self.config.multimodal_api_key:
            self._health_cache = {
                "available": False,
                "model": self.config.multimodal_model,
                "reason": "未配置本机多模态端点或密钥；系统不会按模型名称虚报能力。",
                "capabilities": _empty_capabilities(),
            }
            return self._health_cache

        checks: dict[str, bool] = {}
        errors: dict[str, str] = {}
        try:
            response = self._request_structured(
                "请仅返回 JSON，确认你收到的是文本。",
                attachments=[],
                schema=_probe_schema(),
            )
            checks["supports_text"] = _has_modality(response, "text")
            if not checks["supports_text"]:
                errors["text"] = "端点未确认文本输入。"
        except MultimodalAdapterError as exc:
            checks["supports_text"] = False
            errors["text"] = _safe_error(exc)

        image = _tiny_png_bytes()
        try:
            response = self._request_structured(
                "这是一张测试图片。请仅返回 JSON，并把 image 放入 modalities_observed。",
                attachments=[("image", image, "image/png")],
                schema=_probe_schema(),
            )
            checks["supports_image"] = _has_modality(response, "image")
            if not checks["supports_image"]:
                errors["image"] = "端点没有确认图片输入。"
        except MultimodalAdapterError as exc:
            checks["supports_image"] = False
            errors["image"] = _safe_error(exc)

        wav = _tiny_wav_bytes()
        try:
            response = self._request_structured(
                "这是一段测试 WAV 音频。请仅返回 JSON，并把 audio 放入 modalities_observed。",
                attachments=[("audio", wav, "audio/wav")],
                schema=_probe_schema(),
            )
            checks["supports_audio"] = _has_modality(response, "audio")
            if not checks["supports_audio"]:
                errors["audio"] = "端点没有确认 WAV 音频输入。"
        except MultimodalAdapterError as exc:
            checks["supports_audio"] = False
            errors["audio"] = _safe_error(exc)

        try:
            with _standard_video_probe() as video_path:
                response = self._request_structured(
                    "这是 5 秒黑色视频和 440Hz 声音的测试 MP4。请仅返回 JSON，"
                    "把 video 与 audio 都放入 modalities_observed。",
                    attachments=[("video", video_path.read_bytes(), "video/mp4")],
                    schema=_probe_schema(),
                )
            checks["supports_video"] = _has_modality(response, "video")
            checks["supports_video_audio"] = checks["supports_video"] and _has_modality(response, "audio")
            if not checks["supports_video"]:
                errors["video"] = "端点没有确认 MP4 视频输入。"
            elif not checks["supports_video_audio"]:
                errors["video_audio"] = "端点没有确认同时读取 MP4 画面与音轨。"
        except MultimodalAdapterError as exc:
            checks["supports_video"] = False
            checks["supports_video_audio"] = False
            errors["video"] = _safe_error(exc)

        # 输出结构化能力与输入模态能力独立：有些 OpenAI-compatible 反代能对文本、
        # 图片稳定返回 JSON，却根本不接受 WAV 或 MP4。后者不能反向否定图片降级链路。
        checks["supports_structured_output"] = (
            checks.get("supports_text") is True and checks.get("supports_image") is True
        )
        capabilities = _empty_capabilities()
        capabilities.update(checks)
        available = bool(
            capabilities["supports_text"]
            and capabilities["supports_image"]
            and capabilities["supports_audio"]
            and capabilities["supports_video_audio"]
            and capabilities["supports_structured_output"]
        )
        self._health_cache = {
            "available": available,
            "model": self.config.multimodal_model,
            "reason": None if available else "多模态端点未通过全部实际模态与结构化输出探测。",
            "capabilities": capabilities,
            "errors": errors,
            "identity": self.identity(),
        }
        return self._health_cache

    def analyze_video_segment(
        self,
        video_path: Path,
        *,
        asset_id: str,
        source_start_seconds: float,
        source_end_seconds: float,
        transcript_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        health = self.health_check()
        capabilities = health.get("capabilities") or {}
        if capabilities.get("supports_video_audio") is not True:
            raise MultimodalAdapterError("当前端点未实测通过带声音 MP4；不能宣称直接音视频理解。")
        if not video_path.exists():
            raise MultimodalAdapterError("待分析的视频代理不存在。")
        prompt = _media_prompt(
            asset_id=asset_id,
            source_start_seconds=source_start_seconds,
            source_end_seconds=source_end_seconds,
            transcript_context=transcript_context,
            mode="direct_video_audio",
        )
        payload = self._request_structured(
            prompt,
            attachments=[("video", video_path.read_bytes(), "video/mp4")],
            schema=_media_evidence_schema(),
        )
        return _normalise_observations(
            payload,
            asset_id=asset_id,
            source_start_seconds=source_start_seconds,
            source_end_seconds=source_end_seconds,
            mode="direct_video_audio",
            analyzer=self.identity(),
        )

    def analyze_image_evidence(
        self,
        image_paths: list[Path],
        *,
        asset_id: str,
        frame_times: list[float],
        transcript_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        health = self.health_check()
        capabilities = health.get("capabilities") or {}
        if capabilities.get("supports_image") is not True:
            raise MultimodalAdapterError("当前端点未实测通过图片输入。")
        if not image_paths or len(image_paths) != len(frame_times):
            raise MultimodalAdapterError("图片证据与时间映射不完整。")
        attachments = []
        for path in image_paths:
            if not path.exists():
                raise MultimodalAdapterError("图片证据文件不存在。")
            attachments.append(("image", path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/jpeg"))
        prompt = _image_prompt(asset_id, frame_times, transcript_context)
        payload = self._request_structured(prompt, attachments=attachments, schema=_media_evidence_schema())
        start = min(frame_times)
        end = max(frame_times)
        result = _normalise_observations(
            payload,
            asset_id=asset_id,
            source_start_seconds=start,
            source_end_seconds=end,
            mode="image_evidence_only",
            analyzer=self.identity(),
            source_time_coordinates=True,
        )
        result["limitations"] = ["仅依据稀疏图片与转写，不能声称已理解连续视频或音轨。"]
        result["frame_times"] = frame_times
        return result

    def review_video_segment(
        self,
        video_path: Path,
        *,
        stage: str,
        source_start_seconds: float,
        source_end_seconds: float,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """对真实渲染片段作观看复核；技术规格仍由 Engine QC 独立证明。"""
        health = self.health_check()
        if (health.get("capabilities") or {}).get("supports_video_audio") is not True:
            raise MultimodalAdapterError("当前端点未实测通过带声音 MP4，不能复核渲染。")
        if stage not in {"work_preview", "candidate"}:
            raise MultimodalAdapterError("渲染复核阶段不合法。")
        if not video_path.exists():
            raise MultimodalAdapterError("待复核视频片段不存在。")
        ready_key = "ready_for_finishing" if stage == "work_preview" else "ready_for_candidate"
        prompt = (
            "你正在复核一个真实渲染片段。只报告可观察的声画、文字、连续性或可懂度问题；"
            "不要重新选片、不要给 Resolve 操作或技术规格结论。"
            f"阶段={stage}；该片段在完整渲染中的时间={source_start_seconds:.3f}-{source_end_seconds:.3f} 秒。"
            f"当前已批准上下文摘要={json.dumps(_limited_context(context), ensure_ascii=False)}。"
            f"只有在本片段没有阻止进入下一阶段的问题时，才将 {ready_key} 设为 true。"
        )
        raw = self._request_structured(
            prompt,
            attachments=[("video", video_path.read_bytes(), "video/mp4")],
            schema=_render_review_schema(),
        )
        blocking = raw.get("blocking_issues")
        observations = raw.get("observations")
        if not isinstance(blocking, list) or not isinstance(observations, list):
            raise MultimodalAdapterError("渲染复核输出缺少 blocking_issues 或 observations。")
        return {
            "stage": stage,
            "source_range_seconds": [round(source_start_seconds, 3), round(source_end_seconds, 3)],
            "observations": observations,
            "blocking_issues": [str(item) for item in blocking],
            "ready_for_finishing": raw.get("ready_for_finishing") is True,
            "ready_for_candidate": raw.get("ready_for_candidate") is True,
            "analyzer": self.identity(),
        }

    def _request_structured(
        self,
        prompt: str,
        *,
        attachments: list[tuple[str, bytes, str]],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        url = _chat_completion_url(self.config.multimodal_base_url)
        # 一些反代忽略 response_format；将同一 Schema 写进提示词，仍要求严格 JSON。
        content: list[dict[str, Any]] = [{"type": "text", "text": _structured_prompt(prompt, schema)}]
        for kind, data, media_type in attachments:
            encoded = base64.b64encode(data).decode("ascii")
            data_url = f"data:{media_type};base64,{encoded}"
            if kind == "image":
                content.append({"type": "image_url", "image_url": {"url": data_url}})
            elif kind == "audio":
                content.append({"type": "input_audio", "input_audio": {"data": encoded, "format": "wav"}})
            elif kind == "video":
                content.append({"type": "video_url", "video_url": {"url": data_url}})
            else:
                raise MultimodalAdapterError(f"不支持的多模态附件类型：{kind}")
        request_body = {
            "model": self.config.multimodal_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你只生成可观察媒体证据，不选择剪辑内容、不设计音乐、字幕、动效或 Resolve 操作。",
                },
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "media_evidence", "strict": True, "schema": schema},
            },
        }
        headers = {"Authorization": f"Bearer {self.config.multimodal_api_key}", "Content-Type": "application/json"}
        try:
            response = self._transport(url, request_body, self.config.multimodal_timeout_seconds, headers)
        except (OSError, HTTPError, URLError, TimeoutError) as exc:
            raise MultimodalAdapterError(f"多模态端点请求失败：{_safe_error(exc)}") from exc
        except Exception as exc:
            raise MultimodalAdapterError(f"多模态端点调用异常：{_safe_error(exc)}") from exc
        return _parse_completion(response)


def _post_json(url: str, body: dict[str, Any], timeout_seconds: int, headers: dict[str, str]) -> dict[str, Any]:
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - URL 只来自本机安全配置。
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        # 不回显请求头或 Key；仅保留状态码和服务端安全尾部信息。
        detail = exc.read().decode("utf-8", errors="replace")[-400:]
        raise MultimodalAdapterError(f"多模态端点返回 HTTP {exc.code}：{detail}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MultimodalAdapterError("多模态端点返回的 JSON 无法解析。") from exc
    if not isinstance(value, dict):
        raise MultimodalAdapterError("多模态端点返回不是对象。")
    return value


def _parse_completion(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise MultimodalAdapterError("多模态端点未返回 choices。")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise MultimodalAdapterError("多模态端点未返回 assistant message。")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
    if not isinstance(content, str):
        raise MultimodalAdapterError("多模态端点没有可解析的结构化内容。")
    parsed = _parse_json_object(content)
    if not isinstance(parsed, dict):
        raise MultimodalAdapterError("多模态结构化输出必须是对象。")
    return parsed


def _structured_prompt(prompt: str, schema: dict[str, Any]) -> str:
    """兼容忽略 response_format 的反代，且不降低本地结构化输出要求。"""
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{prompt}\n\n"
        "输出协议：只返回一个 JSON 对象，不要解释、不要 Markdown 代码块、不要额外字段。"
        f"返回对象必须满足以下 JSON Schema：{schema_text}"
    )


def _parse_json_object(content: str) -> Any:
    """仅接受纯 JSON 或单个 fenced JSON，拒绝夹带解释的宽松解析。"""
    candidate = content.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise MultimodalAdapterError("多模态端点没有遵守 JSON 结构化输出合同。")
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise MultimodalAdapterError("多模态端点没有遵守 JSON 结构化输出合同。") from exc


def _chat_completion_url(base_url: str | None) -> str:
    if not base_url:
        raise MultimodalAdapterError("未配置多模态端点。")
    clean = base_url.rstrip("/")
    return clean if clean.endswith("/chat/completions") else f"{clean}/chat/completions"


def _empty_capabilities() -> dict[str, bool]:
    return {
        "supports_text": False,
        "supports_image": False,
        "supports_audio": False,
        "supports_video": False,
        "supports_video_audio": False,
        "supports_structured_output": False,
    }


def _probe_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "modalities_observed": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
        },
        "required": ["modalities_observed", "summary"],
        "additionalProperties": False,
    }


def _media_evidence_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start_seconds": {"type": "number"},
                        "end_seconds": {"type": "number"},
                        "visual_observation": {"type": "string"},
                        "audio_observation": {"type": "string"},
                        "audio_visual_relation": {"type": "string"},
                        "uncertainty": {"type": "string"},
                        "needs_dense_review": {"type": "boolean"},
                    },
                    "required": [
                        "start_seconds",
                        "end_seconds",
                        "visual_observation",
                        "audio_observation",
                        "audio_visual_relation",
                        "uncertainty",
                        "needs_dense_review",
                    ],
                    "additionalProperties": False,
                },
            },
            "overall_uncertainty": {"type": "string"},
        },
        "required": ["observations", "overall_uncertainty"],
        "additionalProperties": False,
    }


def _render_review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "observations": {"type": "array", "items": {"type": "object"}},
            "blocking_issues": {"type": "array", "items": {"type": "string"}},
            "ready_for_finishing": {"type": "boolean"},
            "ready_for_candidate": {"type": "boolean"},
        },
        "required": ["observations", "blocking_issues", "ready_for_finishing", "ready_for_candidate"],
        "additionalProperties": False,
    }


def _has_modality(value: dict[str, Any], modality: str) -> bool:
    raw = value.get("modalities_observed")
    return isinstance(raw, list) and modality in raw


def _media_prompt(
    *,
    asset_id: str,
    source_start_seconds: float,
    source_end_seconds: float,
    transcript_context: list[dict[str, Any]],
    mode: str,
) -> str:
    return (
        "为已授权的视频代理生成可审计的媒体观察。不要选择成片片段，不要编造精确帧级切点。"
        f"素材={asset_id}；源时间范围={source_start_seconds:.3f}-{source_end_seconds:.3f} 秒；模式={mode}。"
        "每条 observation 的时间必须相对于该代理片段开始，且只描述可观察的画面、声音和声画关系。"
        "快速动作、微表情、画内小字或时间边界不确定时必须写 needs_dense_review=true。"
        f"转写候选（只作语言参考，不证明画面）：{json.dumps(transcript_context, ensure_ascii=False)}"
    )


def _image_prompt(asset_id: str, frame_times: list[float], transcript_context: list[dict[str, Any]]) -> str:
    return (
        "为稀疏抽帧生成补充视觉证据。不能声称看过帧之间的连续运动、音频或整个原视频。"
        f"素材={asset_id}；图片依次对应源时间秒={json.dumps(frame_times)}。"
        "observation 的 start_seconds/end_seconds 必须对应一张或相邻图片的时间；"
        "所有非静态结论、精确切点、微表情和画内小字都应 needs_dense_review=true。"
        f"转写候选：{json.dumps(transcript_context, ensure_ascii=False)}"
    )


def _normalise_observations(
    raw: dict[str, Any],
    *,
    asset_id: str,
    source_start_seconds: float,
    source_end_seconds: float,
    mode: str,
    analyzer: dict[str, Any],
    source_time_coordinates: bool = False,
) -> dict[str, Any]:
    observations = raw.get("observations")
    if not isinstance(observations, list):
        raise MultimodalAdapterError("多模态结构化输出缺少 observations 数组。")
    duration = max(0.0, source_end_seconds - source_start_seconds)
    normalised: list[dict[str, Any]] = []
    for item in observations:
        if not isinstance(item, dict):
            raise MultimodalAdapterError("多模态 observation 必须是对象。")
        try:
            relative_start = float(item["start_seconds"])
            relative_end = float(item["end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MultimodalAdapterError("多模态 observation 缺少有效时间范围。") from exc
        if source_time_coordinates:
            # 图片提示中已经要求源时间，不能再按片段偏移叠加一次。
            if (
                relative_start < source_start_seconds - 0.1
                or relative_end < relative_start
                or relative_end > source_end_seconds + 0.1
            ):
                raise MultimodalAdapterError("图片多模态 observation 时间超出已附加抽帧范围，已拒绝写入证据。")
            absolute_start = max(source_start_seconds, relative_start)
            absolute_end = min(source_end_seconds, relative_end)
        else:
            if relative_start < -0.1 or relative_end < relative_start or relative_end > duration + 0.1:
                raise MultimodalAdapterError("多模态 observation 时间超出当前代理片段，已拒绝写入证据。")
            absolute_start = max(source_start_seconds, source_start_seconds + relative_start)
            absolute_end = min(source_end_seconds, source_start_seconds + relative_end)
        normalised.append(
            {
                "asset_id": asset_id,
                "start_seconds": round(absolute_start, 3),
                "end_seconds": round(absolute_end, 3),
                "visual_observation": str(item.get("visual_observation") or ""),
                "audio_observation": str(item.get("audio_observation") or ""),
                "audio_visual_relation": str(item.get("audio_visual_relation") or ""),
                "uncertainty": str(item.get("uncertainty") or ""),
                "needs_dense_review": bool(item.get("needs_dense_review")),
                "source_mode": mode,
            }
        )
    return {
        "generator": "OpenAICompatibleMultimodalAdapter",
        "analysis_mode": mode,
        "asset_id": asset_id,
        "source_range_seconds": [round(source_start_seconds, 3), round(source_end_seconds, 3)],
        "observations": normalised,
        "overall_uncertainty": str(raw.get("overall_uncertainty") or ""),
        "analyzer": analyzer,
    }


def _tiny_png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
        "N2Uu8QAAAABJRU5ErkJggg=="
    )


def _tiny_wav_bytes() -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


class _standard_video_probe:
    """按需生成有画面和音轨的本地小样本，临时文件离开上下文即删除。"""

    def __enter__(self) -> Path:
        self._directory = tempfile.TemporaryDirectory(prefix="davinci-multimodal-health-")
        self.path = Path(self._directory.name) / "video-audio-probe.mp4"
        command = [
            ffmpeg_path(),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(self.path),
        ]
        completed = run_command(command, timeout_seconds=60)
        if completed.returncode != 0 or not self.path.exists():
            raise MultimodalAdapterError("无法生成多模态健康检查用的带声音 MP4。")
        return self.path

    def __exit__(self, *_: Any) -> None:
        self._directory.cleanup()


def _safe_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:500]


def _limited_context(value: dict[str, Any], maximum: int = 12_000) -> Any:
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) <= maximum:
        return value
    return {"truncated": True, "preview": raw[:maximum]}
