"""创意能力入库前的只读静态预检 CLI。

此命令不会连接 Resolve、不会部署文件、不会修改 Catalog；它只能提前筛掉没有安全
Adapter、哈希不一致或明显含脚本风险的资源。通过预检仍必须完成五步实机认证。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from davinci_engine.common import sha256_file
from davinci_engine.creative.adapters import CreativeAdapterError, default_adapter_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="对单个本地化创意能力执行只读 Adapter 预检")
    parser.add_argument("--mechanism", required=True, help="明确机制，例如 lut_3d 或 fusion_effect")
    parser.add_argument("--asset", required=True, type=Path, help="已完整本地化的单个文件")
    parser.add_argument("--expected-hash", help="可选 SHA-256；提供后必须与文件一致")
    parser.add_argument("--constraints-json", default="{}", help="受控约束 JSON 对象")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        constraints = json.loads(args.constraints_json)
        if not isinstance(constraints, dict):
            raise ValueError("--constraints-json 必须是对象。")
        asset = args.asset.resolve()
        if not asset.is_file():
            raise ValueError("素材文件不存在。")
        actual_hash = sha256_file(asset)
        if args.expected_hash and actual_hash != args.expected_hash.lower():
            raise ValueError("素材 SHA-256 与 --expected-hash 不一致。")
        result = default_adapter_registry().preflight(args.mechanism, asset, constraints)
        print(
            json.dumps(
                {
                    "asset": str(asset),
                    "content_hash": actual_hash,
                    "mechanism": result.mechanism,
                    "ready_for_live_certification": result.ready_for_live_certification,
                    "details": result.details,
                    "reason": result.reason,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.ready_for_live_certification else 2
    except (CreativeAdapterError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ready_for_live_certification": False, "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
