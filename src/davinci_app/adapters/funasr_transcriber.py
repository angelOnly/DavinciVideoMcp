"""本机 FunASR Adapter：只使用已配置的本地权重，绝不触发下载。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from davinci_app.common import digest_json, sha256_file
from davinci_app.config import AppConfig


class FunASRAdapterError(RuntimeError):
    """FunASR 本地模型、加载或推理失败。"""


@dataclass(frozen=True)
class FunASRModelManifest:
    root: Path
    asr_model: Path
    vad_model: Path
    punc_model: Path
    speaker_model: Path | None
    configuration_hash: str

    @classmethod
    def load(cls, path: Path, *, repository_root: Path) -> "FunASRModelManifest":
        if not path.exists():
            raise FunASRAdapterError("未找到 models/manifest.yaml；不会自动下载模型。")
        values = _read_simple_manifest(path)
        if values.get("auto_download") is not False:
            raise FunASRAdapterError("FunASR manifest 必须明确设置 auto_download: false。")
        raw_root = values.get("root")
        if not isinstance(raw_root, str) or not raw_root:
            raise FunASRAdapterError("FunASR manifest 缺少 root。")
        root = _resolve_local_path(raw_root, repository_root)
        asr = _required_model_path(values, "asr_model", root)
        vad = _required_model_path(values, "vad_model", root)
        punc = _required_model_path(values, "punc_model", root)
        raw_speaker = values.get("speaker_model")
        speaker = _resolve_local_path(raw_speaker, root) if isinstance(raw_speaker, str) and raw_speaker else None
        manifest = cls(
            root=root,
            asr_model=asr,
            vad_model=vad,
            punc_model=punc,
            speaker_model=speaker,
            configuration_hash=digest_json(
                {
                    "manifest": str(path.resolve()),
                    "root": str(root),
                    "asr_model": str(asr),
                    "vad_model": str(vad),
                    "punc_model": str(punc),
                    "speaker_model": str(speaker) if speaker else None,
                    "auto_download": False,
                }
            ),
        )
        manifest.validate_files()
        return manifest

    def validate_files(self) -> None:
        for label, directory in (
            ("ASR", self.asr_model),
            ("VAD", self.vad_model),
            ("标点", self.punc_model),
        ):
            if not directory.is_dir():
                raise FunASRAdapterError(f"{label} 模型目录不存在：{directory}")
            if not (directory / "config.yaml").exists() or not (directory / "model.pt").exists():
                raise FunASRAdapterError(f"{label} 模型目录缺少 config.yaml 或 model.pt：{directory}")
        if self.speaker_model is not None and not self.speaker_model.is_dir():
            raise FunASRAdapterError(f"说话人模型目录不存在：{self.speaker_model}")

    def identity(self) -> dict[str, Any]:
        return {
            "name": "FunASR",
            "configuration_hash": self.configuration_hash,
            "models": {
                "asr": _model_fingerprint(self.asr_model),
                "vad": _model_fingerprint(self.vad_model),
                "punc": _model_fingerprint(self.punc_model),
                "speaker": _model_fingerprint(self.speaker_model) if self.speaker_model else None,
            },
        }


class FunASRTranscriberAdapter:
    """将 FunASR 不稳定的原始返回规整为统一转写证据合同。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        auto_model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._auto_model_factory = auto_model_factory
        self._manifest: FunASRModelManifest | None = None
        self._model: Any | None = None
        self._vad_model: Any | None = None

    def identity(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        return {**manifest.identity(), "adapter": "funasr-local-v1", "device": self._device()}

    def health_check(self) -> dict[str, Any]:
        """实际加载本地模型并跑随权重附带的中文样本，不用目录存在替代实测。"""
        try:
            manifest = self._load_manifest()
            sample = manifest.asr_model / "example" / "zh.mp3"
            if not sample.exists():
                raise FunASRAdapterError("FunASR ASR 模型缺少标准中文健康检查音频 example/zh.mp3。")
            result = self.transcribe(sample, source_content_hash=sha256_file(sample))
            if not result["segments"] or not any(segment.get("text") for segment in result["segments"]):
                raise FunASRAdapterError("FunASR 标准音频未返回带时间范围的可用文本。")
            return {
                "available": True,
                "identity": self.identity(),
                "sample": str(sample),
                "segment_count": len(result["segments"]),
                "reason": None,
            }
        except (FunASRAdapterError, OSError, RuntimeError, ValueError) as exc:
            return {"available": False, "reason": _safe_error(exc)}

    def transcribe(self, audio_path: Path, *, source_content_hash: str) -> dict[str, Any]:
        if not audio_path.exists() or not audio_path.is_file():
            raise FunASRAdapterError("待转写音频不存在。")
        manifest = self._load_manifest()
        model = self._load_model()
        try:
            raw_result = model.generate(
                input=str(audio_path),
                cache={},
                language="auto",
                use_itn=True,
                batch_size_s=60,
                merge_vad=True,
                merge_length_s=15,
            )
        except TypeError:
            # 兼容旧版 FunASR；模型路径仍全部是本地绝对路径，不能回退为在线 ID。
            raw_result = model.generate(input=str(audio_path), cache={}, language="auto", use_itn=True)
        vad_segments = self._run_vad(audio_path)
        segments = _normalise_transcript_segments(raw_result, vad_segments)
        text = "".join(str(item.get("text") or "") for item in segments).strip()
        return {
            "generator": "FunASRTranscriberAdapter",
            "generator_version": "funasr-local-v1",
            "source_audio_hash": source_content_hash,
            "audio_path": str(audio_path),
            "model": self.identity(),
            "segments": segments,
            "vad_segments": vad_segments,
            "speech_detected": bool(text),
            "time_precision": "segment_or_vad；不是逐词剪辑切点。",
            "raw_summary": _raw_summary(raw_result),
        }

    def _load_manifest(self) -> FunASRModelManifest:
        if self._manifest is None:
            self._manifest = FunASRModelManifest.load(
                self.config.funasr_manifest,
                repository_root=self.config.repository_root,
            )
        return self._manifest

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        manifest = self._load_manifest()
        factory = self._auto_model_factory or _import_auto_model()
        kwargs: dict[str, Any] = {
            "model": str(manifest.asr_model),
            "vad_model": str(manifest.vad_model),
            "punc_model": str(manifest.punc_model),
            "trust_remote_code": False,
            "device": self._device(),
            # 当前 FunASR 版本支持此开关；失败时下面仅移除此参数重试。
            "disable_update": True,
        }
        if manifest.speaker_model is not None:
            kwargs["spk_model"] = str(manifest.speaker_model)
        try:
            self._model = factory(**kwargs)
        except TypeError:
            kwargs.pop("disable_update", None)
            self._model = factory(**kwargs)
        except Exception as exc:  # FunASR 的具体异常类型跨版本不稳定。
            raise FunASRAdapterError(f"无法加载本地 FunASR 模型：{_safe_error(exc)}") from exc
        return self._model

    def _load_vad_model(self) -> Any:
        if self._vad_model is not None:
            return self._vad_model
        manifest = self._load_manifest()
        factory = self._auto_model_factory or _import_auto_model()
        kwargs = {
            "model": str(manifest.vad_model),
            "trust_remote_code": False,
            "device": self._device(),
            "disable_update": True,
        }
        try:
            self._vad_model = factory(**kwargs)
        except TypeError:
            kwargs.pop("disable_update", None)
            self._vad_model = factory(**kwargs)
        except Exception as exc:
            raise FunASRAdapterError(f"无法加载本地 VAD 模型：{_safe_error(exc)}") from exc
        return self._vad_model

    def _run_vad(self, audio_path: Path) -> list[dict[str, Any]]:
        try:
            raw = self._load_vad_model().generate(input=str(audio_path), cache={})
        except Exception as exc:
            raise FunASRAdapterError(f"FunASR VAD 推理失败：{_safe_error(exc)}") from exc
        intervals = _extract_vad_intervals(raw)
        if not intervals:
            # 无语音是合法结果；但保存空数组仍可和转写的 speech_detected 交叉检查。
            return []
        return [
            {
                "start_seconds": round(start / 1000.0, 3),
                "end_seconds": round(end / 1000.0, 3),
                "source": "funasr_vad",
            }
            for start, end in intervals
            if end > start >= 0
        ]

    def _device(self) -> str:
        requested = self.config.funasr_device.lower()
        if requested != "auto":
            return requested
        try:
            import torch  # 仅 Adapter 内部导入，核心模块不依赖 PyTorch。

            return "cuda:0" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"


def _read_simple_manifest(path: Path) -> dict[str, Any]:
    """本项目 manifest 只需解析一层 funasr 键，避免新增 YAML 运行时依赖。"""
    values: dict[str, Any] = {}
    in_funasr = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and line.strip() == "funasr:":
            in_funasr = True
            continue
        if not in_funasr or ":" not in line:
            continue
        key, raw_value = line.strip().split(":", 1)
        value = raw_value.strip().strip('"\'')
        if value in {"null", "~", ""}:
            values[key] = None
        elif value.lower() == "false":
            values[key] = False
        elif value.lower() == "true":
            values[key] = True
        else:
            values[key] = value
    return values


def _required_model_path(values: dict[str, Any], key: str, root: Path) -> Path:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise FunASRAdapterError(f"FunASR manifest 缺少 {key}。")
    return _resolve_local_path(value, root)


def _resolve_local_path(value: str, base: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _import_auto_model() -> Callable[..., Any]:
    try:
        from funasr import AutoModel  # type: ignore[import-not-found]
    except Exception as exc:
        raise FunASRAdapterError(f"无法导入 FunASR AutoModel：{_safe_error(exc)}") from exc
    return AutoModel


def _extract_vad_intervals(raw: Any) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for result in _result_items(raw):
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    intervals.append((float(item[0]), float(item[1])))
                except (TypeError, ValueError):
                    continue
    return intervals


def _normalise_transcript_segments(raw: Any, vad_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in _result_items(raw):
        sentence_info = result.get("sentence_info") if isinstance(result, dict) else None
        if isinstance(sentence_info, list):
            for sentence in sentence_info:
                if not isinstance(sentence, dict):
                    continue
                text = str(sentence.get("text") or "").strip()
                timing = _milliseconds_range(sentence.get("timestamp") or sentence.get("time_stamp"))
                if text and timing:
                    rows.append(
                        {
                            "start_seconds": round(timing[0] / 1000.0, 3),
                            "end_seconds": round(timing[1] / 1000.0, 3),
                            "text": text,
                            "speaker": sentence.get("speaker") or sentence.get("spk") or None,
                            "confidence": _number_or_none(sentence.get("confidence")),
                            "time_source": "funasr_sentence_timestamp",
                        }
                    )
    if rows:
        return rows
    text = " ".join(
        str(item.get("text") or "").strip() for item in _result_items(raw) if isinstance(item, dict)
    ).strip()
    if not text:
        return []
    if vad_segments:
        return [
            {
                "start_seconds": vad_segments[0]["start_seconds"],
                "end_seconds": vad_segments[-1]["end_seconds"],
                "text": text,
                "speaker": None,
                "confidence": None,
                "time_source": "funasr_vad_aggregate",
            }
        ]
    # VAD 为空时不能声称精确位置；保留明确未知，供上游拒绝关键精确切点。
    return [
        {
            "start_seconds": None,
            "end_seconds": None,
            "text": text,
            "speaker": None,
            "confidence": None,
            "time_source": "time_unavailable",
        }
    ]


def _result_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _milliseconds_range(value: Any) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        first, last = value[0], value[-1]
        if isinstance(first, (list, tuple)) and len(first) >= 2:
            first, last = first[0], last[-1]
        try:
            return float(first), float(last)
        except (TypeError, ValueError):
            return None
    return None


def _number_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_fingerprint(directory: Path | None) -> dict[str, Any] | None:
    if directory is None:
        return None
    model = directory / "model.pt"
    stat = model.stat()
    return {
        "path": str(directory),
        "model_bytes": stat.st_size,
        "model_modified_ns": stat.st_mtime_ns,
        "config_hash": sha256_file(directory / "config.yaml"),
    }


def _raw_summary(raw: Any) -> dict[str, Any]:
    """保存可审计轮廓，不把跨版本的完整 Python 对象直接写入 JSON。"""
    items = _result_items(raw)
    return {
        "result_count": len(items),
        "keys": [sorted(str(key) for key in item.keys()) for item in items],
        "json_preview": json.dumps(items, ensure_ascii=False, default=str)[:2000],
    }


def _safe_error(exc: BaseException) -> str:
    # 异常不得包含环境变量、请求头或模型外部 URL；截断即可保留可操作信息。
    return str(exc).replace("\n", " ")[:500]
