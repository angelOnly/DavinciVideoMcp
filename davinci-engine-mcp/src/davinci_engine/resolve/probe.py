"""仅供 bootstrap 子进程调用，禁止在此写入 Resolve。"""

from __future__ import annotations

import json
import sys

from davinci_engine.resolve.bootstrap import configure


def main() -> int:
    configured = configure()
    if not configured.available:
        print(json.dumps({"safe": False, "connected": False, "reason": configured.reason}, ensure_ascii=False))
        return 2
    try:
        import DaVinciResolveScript as resolve_script  # type: ignore[import-not-found]

        resolve = resolve_script.scriptapp("Resolve")
        print(json.dumps({"safe": True, "connected": bool(resolve)}, ensure_ascii=False))
        return 0 if resolve else 10
    except BaseException as exc:  # 子进程隔离原生导入，主进程不能冒险。
        print(json.dumps({"safe": False, "connected": False, "reason": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

