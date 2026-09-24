# HootBeatBot — Direct SoundCloud Music Bridge

HootBeatBot builds a public SoundCloud search catalog with ephemeral Chromium, downloads selected tracks with `yt-dlp`, validates the resulting MP3 with `ffprobe`, deduplicates it with SHA-256, and uploads it with Telegram Bot API to an exact forum Topic.

Chromium is used only for unauthenticated public SoundCloud search. It has no persistent browser profile, login, cookies, or Telegram access. No personal Telegram account or Telegram API ID/hash is used.

## Pipeline

```text
public SoundCloud search in ephemeral Chromium
  -> atomic /data/state catalog cache
  -> highest-ranked unused URL (popularity + recency)
  -> yt-dlp metadata inspection
  -> reject previews shorter than the configured minimum
  -> download and convert to MP3
  -> ffprobe format/duration validation
  -> byte-size bound + MP3 signature check
  -> SHA-256 deduplication
  -> Bot API upload to exact chat_id/message_thread_id
```

The outgoing Telegram audio has title and performer metadata but no caption/description.

## Runtime behavior

- Direct `https://soundcloud.com/...` URLs remain supported for `--run-once` and bypass the catalog.
- Scheduled queries select the highest-ranked unused URL from the cache; failed candidates are rejected and the next bounded candidate is tried.
- The browser refreshes once at startup and every six hours by default. Delivery never launches Chromium. A failed refresh uses an existing valid cache and fails closed if no valid cache exists.
- Audio shorter than `SOUNDCLOUD_MIN_DURATION_SECONDS` is rejected.
- Actual MP3 duration must match SoundCloud metadata within 8 seconds or 3%, whichever is larger.
- The downloaded file must be an MP3, have a valid MP3 signature, and stay below `MAX_MEDIA_BYTES`.
- Temporary source and upload files are removed after success or failure.
- SHA-256 content deduplication is persisted in SQLite before publishing.
- Scheduled jobs route only to explicitly configured forum Topics.
- Optional moderation uses the same Bot API bot and does not require a personal account.

Only download and redistribute media you are permitted to use. SoundCloud extractor behavior can change, so keep `yt-dlp` current and verify after upgrades.

## Setup

Python 3.11+, system Chromium, and `ffmpeg`/`ffprobe` are required. Docker installs these dependencies.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
cp config/topics.example.yml config/topics.yml
```

Required environment values:

```env
DESTINATION_BOT_TOKEN=123456789:replace-me
DATABASE_URL=sqlite+aiosqlite:////data/state/music_bridge.db
TOPICS_CONFIG_PATH=/app/config/topics.yml
SOURCE_RESPONSE_TIMEOUT_SECONDS=180
MAX_MEDIA_BYTES=52428800
SOUNDCLOUD_MIN_DURATION_SECONDS=60
SOUNDCLOUD_SEARCH_LIMIT=50
SOUNDCLOUD_CATALOG_PATH=/data/state/soundcloud-catalog.json
SOUNDCLOUD_CATALOG_REFRESH_INTERVAL_SECONDS=21600
SOUNDCLOUD_CATALOG_REFRESH_TIMEOUT_SECONDS=120
SOUNDCLOUD_CANDIDATE_ATTEMPTS=3
CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
```

No `TELEGRAM_WEB_*`, user session, phone number, login code, or 2FA value is needed.

## Topic configuration

A Topic can use a direct SoundCloud URL for an exact one-off selection or an explicit public catalog query for recurring discovery:

```yaml
moderation_enabled: false

rotation_schedule:
  enabled: true
  timezone: Asia/Tehran
  minutes: [5, 35]

groups:
  - chat_id: -1001234567890
    general_thread_id: 1
    specialist_thread_ids: [4, 7]

topics:
  - name: rock
    chat_id: -1001234567890
    message_thread_id: 4
    genre: "https://soundcloud.com/artist/full-rock-track"
    request_template: "{genre}"
    hour: 12
    minute: 0
    timezone: Asia/Tehran
    enabled: true

  - name: house
    chat_id: -1001234567890
    message_thread_id: 2
    genre: "independent progressive house free download"
    catalog_query: "popular progressive house"
    hour: 12
    minute: 5
    timezone: Asia/Tehran
    enabled: true
```

Group and route IDs must be exact Bot API IDs. The destination must be a forum supergroup.

## Commands

```bash
# Validate environment and YAML without connecting to Telegram
.venv/bin/music-bridge --check-config

# Run exactly one configured Topic
.venv/bin/music-bridge --run-once rock

# Run scheduler and optional moderation
.venv/bin/music-bridge
```

## Docker

The image contains Python, Playwright's driver, system Chromium plus Debian's
`chromium-sandbox` helper, `yt-dlp`, and `ffmpeg`. It runs as non-root UID
`10001`, is read-only except for bounded state/tmp mounts, drops all Linux
capabilities, then restores only `SYS_CHROOT` for Chromium's sandbox. It has no
persistent browser/profile volume. Compose intentionally does not set
`no-new-privileges`: Chromium's root-owned setuid sandbox helper needs that bit
clear to enter its sandbox before dropping privileges. Chromium is launched with
Playwright `chromium_sandbox=True` and never with `--no-sandbox`.

Chromium's user-namespace sandbox also needs the checked-in Playwright seccomp
profile and a host AppArmor profile based on Docker's default policy with only
`userns` added. Install the AppArmor profile once on each Docker host (and again
when that checked-in policy changes):

```bash
sudo ./scripts/install-apparmor-profile.sh
```

Do not replace the named profile with `apparmor=unconfined`. Compose fails closed
when `hootbeatbot-chromium` is not loaded.

The private `config/topics.yml` is excluded from the Docker build context. The
image contains only `config/topics.example.yml`; deployment must bind-mount the
real routing file at `/app/config/topics.yml`.

```bash
docker compose build
docker compose run --rm bridge music-bridge --check-config
docker compose up -d
```

SQLite supports one service replica. Back up `/data/state/music_bridge.db` and `/data/state/soundcloud-catalog.json` while the service is stopped.

## Verification

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

A release is not complete until a real SoundCloud URL has been downloaded, probed, uploaded by HootBeatBot to the exact test Topic, and read back from Telegram.
