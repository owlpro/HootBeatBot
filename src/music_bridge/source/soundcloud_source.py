"""Direct SoundCloud source backed by yt-dlp and ffprobe."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

from music_bridge.domain import MediaKind, SourceMedia, SourceRequest


class SoundCloudDownloadError(RuntimeError):
    """A safely classified yt-dlp or ffprobe execution failure."""


class SoundCloudValidationError(RuntimeError):
    """SoundCloud metadata or downloaded audio failed validation."""


_RESERVED_PATHS = {"search", "discover", "stream", "you", "charts", "upload"}


def canonical_track_url(value: str) -> str | None:
    """Validate and canonicalize a public HTTPS SoundCloud track URL."""
    parsed = urlparse(value.strip())
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or hostname not in {"soundcloud.com", "www.soundcloud.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0].casefold() in _RESERVED_PATHS:
        return None
    return urlunparse(("https", "soundcloud.com", f"/{parts[0]}/{parts[1]}", "", "", ""))


def is_url_like(value: str) -> bool:
    """Distinguish URL-shaped configuration from colon-bearing search text."""
    parsed = urlparse(value.strip())
    return parsed.netloc != "" or parsed.scheme in {"http", "https"} or "://" in value


def source_message_id_for_url(url: str) -> int:
    """Return the stable positive SQLite-safe identity for a SoundCloud URL."""
    digest = hashlib.sha256(url.encode("utf-8")).digest()
    source_id = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
    return source_id or 1


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    async def run(
        self,
        *args: str,
        timeout_seconds: float,
        download_directory: Path | None = None,
        max_download_bytes: int | None = None,
    ) -> CommandResult: ...


class AsyncCommandRunner:
    """Run fixed-argument commands without a shell and with bounded execution time."""

    def __init__(self, *, max_output_bytes: int = 2 * 1024 * 1024) -> None:
        self._max_output_bytes = max_output_bytes

    async def _read_bounded(self, stream: asyncio.StreamReader) -> bytes:
        output = bytearray()
        while chunk := await stream.read(64 * 1024):
            output.extend(chunk)
            if len(output) > self._max_output_bytes:
                raise SoundCloudDownloadError("command_output_too_large")
        return bytes(output)

    @staticmethod
    async def _monitor_download(
        process: asyncio.subprocess.Process,
        directory: Path | None,
        max_bytes: int | None,
    ) -> None:
        if directory is None or max_bytes is None:
            return
        while process.returncode is None:
            size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
            if size > max_bytes:
                raise SoundCloudValidationError("media_too_large")
            await asyncio.sleep(0.01)

    @staticmethod
    async def _terminate_group(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
            return
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def run(
        self,
        *args: str,
        timeout_seconds: float,
        download_directory: Path | None = None,
        max_download_bytes: int | None = None,
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(self._read_bounded(process.stdout))
        stderr_task = asyncio.create_task(self._read_bounded(process.stderr))
        monitor_task = asyncio.create_task(
            self._monitor_download(process, download_directory, max_download_bytes)
        )
        try:
            stdout, stderr, _returncode, _monitored = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, process.wait(), monitor_task),
                timeout_seconds,
            )
        except BaseException:
            await self._terminate_group(process)
            await asyncio.gather(stdout_task, stderr_task, monitor_task, return_exceptions=True)
            raise
        return CommandResult(
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    webpage_url: str
    title: str | None
    performer: str | None
    duration: float
    view_count: int
    like_count: int
    repost_count: int
    comment_count: int


@dataclass(frozen=True, slots=True)
class _PreparedMedia:
    candidate: _Candidate
    path: Path
    size: int


class SoundCloudMusicSource:
    """Resolve, download, transcode, and validate SoundCloud audio without Telegram."""

    def __init__(
        self,
        runner: CommandRunner,
        *,
        download_root: Path,
        timeout_seconds: float,
        max_media_bytes: int,
        min_duration_seconds: float = 60,
        duration_tolerance_seconds: float = 8,
        search_limit: int = 10,
        chooser: Callable[[Sequence[_Candidate]], _Candidate] | None = None,
        chunk_size: int = 256 * 1024,
    ) -> None:
        self._runner = runner
        self._download_root = download_root
        self._timeout = timeout_seconds
        self._max_bytes = max_media_bytes
        self._min_duration = min_duration_seconds
        self._duration_tolerance = duration_tolerance_seconds
        self._search_limit = search_limit
        self._chooser = chooser
        self._chunk_size = chunk_size
        self._prepared: dict[int, _PreparedMedia] = {}
        self._prepared_lock = asyncio.Lock()
        self._reserved_source_ids: set[int] = set()

    def _target(self, query: str) -> str:
        value = query.strip()
        if is_url_like(value):
            canonical = canonical_track_url(value)
            if canonical is None:
                raise SoundCloudValidationError("unsupported_url")
            return canonical
        if not value:
            raise SoundCloudValidationError("empty_query")
        return f"scsearch{self._search_limit}:{value}"

    async def _run(
        self,
        *args: str,
        allow_nonzero: bool = False,
        download_directory: Path | None = None,
        max_download_bytes: int | None = None,
    ) -> CommandResult:
        try:
            result = await self._runner.run(
                *args,
                timeout_seconds=self._timeout,
                download_directory=download_directory,
                max_download_bytes=max_download_bytes,
            )
        except asyncio.CancelledError:
            raise
        except SoundCloudValidationError:
            raise
        except Exception:
            raise SoundCloudDownloadError("soundcloud_command_failed") from None
        if result.returncode != 0 and not allow_nonzero:
            raise SoundCloudDownloadError("soundcloud_command_failed")
        return result

    def _candidate(self, value: Any) -> _Candidate | None:
        if not isinstance(value, dict):
            return None
        webpage_url = value.get("webpage_url") or value.get("original_url") or value.get("url")
        duration = value.get("duration")
        if not isinstance(webpage_url, str):
            return None
        canonical_url = canonical_track_url(webpage_url)
        if canonical_url is None:
            return None
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < self._min_duration
        ):
            return None

        title = value.get("title")
        performer = value.get("uploader") or value.get("artist")

        def count(name: str) -> int:
            raw = value.get(name)
            if not isinstance(raw, (int, float)) or not math.isfinite(raw):
                return 0
            return max(0, int(raw))

        return _Candidate(
            webpage_url=canonical_url,
            title=title if isinstance(title, str) else None,
            performer=performer if isinstance(performer, str) else None,
            duration=float(duration),
            view_count=count("view_count"),
            like_count=count("like_count"),
            repost_count=count("repost_count"),
            comment_count=count("comment_count"),
        )

    @staticmethod
    def _popularity_score(candidate: _Candidate) -> int:
        """Weight high-intent engagement more strongly than passive plays."""
        return (
            candidate.view_count
            + candidate.like_count * 40
            + candidate.repost_count * 200
            + candidate.comment_count * 20
        )

    async def _inspect(
        self, query: str, excluded_source_ids: frozenset[int] = frozenset()
    ) -> _Candidate:
        target = self._target(query)
        result = await self._run(
            "yt-dlp",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            "--no-playlist",
            target,
            allow_nonzero=target.startswith("scsearch"),
        )
        try:
            payload: Any = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            raise SoundCloudValidationError("invalid_soundcloud_metadata") from None
        raw_entries = payload.get("entries") if isinstance(payload, dict) else None
        entries = raw_entries if isinstance(raw_entries, list) else [payload]
        candidates = [
            candidate
            for entry in entries
            if (candidate := self._candidate(entry))
            and source_message_id_for_url(candidate.webpage_url) not in excluded_source_ids
            # A 64 kbit/s floor rejects impossible search results before download.
            and (
                not target.startswith("scsearch")
                or candidate.duration * 64_000 / 8 <= self._max_bytes
            )
        ]
        if not candidates:
            raise SoundCloudValidationError("no_full_length_soundcloud_result")
        if self._chooser is not None:
            return self._chooser(candidates)
        return max(candidates, key=self._popularity_score)

    async def _download(self, candidate: _Candidate, directory: Path) -> Path:
        result = await self._run(
            "yt-dlp",
            "--quiet",
            "--no-warnings",
            "--no-playlist",
            "--max-filesize",
            str(self._max_bytes),
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "192K",
            "--print",
            "after_move:filepath",
            "--restrict-filenames",
            "-o",
            str(directory / "%(id)s.%(ext)s"),
            candidate.webpage_url,
            download_directory=directory,
            max_download_bytes=self._max_bytes,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise SoundCloudDownloadError("soundcloud_output_missing")
        path = Path(lines[-1]).resolve()
        root = directory.resolve()
        if path.parent != root or not path.is_file():
            raise SoundCloudValidationError("unsafe_or_missing_output")
        return path

    async def _probe(self, path: Path) -> tuple[float, str]:
        result = await self._run(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name",
            "-of",
            "json",
            str(path),
        )
        try:
            payload = json.loads(result.stdout)
            format_data = payload["format"]
            duration = float(format_data["duration"])
            format_name = str(format_data["format_name"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise SoundCloudValidationError("invalid_probe_output") from None
        if not math.isfinite(duration):
            raise SoundCloudValidationError("invalid_probe_output")
        return duration, format_name

    async def request_track(self, request: SourceRequest) -> SourceMedia:
        candidate = await self._inspect(request.query, request.excluded_source_message_ids)
        source_id = source_message_id_for_url(candidate.webpage_url)
        async with self._prepared_lock:
            if source_id in self._reserved_source_ids:
                raise SoundCloudValidationError("media_already_prepared")
            self._reserved_source_ids.add(source_id)
        directory: Path | None = None
        try:
            self._download_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = Path(tempfile.mkdtemp(prefix="soundcloud-", dir=self._download_root))
            path = await self._download(candidate, directory)
            size = path.stat().st_size
            if size > self._max_bytes:
                raise SoundCloudValidationError("media_too_large")
            if size < 256 * 1024:
                raise SoundCloudValidationError("media_too_small")
            with path.open("rb") as audio:
                signature = audio.read(3)
            if signature != b"ID3" and not signature.startswith(b"\xff"):
                raise SoundCloudValidationError("invalid_mp3_signature")
            duration, format_name = await self._probe(path)
            if "mp3" not in format_name.casefold() or path.suffix.casefold() != ".mp3":
                raise SoundCloudValidationError("not_mp3")
            tolerance = max(self._duration_tolerance, candidate.duration * 0.03)
            if duration < self._min_duration or abs(duration - candidate.duration) > tolerance:
                raise SoundCloudValidationError("duration_mismatch")
            prepared = _PreparedMedia(candidate, path, size)
            async with self._prepared_lock:
                self._prepared[source_id] = prepared
            return SourceMedia(
                source_chat_id=-1,
                source_message_id=source_id,
                kind=MediaKind.AUDIO,
                file_name=path.name,
                mime_type="audio/mpeg",
                size=size,
                title=candidate.title,
                performer=candidate.performer,
                duration_seconds=max(1, round(duration)),
            )
        except BaseException:
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
            async with self._prepared_lock:
                self._prepared.pop(source_id, None)
                self._reserved_source_ids.discard(source_id)
            raise

    async def _stream(self, source_id: int) -> AsyncIterator[bytes]:
        async with self._prepared_lock:
            prepared = self._prepared.pop(source_id, None)
        if prepared is None:
            raise SoundCloudValidationError("unknown_media_handle")
        directory = prepared.path.parent
        try:
            with prepared.path.open("rb") as audio:
                while chunk := audio.read(self._chunk_size):
                    yield chunk
                    await asyncio.sleep(0)
        finally:
            shutil.rmtree(directory, ignore_errors=True)
            async with self._prepared_lock:
                self._reserved_source_ids.discard(source_id)

    def iter_download(self, media: SourceMedia) -> AsyncIterator[bytes]:
        return self._stream(media.source_message_id)

    async def discard(self, media: SourceMedia) -> None:
        """Idempotently release prepared media that was not consumed."""
        async with self._prepared_lock:
            prepared = self._prepared.pop(media.source_message_id, None)
            self._reserved_source_ids.discard(media.source_message_id)
        if prepared is not None:
            shutil.rmtree(prepared.path.parent, ignore_errors=True)
