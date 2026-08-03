"""认证创意能力交给 Resolve 前的受管本地化。

Nextcloud 中的素材名称和路径可以包含中文；部分 Resolve 原生接口和旧插件不能稳定
处理这些路径。因此只把经过 Catalog 绑定且哈希一致的文件复制到工程内的 ASCII
内容寻址缓存，Resolve 永远不直接读取云盘源路径。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from davinci_app.common import is_windows_placeholder, sha256_file


class AssetLocalizationError(RuntimeError):
    """认证资源无法安全本地化。"""


@dataclass(frozen=True)
class LocalizedCreativeAsset:
    """源文件与缓存文件均通过同一 SHA-256 校验后的本地对象。"""

    local_path: Path
    cache_relative_path: str
    content_hash: str
    byte_count: int
    reused_existing_object: bool

    def to_evidence(self) -> dict[str, object]:
        return {
            "strategy": "content_addressed_local_cache_v1",
            "cache_relative_path": self.cache_relative_path,
            "content_hash": self.content_hash,
            "byte_count": self.byte_count,
            "reused_existing_object": self.reused_existing_object,
        }


def localize_verified_asset(
    source_path: Path,
    expected_content_hash: str,
    cache_root: Path,
) -> LocalizedCreativeAsset:
    """复制已绑定的认证文件，并在复制前后都校验内容哈希。

    缓存文件名不使用采购包文件名，避免中文、编号或同名文件影响 Resolve 调用。
    发现同哈希缓存对象不一致时不覆盖它，防止掩盖磁盘损坏或并发异常。
    """

    if not re.fullmatch(r"[0-9a-f]{64}", expected_content_hash):
        raise AssetLocalizationError("认证能力的内容哈希格式无效。")
    source = source_path.resolve()
    if not source.is_file() or is_windows_placeholder(source):
        raise AssetLocalizationError("认证能力尚未完整本地化，不能交给 Resolve。")
    if sha256_file(source) != expected_content_hash:
        raise AssetLocalizationError("认证能力源文件的内容哈希已变化。")

    suffix = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        suffix = ".bin"
    root = cache_root.resolve()
    relative = Path("objects") / expected_content_hash / f"asset{suffix}"
    destination = root / relative
    try:
        str(destination).encode("ascii")
    except UnicodeEncodeError as exc:
        raise AssetLocalizationError("创意缓存路径必须仅包含 ASCII 字符。") from exc

    if destination.is_file():
        if sha256_file(destination) != expected_content_hash:
            raise AssetLocalizationError("同哈希创意缓存对象内容不一致，请先人工检查。")
        return LocalizedCreativeAsset(
            local_path=destination,
            cache_relative_path=relative.as_posix(),
            content_hash=expected_content_hash,
            byte_count=destination.stat().st_size,
            reused_existing_object=True,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        if sha256_file(temporary) != expected_content_hash:
            raise AssetLocalizationError("复制到创意缓存后的内容哈希不一致。")
        # 不覆盖另一个并发任务可能已经完成的对象；它必须先通过同样的哈希检查。
        if destination.exists():
            if sha256_file(destination) != expected_content_hash:
                raise AssetLocalizationError("并发写入的创意缓存对象内容不一致。")
        else:
            os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    if not destination.is_file() or sha256_file(destination) != expected_content_hash:
        raise AssetLocalizationError("创意缓存写入后无法确认文件身份。")
    return LocalizedCreativeAsset(
        local_path=destination,
        cache_relative_path=relative.as_posix(),
        content_hash=expected_content_hash,
        byte_count=destination.stat().st_size,
        reused_existing_object=False,
    )
