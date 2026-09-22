"""Safe bounded temporary media handling."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from music_bridge.domain import SourceMedia


class MediaTooLarge(ValueError):
    """Media metadata or streamed bytes exceed the configured bound."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    size: int
    sha256: str


def safe_filename(value: str | None) -> str:
    name = Path(value or "track.bin").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")
    return cleaned[:180] or "track.bin"


@asynccontextmanager
async def delivery_workspace(root: Path | None = None) -> AsyncIterator[Path]:
    root.mkdir(parents=True, exist_ok=True) if root else None
    path = Path(tempfile.mkdtemp(prefix="music-bridge-", dir=root))
    path.chmod(0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def download_bounded(
    media: SourceMedia,
    stream: AsyncIterator[bytes],
    directory: Path,
    max_bytes: int,
) -> DownloadResult:
    """Stream to disk while enforcing the true byte limit and computing SHA-256."""
    if media.size is not None and media.size > max_bytes:
        raise MediaTooLarge(f"declared media size exceeds {max_bytes} bytes")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / safe_filename(media.file_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("xb") as target:
            async for chunk in stream:
                total += len(chunk)
                if total > max_bytes:
                    raise MediaTooLarge(f"download exceeds {max_bytes} bytes")
                digest.update(chunk)
                target.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return DownloadResult(path=path, size=total, sha256=digest.hexdigest())
