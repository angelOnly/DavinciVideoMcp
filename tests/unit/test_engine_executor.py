from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from davinci_engine.execution.executor import EngineExecutor


class _Timeline:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tracks = {"video": 0, "audio": 0, "subtitle": 0}

    def GetName(self) -> str:
        return self.name

    def GetTrackCount(self, kind: str) -> int:
        return self.tracks[kind]

    def AddTrack(self, kind: str) -> None:
        # 模拟 Resolve 21：副作用已发生，但 Python Bridge 返回 None。
        self.tracks[kind] += 1
        return None

    def SetTrackName(self, _kind: str, _index: int, _name: str) -> None:
        return None


class _Project:
    def __init__(self) -> None:
        self.timelines: list[_Timeline] = []
        self.current: _Timeline | None = None

    def GetTimelineCount(self) -> int:
        return len(self.timelines)

    def GetTimelineByIndex(self, index: int) -> _Timeline:
        return self.timelines[index - 1]

    def SetCurrentTimeline(self, timeline: _Timeline) -> None:
        # 同样模拟成功但无返回值。
        self.current = timeline
        return None

    def GetCurrentTimeline(self) -> _Timeline | None:
        return self.current


class _MediaPool:
    def __init__(self, project: _Project) -> None:
        self.project = project

    def CreateEmptyTimeline(self, name: str) -> None:
        timeline = _Timeline(name)
        self.project.timelines.append(timeline)
        self.project.current = timeline
        return None


class _RenderProject:
    def __init__(self) -> None:
        self.current = {"format": "mov", "codec": ""}
        self.format_codec_calls: list[tuple[str, str]] = []
        self.render_settings: dict[str, object] | None = None

    def GetRenderFormats(self) -> dict[str, str]:
        # Resolve 21：显示名称映射到实际 API 标识符。
        return {"MP4": "mp4"}

    def GetRenderCodecs(self, format_id: str) -> dict[str, str]:
        assert format_id == "mp4"
        return {"H.264": "H264"}

    def SetCurrentRenderFormatAndCodec(self, format_id: str, codec_id: str) -> None:
        self.format_codec_calls.append((format_id, codec_id))
        self.current = {"format": format_id, "codec": codec_id}
        # 模拟 Resolve 21：成功后没有布尔返回值。
        return None

    def GetCurrentRenderFormatAndCodec(self) -> dict[str, str]:
        return self.current

    def SetRenderSettings(self, settings: dict[str, object]) -> None:
        self.render_settings = settings
        return None


class _CompletedRenderProject:
    def IsRenderingInProgress(self) -> bool:
        return False

    def GetRenderJobStatus(self, _job_id: str) -> dict[str, object]:
        return {"JobStatus": "完成", "CompletionPercentage": 100}


class EngineExecutorReadbackTests(unittest.TestCase):
    def test_create_timeline_uses_readback_when_resolve_returns_none(self) -> None:
        project = _Project()
        pool = _MediaPool(project)
        project.GetMediaPool = lambda: pool  # type: ignore[attr-defined]
        executor = EngineExecutor(connection=None, journal=None)  # type: ignore[arg-type]

        timeline, clips_already_placed = executor._create_timeline(
            project,
            SimpleNamespace(timeline_name="run_test", clips=()),
            {},
        )

        self.assertEqual("run_test", timeline.GetName())
        self.assertFalse(clips_already_placed)
        self.assertIs(timeline, project.GetCurrentTimeline())

    def test_ensure_tracks_reads_track_count_instead_of_return_value(self) -> None:
        timeline = _Timeline("run_test")

        EngineExecutor._ensure_tracks(timeline, "video", 2)

        self.assertEqual(2, timeline.GetTrackCount("video"))

    def test_render_configuration_uses_api_identifiers_and_readback(self) -> None:
        project = _RenderProject()

        EngineExecutor._configure_h264_render(project, _Timeline("run_test"), Path("E:/renders/candidate.mp4"))

        self.assertEqual([("mp4", "H264")], project.format_codec_calls)
        self.assertEqual("candidate", project.render_settings["CustomName"])

    def test_completed_render_accepts_localized_status(self) -> None:
        EngineExecutor._wait_for_render(_CompletedRenderProject(), "job-1")
