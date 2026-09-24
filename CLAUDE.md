# CLAUDE.md

## Project

HootBeatBot is a single-process Python service that:

1. Refreshes an unauthenticated public SoundCloud catalog with ephemeral system Chromium.
2. Selects the highest-ranked unused cached URL and falls back across bounded bad candidates.
3. Rejects previews, oversized files, non-MP3 output, and actual durations that do not match SoundCloud metadata.
4. Deduplicates content by SHA-256 in async SQLite.
5. Uploads through aiogram Bot API to the exact forum `chat_id` and `message_thread_id` without a caption.
6. Optionally moderates specialist Topics with Bot API polling.

Chromium is bot-owned and may access only public SoundCloud search in an ephemeral context. It must never use a persistent profile, login, cookies, Telegram Web, personal Telegram account, API ID/hash, phone number, login code, or 2FA value.

## Stack

- Python 3.11+
- aiogram 3
- yt-dlp (pinned)
- Playwright with system Chromium (public discovery only)
- ffmpeg / ffprobe
- APScheduler 3
- SQLAlchemy 2 + aiosqlite
- Pydantic Settings 2
- pytest, Ruff, mypy

## Important paths

- `src/music_bridge/main.py` — lifecycle and dependency wiring
- `src/music_bridge/source/soundcloud_source.py` — direct SoundCloud source
- `src/music_bridge/source/soundcloud_catalog.py` — browser collector, atomic cache, ranking and fallback
- `src/music_bridge/destination/aiogram_publisher.py` — exact Topic upload, no caption
- `src/music_bridge/service.py` — download/dedupe/publish orchestration
- `src/music_bridge/settings.py` — strict environment/YAML validation
- `config/topics.yml` — real routes and SoundCloud requests

## Security and behavior

- Never commit `.env`, Bot API tokens, databases, or downloaded media.
- Never invoke a shell for yt-dlp/ffprobe; pass fixed argv to subprocess execution.
- Restrict direct URLs to HTTPS SoundCloud hosts.
- Never launch Chromium during a send; refresh only at startup and on the configured interval.
- Keep browser contexts ephemeral and unauthenticated; persist only validated catalog JSON under `/data/state`.
- Keep downloads bounded by timeout, duration, format, size, and safe temporary directories.
- Do not include captions/descriptions in Telegram uploads.
- Preserve atomic deduplication and ambiguous-delivery handling.
- Moderation must remain independently disabled with `moderation_enabled: false`.
- Use strict TDD for feature and bug changes.

## Quality gates

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m compileall -q src
sh -n scripts/docker-entrypoint.sh
docker compose config
docker compose build
```

Also perform a real SoundCloud download/probe and an exact-Topic Telegram read-back before claiming production readiness.
