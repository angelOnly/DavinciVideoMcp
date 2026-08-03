"""用仓库 videos 目录中的三组真实素材创建 Engine 冒烟测试任务。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from davinci_app.bootstrap import ApplicationContainer


def create_three_test_projects(container: ApplicationContainer, videos_root: Path) -> list[dict[str, Any]]:
    groups = [
        {
            "title": "游泳 Vlog Engine 技术预览",
            "directory": videos_root / "游泳vlog",
            "role": "primary",
            "brief": {
                "testing_preset": "fragment_montage",
                "orientation": "portrait",
                "max_clip_seconds": 8,
                "note": "测试预设：按导入顺序拼接短片段，不代替专业素材理解和 EditPlan。",
            },
        },
        {
            "title": "猫咪日常 Engine 技术预览",
            "directory": videos_root / "猫咪日常",
            "role": "primary",
            "brief": {
                "testing_preset": "fragment_montage",
                "orientation": "portrait",
                "max_clip_seconds": 6,
                "note": "测试预设：按导入顺序拼接短片段，不代替专业素材理解和 EditPlan。",
            },
        },
        {
            "title": "Tim O'Reilly 访谈 Engine 技术预览",
            "files": [videos_root / "tunisia-tim_oreilly_2-h264_720p_512kb.mp4"],
            "role": "interview",
            "brief": {
                "testing_preset": "interview_excerpt",
                "orientation": "landscape",
                "max_duration_seconds": 90,
                "note": "测试预设：在转写与用户简报缺失时只生成开场节选，不伪造采访语义精选。",
            },
        },
    ]
    runs: list[dict[str, Any]] = []
    for group in groups:
        project = container.projects.create_project(group["title"], group["brief"])
        files = group.get("files") or sorted(group["directory"].glob("*.mp4"), key=lambda path: path.stat().st_mtime)
        if not files:
            raise RuntimeError(f"测试素材目录中没有 MP4 文件：{group.get('directory')}")
        for source in files:
            asset = container.projects.import_local_file(project["id"], source, role=group["role"])
            if asset["state"] != "ready":
                raise RuntimeError(f"测试素材校验失败：{source.name}：{asset['errors']}")
        # 三组素材只验证 Resolve 写入与渲染；不允许误入正式候选链路。
        runs.append(container.projects.freeze_run(project["id"], kind="engine_smoke"))
    return runs


def run_until_idle(container: ApplicationContainer) -> int:
    completed = 0
    while container.worker.run_once():
        completed += 1
    return completed
