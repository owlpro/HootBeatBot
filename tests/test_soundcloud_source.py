import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from music_bridge.domain import SourceRequest
from music_bridge.source.soundcloud_source import (
    AsyncCommandRunner,
    CommandResult,
    SoundCloudDownloadError,
    SoundCloudMusicSource,
    SoundCloudValidationError,
    source_message_id_for_url,
)


class FakeRunner:
    def __init__(
        self,
        metadata: dict[str, object],
        *,
        audio: bytes = b"ID3" + b"a" * 900_000,
        probed_duration: float = 180.0,
        probed_format: str = "mp3",
        failure: Exception | None = None,
        metadata_returncode: int = 0,
    ) -> None:
        self.metadata = metadata
        self.audio = audio
        self.probed_duration = probed_duration
        self.probed_format = probed_format
        self.failure = failure
        self.metadata_returncode = metadata_returncode
        self.calls: list[tuple[tuple[str, ...], float]] = []

    async def run(
        self,
        *args: str,
        timeout_seconds: float,
        download_directory: Path | None = None,
        max_download_bytes: int | None = None,
    ) -> CommandResult:
        del download_directory, max_download_bytes
        self.calls.append((args, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        if args[0] == "ffprobe":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "format": {
                            "duration": str(self.probed_duration),
                            "format_name": self.probed_format,
                        }
                    }
                ),
                "",
            )
        if "--skip-download" in args:
            return CommandResult(
                self.metadata_returncode, json.dumps(self.metadata), "partial failure"
            )
        output_index = args.index("-o") + 1
        output_template = Path(args[output_index])
        path = output_template.parent / "track.mp3"
        path.write_bytes(self.audio)
        return CommandResult(0, str(path), "")


def direct_metadata(*, duration: float = 180.0) -> dict[str, object]:
    return {
        "id": "soundcloud-track-id",
        "title": "Full Rock Track",
        "uploader": "Independent Artist",
        "duration": duration,
        "webpage_url": "https://soundcloud.com/artist/full-rock-track",
        "extractor_key": "Soundcloud",
    }


@pytest.mark.asyncio
async def test_direct_soundcloud_url_downloads_validated_mp3_and_cleans_source_temp(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(direct_metadata())
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
        chooser=lambda candidates: candidates[0],
        chunk_size=400_000,
    )

    media = await source.request_track(
        SourceRequest("https://soundcloud.com/artist/full-rock-track")
    )
    chunks = [chunk async for chunk in source.iter_download(media)]

    assert media.title == "Full Rock Track"
    assert media.performer == "Independent Artist"
    assert media.mime_type == "audio/mpeg"
    assert media.size == 900_003
    assert media.duration_seconds == 180
    assert list(map(len, chunks)) == [400_000, 400_000, 100_003]
    assert list(tmp_path.iterdir()) == []
    download_args = runner.calls[1][0]
    assert "--max-filesize" in download_args
    assert "2000000" in download_args
    audio_format_index = download_args.index("--audio-format")
    assert ("--audio-format", "mp3") == (download_args[audio_format_index : audio_format_index + 2])


@pytest.mark.asyncio
async def test_direct_soundcloud_url_is_canonicalized_before_inspection(tmp_path: Path) -> None:
    runner = FakeRunner(direct_metadata())
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
    )

    media = await source.request_track(
        SourceRequest(
            " https://www.soundcloud.com/artist/full-rock-track/?utm_source=clipboard#fragment "
        )
    )
    await source.discard(media)

    assert runner.calls[0][0][-1] == "https://soundcloud.com/artist/full-rock-track"


@pytest.mark.asyncio
async def test_concurrent_same_source_requests_do_not_overwrite_prepared_download(
    tmp_path: Path,
) -> None:
    source = SoundCloudMusicSource(
        FakeRunner(direct_metadata()),
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
    )
    request = SourceRequest("https://soundcloud.com/artist/full-rock-track")

    results = await asyncio.gather(
        source.request_track(request), source.request_track(request), return_exceptions=True
    )

    failures = [result for result in results if isinstance(result, BaseException)]
    media = next(result for result in results if not isinstance(result, BaseException))
    assert len(failures) == 1
    assert isinstance(failures[0], SoundCloudValidationError)
    assert len(list(tmp_path.iterdir())) == 1
    _ = [chunk async for chunk in source.iter_download(media)]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_discard_removes_unconsumed_prepared_media_idempotently(tmp_path: Path) -> None:
    source = SoundCloudMusicSource(
        FakeRunner(direct_metadata()),
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
    )
    request = SourceRequest("https://soundcloud.com/artist/full-rock-track")
    media = await source.request_track(request)
    assert len(list(tmp_path.iterdir())) == 1

    await source.discard(media)
    await source.discard(media)

    assert list(tmp_path.iterdir()) == []
    replacement = await source.request_track(request)
    await source.discard(replacement)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_search_rejects_preview_and_selects_full_soundcloud_result(tmp_path: Path) -> None:
    metadata = {
        "entries": [
            direct_metadata(duration=30),
            {
                **direct_metadata(duration=215),
                "id": "full-result",
                "title": "Usable Hip Hop Track",
                "webpage_url": "https://soundcloud.com/artist/usable-hip-hop-track",
            },
        ]
    }
    runner = FakeRunner(metadata, probed_duration=215)
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
        chooser=lambda candidates: candidates[0],
    )

    media = await source.request_track(SourceRequest("independent hip hop"))
    _ = [chunk async for chunk in source.iter_download(media)]

    assert media.title == "Usable Hip Hop Track"
    inspect_args = runner.calls[0][0]
    assert inspect_args[-1] == "scsearch10:independent hip hop"
    assert runner.calls[1][0][-1] == "https://soundcloud.com/artist/usable-hip-hop-track"


@pytest.mark.asyncio
async def test_search_defaults_to_most_popular_full_length_result(
    tmp_path: Path,
) -> None:
    less_popular = {
        **direct_metadata(duration=215),
        "title": "Less Popular",
        "webpage_url": "https://soundcloud.com/artist/less-popular",
        "view_count": 10_000,
        "like_count": 100,
        "repost_count": 2,
    }
    more_popular = {
        **direct_metadata(duration=215),
        "title": "Popular Track",
        "webpage_url": "https://soundcloud.com/artist/popular-track",
        "view_count": 1_000_000,
        "like_count": 25_000,
        "repost_count": 500,
    }
    runner = FakeRunner({"entries": [less_popular, more_popular]}, probed_duration=215)
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
    )

    media = await source.request_track(SourceRequest("popular Persian rap 2026"))
    _ = [chunk async for chunk in source.iter_download(media)]

    assert media.title == "Popular Track"
    assert runner.calls[1][0][-1] == "https://soundcloud.com/artist/popular-track"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_metric", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "metric_name", ["view_count", "like_count", "repost_count", "comment_count"]
)
async def test_non_finite_popularity_metrics_do_not_abort_candidate_inspection(
    tmp_path: Path, invalid_metric: float, metric_name: str
) -> None:
    invalid = {
        **direct_metadata(duration=180),
        "title": "Invalid Metric",
        metric_name: invalid_metric,
    }
    valid = {
        **direct_metadata(duration=180),
        "title": "Valid Metric",
        "webpage_url": "https://soundcloud.com/artist/valid-metric",
        "view_count": 1,
    }
    runner = FakeRunner({"entries": [invalid, valid]})
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
    )

    media = await source.request_track(SourceRequest("rock"))
    _ = [chunk async for chunk in source.iter_download(media)]

    assert media.title == "Valid Metric"


@pytest.mark.asyncio
async def test_actual_duration_must_match_soundcloud_metadata(tmp_path: Path) -> None:
    runner = FakeRunner(direct_metadata(duration=180), probed_duration=1)
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
        chooser=lambda candidates: candidates[0],
    )

    with pytest.raises(SoundCloudValidationError, match="duration_mismatch"):
        await source.request_track(SourceRequest("https://soundcloud.com/artist/full-rock-track"))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_duration", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("location", ["metadata", "probe"])
async def test_non_finite_durations_are_rejected(
    tmp_path: Path, invalid_duration: float, location: str
) -> None:
    metadata_duration = invalid_duration if location == "metadata" else 180.0
    probed_duration = invalid_duration if location == "probe" else 180.0
    runner = FakeRunner(
        direct_metadata(duration=metadata_duration), probed_duration=probed_duration
    )
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
    )

    with pytest.raises(SoundCloudValidationError):
        await source.request_track(SourceRequest("https://soundcloud.com/artist/full-rock-track"))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_non_mp3_probe_result_is_rejected(tmp_path: Path) -> None:
    runner = FakeRunner(direct_metadata(), probed_format="ogg")
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
        chooser=lambda candidates: candidates[0],
    )

    with pytest.raises(SoundCloudValidationError, match="not_mp3"):
        await source.request_track(SourceRequest("https://soundcloud.com/artist/full-rock-track"))


@pytest.mark.asyncio
async def test_oversized_output_is_removed(tmp_path: Path) -> None:
    runner = FakeRunner(direct_metadata(), audio=b"ID3" + b"x" * 2_000_000)
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=1_000_000,
        chooser=lambda candidates: candidates[0],
    )

    with pytest.raises(SoundCloudValidationError, match="media_too_large"):
        await source.request_track(SourceRequest("https://soundcloud.com/artist/full-rock-track"))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_external_direct_url_is_rejected_before_command_execution(tmp_path: Path) -> None:
    runner = FakeRunner(direct_metadata())
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
        chooser=lambda candidates: candidates[0],
    )

    with pytest.raises(SoundCloudValidationError, match="unsupported_url"):
        await source.request_track(SourceRequest("https://example.com/not-soundcloud"))

    assert runner.calls == []


@pytest.mark.asyncio
async def test_runner_failure_is_safely_classified(tmp_path: Path) -> None:
    runner = FakeRunner(direct_metadata(), failure=TimeoutError("secret URL and process details"))
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
        chooser=lambda candidates: candidates[0],
    )

    with pytest.raises(SoundCloudDownloadError, match="soundcloud_command_failed") as caught:
        await source.request_track(SourceRequest("rock"))

    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_search_excludes_used_candidate_before_download(tmp_path: Path) -> None:
    used = direct_metadata(duration=180)
    fresh = {
        **direct_metadata(duration=180),
        "webpage_url": "https://soundcloud.com/artist/fresh",
        "title": "Fresh",
    }
    runner = FakeRunner({"entries": [used, fresh]})
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
        chooser=lambda candidates: candidates[0],
    )

    media = await source.request_track(
        SourceRequest(
            "rock",
            frozenset({source_message_id_for_url("https://soundcloud.com/artist/full-rock-track")}),
        )
    )

    assert media.title == "Fresh"
    assert runner.calls[1][0][-1] == "https://soundcloud.com/artist/fresh"


@pytest.mark.asyncio
async def test_search_uses_partial_valid_json_when_yt_dlp_reports_one_bad_entry(
    tmp_path: Path,
) -> None:
    runner = FakeRunner({"entries": [direct_metadata(duration=180)]}, metadata_returncode=1)
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
    )

    media = await source.request_track(SourceRequest("rock"))

    assert media.title == "Full Rock Track"


@pytest.mark.asyncio
async def test_implausibly_long_candidate_is_filtered_before_download(tmp_path: Path) -> None:
    runner = FakeRunner({"entries": [direct_metadata(duration=10_000)]})
    source = SoundCloudMusicSource(
        runner,
        download_root=tmp_path,
        timeout_seconds=30,
        max_media_bytes=2_000_000,
    )

    with pytest.raises(SoundCloudValidationError, match="no_full_length_soundcloud_result"):
        await source.request_track(SourceRequest("rock"))

    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_direct_url_nonzero_metadata_result_is_strict_even_with_valid_json(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(direct_metadata(), metadata_returncode=1)
    source = SoundCloudMusicSource(
        runner, download_root=tmp_path, timeout_seconds=30, max_media_bytes=2_000_000
    )

    with pytest.raises(SoundCloudDownloadError, match="soundcloud_command_failed"):
        await source.request_track(SourceRequest("https://soundcloud.com/artist/full-rock-track"))


@pytest.mark.asyncio
async def test_async_runner_bounds_stdout() -> None:
    runner = AsyncCommandRunner(max_output_bytes=1024)

    with pytest.raises(SoundCloudDownloadError, match="command_output_too_large"):
        await runner.run(
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 100000); sys.stdout.flush()",
            timeout_seconds=5,
        )


@pytest.mark.asyncio
async def test_async_runner_timeout_terminates_child_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"open({str(pid_file)!r},'w').write(str(p.pid)); "
        "time.sleep(60)"
    )
    runner = AsyncCommandRunner()

    with pytest.raises(TimeoutError):
        await runner.run(sys.executable, "-c", script, timeout_seconds=0.5)

    child_pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        os.kill(child_pid, signal.SIGKILL)
        pytest.fail("child process survived timeout")


@pytest.mark.asyncio
async def test_async_runner_stops_process_group_when_download_grows_past_limit(
    tmp_path: Path,
) -> None:
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    pid_file = tmp_path / "child.pid"
    child_script = (
        "import os,sys,time; "
        "f=open(sys.argv[1],'wb'); "
        "[(f.write(b'x'*65536),f.flush(),os.fsync(f.fileno()),time.sleep(.01)) "
        "for _ in range(1000)]"
    )
    parent_script = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_script!r},sys.argv[1]]); "
        f"open({str(pid_file)!r},'w').write(str(p.pid)); "
        "time.sleep(60)"
    )
    runner = AsyncCommandRunner()

    with pytest.raises(SoundCloudValidationError, match="media_too_large"):
        await runner.run(
            sys.executable,
            "-c",
            parent_script,
            str(download_dir / "track.part"),
            timeout_seconds=5,
            download_directory=download_dir,
            max_download_bytes=200_000,
        )

    child_pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        os.kill(child_pid, signal.SIGKILL)
        pytest.fail("download child survived size-limit termination")
