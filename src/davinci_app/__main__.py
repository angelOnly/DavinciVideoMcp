"""统一命令入口：API、Worker、健康检查和三组素材测试。"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from davinci_app.bootstrap import bootstrap
from davinci_app.demo import create_three_test_projects, run_until_idle
from davinci_app.system_health import check_system_health


def main() -> int:
    parser = argparse.ArgumentParser(description="DavinciMcp 本地运行入口")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("health", help="输出本机能力健康状态")
    worker = subcommands.add_parser("worker", help="运行持久任务 Worker")
    worker.add_argument("--once", action="store_true", help="只领取并执行一个任务")
    demo = subcommands.add_parser("demo", help="导入 videos 下三组测试素材并创建任务")
    demo.add_argument("--videos-root", type=Path, default=Path("videos"))
    demo.add_argument("--execute", action="store_true", help="创建后立即由当前 Worker 执行")
    api = subcommands.add_parser("api", help="启动本机 HTTP API 和测试 Web 页面")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8787)
    arguments = parser.parse_args()

    container = bootstrap()
    if arguments.command == "health":
        print(json.dumps(check_system_health(container.config), ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "worker":
        if arguments.once:
            return 0 if container.worker.run_once() else 1
        while True:
            processed = container.worker.run_once()
            if not processed:
                time.sleep(1)
    if arguments.command == "demo":
        videos_root = arguments.videos_root
        if not videos_root.is_absolute():
            videos_root = container.config.repository_root / videos_root
        runs = create_three_test_projects(container, videos_root)
        result: dict[str, object] = {"runs": [run["id"] for run in runs]}
        if arguments.execute:
            result["tasks_processed"] = run_until_idle(container)
            result["runs"] = [container.projects.get_run(run["id"]) for run in runs]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "api":
        from davinci_app.interfaces.http_server import serve

        serve(container, arguments.host, arguments.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

