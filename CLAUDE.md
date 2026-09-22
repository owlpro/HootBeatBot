# HootBeatBot — Claude Code Handoff

## Goal

A single-process Telegram forum bot that:

1. Uses **Telegram Web through Playwright** with a persistent user profile to request music from `@melobot` (no Telegram `api_id`/`api_hash`).
2. Downloads the new audio response safely, hashes it, prevents duplicates, and posts it through Bot API to the configured forum topic.
3. Runs one daily schedule per topic in `Asia/Tehran`.
4. Moderates specialist topics: a non-media user message is forwarded to the configured General topic, then the original is deleted **only after forward success**.

## Current architecture

- Python `>=3.11`
- `aiogram 3`: destination uploads and group polling/moderation
- `playwright 1.55`: persistent Telegram Web session and source download
- `APScheduler 3`: daily topic schedules
- async SQLAlchemy + SQLite: delivery state, occurrence idempotency, content-hash dedupe
- Pydantic Settings + strict YAML configuration
- Docker/Compose, non-root container, persistent `/data` volume

Important modules:

- `src/music_bridge/main.py` — dependency wiring, polling, scheduler, graceful shutdown
- `src/music_bridge/source/playwright_source.py` — Telegram Web driver and music source adapter
- `src/music_bridge/moderation.py` — forward-to-General/delete-after-success behavior
- `src/music_bridge/service.py` — source/download/dedupe/publish orchestration
- `src/music_bridge/repositories.py` — atomic content claims and delivery states
- `src/music_bridge/scheduler.py` — Tehran-time occurrence keys and active-job shutdown
- `src/music_bridge/settings.py` — strict env/YAML validation
- `config/topics.example.yml` — schedule and moderation example

## Non-negotiable behavior

- Never commit `.env`, Telegram Web profile data, Bot API tokens, QR screenshots, databases, or downloaded audio.
- Never log raw Telegram/HTTP exception messages; use safe classified errors.
- Source requests are serialized. A source result must be absent from the pre-request message-ID baseline.
- Deduplication must be claimed atomically before external publishing.
- A Telegram publish with uncertain persistence must become `ambiguous`; never blindly retry it.
- Moderation ignores General, media, service messages, bots, this bot, and unconfigured topics.
- Moderation order is strictly: `forward_message` → verify returned message ID → `delete_message`.
- On forwarding failure, preserve the original message.
- Graceful shutdown stops scheduling/polling, cancels and awaits active jobs, persists cancellation state, then closes Bot/Playwright/database.

## Configuration

Environment (`.env`, based on `.env.example`):

```env
TELEGRAM_WEB_PROFILE_PATH=/data/telegram-web-profile
TELEGRAM_WEB_URL=https://web.telegram.org/k/
TELEGRAM_LOGIN_SCREENSHOT_PATH=/data/state/login/telegram-login.png
DESTINATION_BOT_TOKEN=REPLACE_LOCALLY
SOURCE_BOT_USERNAME=@melobot
DATABASE_URL=sqlite+aiosqlite:////data/state/music_bridge.db
TOPICS_CONFIG_PATH=/app/config/topics.yml
SOURCE_RESPONSE_TIMEOUT_SECONDS=120
MAX_MEDIA_BYTES=52428800
LOG_LEVEL=INFO
```

Never ask for or paste tokens/codes in chat. The previously exposed BotFather token must be revoked; use only the rotated token locally.

YAML shape:

```yaml
groups:
  - chat_id: -1001234567890
    general_thread_id: 1
    specialist_thread_ids: [101, 102]

topics:
  - name: rock
    chat_id: -1001234567890
    message_thread_id: 101
    genre: alternative rock
    request_template: "random {genre}"
    hour: 9
    minute: 0
    timezone: Asia/Tehran
    enabled: true
```

Real `chat_id` and `message_thread_id` values are still required from the staging group.

## Quality commands

Run all before committing or claiming completion:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m compileall -q src scripts
sh -n scripts/docker-entrypoint.sh
```

If Docker is available:

```bash
docker compose config
docker compose build
```

Use strict TDD for every change: failing test first, minimal implementation, full suite, then refactor.

## Remaining work / verification

The local implementation and independent pre-commit review are complete. Current automated baseline: **92 tests passing**, Ruff clean, formatting clean, strict mypy clean, compileall clean, shell syntax clean, and configuration smoke check passing.

1. Verify Telegram Web selectors against the real logged-in staging account; selectors are centralized in `TelegramWebSelectors` and are not yet live-verified.
2. Bootstrap the persistent Telegram Web profile via QR without passing phone/code/2FA as CLI arguments.
3. Create a private forum staging group, add the destination bot as admin, disable BotFather privacy mode, and grant send/delete permissions.
4. Fill real group/topic IDs and run one topic manually.
5. Verify exactly one music message lands in the intended topic.
6. Verify a non-media specialist-topic message is forwarded to General and deleted only after success.
7. Verify media and General-topic messages remain untouched.
8. Run an actual scheduled delivery and graceful-stop test.
9. Build Docker where Docker is installed.

Do not claim real Telegram or Docker verification until those actions have actually succeeded.

## Git

Remote:

```text
git@github.com:owlpro/HootBeatBot.git
```

Target branch: `main`.

Before first push, review `git status`, ensure no secrets/profile/database/audio files are staged, run all quality commands, then create one clean initial commit. Do not rewrite or force-push shared history.
