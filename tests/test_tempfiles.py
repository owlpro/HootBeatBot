import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from music_bridge.domain import MediaKind, SourceMedia
from music_bridge.tempfiles import (
    MediaTooLarge,
    delivery_workspace,
    download_bounded,
    safe_filename,
)


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def media(size: int | None = None, file_name: str = "song.mp3") -> SourceMedia:
    return SourceMedia(
        source_chat_id=7,
        source_message_id=9,
        kind=MediaKind.AUDIO,
        file_name=file_name,
        mime_type="audio/mpeg",
        size=size,
        title="Title",
        performer="Artist",
    )


def test_safe_filename_strips_paths_and_unsafe_characters() -> None:
    assert safe_filename("../../bad name?.mp3") == "bad_name_.mp3"
    assert safe_filename("") == "track.bin"


@pytest.mark.asyncio
async def test_download_streams_hashes_and_counts_bytes(tmp_path: Path) -> None:
    result = await download_bounded(media(), chunks(b"abc", b"def"), tmp_path, 10)
    assert result.size == 6
    assert result.sha256 == "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721"
    assert result.path.read_bytes() == b"abcdef"


@pytest.mark.asyncio
async def test_download_rejects_claimed_or_actual_oversize(tmp_path: Path) -> None:
    with pytest.raises(MediaTooLarge):
        await download_bounded(media(size=11), chunks(b"x"), tmp_path, 10)
    with pytest.raises(MediaTooLarge):
        await download_bounded(media(size=2), chunks(b"123", b"456"), tmp_path, 5)


@pytest.mark.asyncio
async def test_delivery_workspace_is_private_and_always_removed(tmp_path: Path) -> None:
    workspace: Path | None = None
    with pytest.raises(RuntimeError):
        async with delivery_workspace(tmp_path) as path:
            workspace = path
            assert os.stat(path).st_mode & 0o777 == 0o700
            raise RuntimeError("boom")
    assert workspace is not None and not workspace.exists()
