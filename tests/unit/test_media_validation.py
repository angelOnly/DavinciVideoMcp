from __future__ import annotations

import unittest

from davinci_app.media.probe import MediaProbe
from davinci_app.media.validation import UploadValidator


def _probe(codec: str, profile: str | None = None) -> MediaProbe:
    return MediaProbe(
        path="C:/media.mp4",
        duration_seconds=10.0,
        format_name="mp4",
        video_streams=1,
        audio_streams=1,
        width=1920,
        height=1080,
        average_frame_rate=30.0,
        real_frame_rate=30.0,
        video_codec=codec,
        video_profile=profile,
        pixel_format="yuv420p",
        audio_codec="aac",
        sample_rate=48000,
        rotation=None,
    )


class UploadValidatorWorkingCopyTests(unittest.TestCase):
    def test_hevc_and_baseline_h264_get_resolve_compatible_copy(self) -> None:
        self.assertTrue(UploadValidator._requires_working_copy(_probe("hevc", "Main")))
        self.assertTrue(UploadValidator._requires_working_copy(_probe("h264", "Constrained Baseline")))

    def test_intermediate_codec_does_not_need_extra_transcode(self) -> None:
        self.assertFalse(UploadValidator._requires_working_copy(_probe("prores", "Standard")))

