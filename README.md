# Telegram Topic Daily Music Bridge

A single-process MVP that uses a persistent Playwright/Chromium Telegram Web user profile to ask a configurable source bot (default `@melobot`) for music, then uploads the downloaded file with an aiogram Bot API bot to an exact forum topic. The same aiogram polling process moderates configured specialist topics by moving non-media user messages to General.

> Telegram Web automation and media redistribution may be restricted by Telegram, the source bot, or copyright law. Confirm permission before deployment. A Bot API bot cannot receive another bot's messages, which is why the source uses a separate logged-in web profile.

## Behavior

- Source requests are serialized per process.
- Search results are scoped to Telegram Web's result containers; the exact canonical username must match both the selected result and the opened chat header before a query is sent.
- The outbound query message ID is recorded. Responses must follow it in DOM order, explicit reply IDs are preferred, and observed IDs remain consumed across timeout/cancellation. If Telegram Web omits reply IDs, an unassociated media message arriving after the outbound query remains a residual association risk.
- The source session remains serialized through the complete download. Chromium downloads into a dedicated per-request directory whose growth is polled during transfer; oversized downloads are cancelled and all partial files are removed before any complete file is exposed.
- Delivery is deduplicated with SHA-256 and persisted in async SQLite before publishing.
- Daily APScheduler jobs use explicit topic/time-zone mappings.
- aiogram polling runs alongside the scheduler.
- For configured specialist topics, photo, video, audio, document, voice, video note, animation, sticker, and (when supplied by aiogram) paid media are left in place. Non-media user messages are forwarded to the configured General `message_thread_id`; the original is deleted only after Telegram returns a valid forwarded message ID.
- General-topic messages, other topics, service messages, bots, this bot, and media are ignored to prevent loops.

## Setup

Python 3.11 or newer is supported. Playwright and its Chromium build are pinned together at `1.55.0`.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/playwright install chromium
cp .env.example .env
chmod 600 .env
cp config/topics.example.yml config/topics.yml
```

Set an absolute `TELEGRAM_WEB_PROFILE_PATH`, `DESTINATION_BOT_TOKEN`, and `TOPICS_CONFIG_PATH`. The default `TELEGRAM_WEB_URL=https://web.telegram.org/k/` is restricted by validation to official HTTPS Telegram Web. No Telegram API ID/hash is used.

In `config/topics.yml`, `topics` controls scheduled deliveries. `groups` controls moderation:

```yaml
groups:
  - chat_id: -1001234567890
    general_thread_id: 1
    specialist_thread_ids: [101, 102]
```

Group and route IDs must be exact Bot API IDs. Each configured chat must be a forum supergroup.

## Secure Telegram Web login

The bootstrap command accepts no phone number, login code, 2FA value, or password. Authentication happens only in Telegram Web. It applies umask `077`, enforces profile-directory mode `0700`, and writes QR/login screenshots with mode `0600` in a `0700` directory.

Headed login when `DISPLAY` is available:

```bash
.venv/bin/python scripts/bootstrap_telegram_web.py \
  --profile-path /absolute/private/telegram-profile --headed
```

Headless QR/login screenshot (scan it from a trusted device, then check status):

```bash
.venv/bin/python scripts/bootstrap_telegram_web.py \
  --profile-path /absolute/private/telegram-profile \
  --headless-qr-screenshot /absolute/private/login.png
.venv/bin/python scripts/bootstrap_telegram_web.py \
  --profile-path /absolute/private/telegram-profile --check-login
```

`--check-login` reports only `valid` or `required`; it does not print chat names or messages. Delete a QR screenshot after use. Treat the entire browser profile as an account credential: do not commit, share, or broadly back it up.

## Destination bot permissions and privacy

Add the Bot API bot as an administrator in each configured forum supergroup. It needs permission to read topic messages, post/forward messages, and delete messages. Disable BotFather privacy mode so polling receives ordinary group messages. The bot ignores bot-authored messages and its own messages, but those checks do not replace least-privilege administration. If forwarding fails or returns no positive message ID, the original is never deleted.

## Run

```bash
# Validates environment and YAML only; makes no Telegram connection
.venv/bin/music-bridge --check-config

# Runs one configured delivery; moderation polling is not started
.venv/bin/music-bridge --run-once rock

# Scheduler and moderation polling in one process
.venv/bin/music-bridge
```

Shutdown stops scheduled intake, cancels/drains active delivery jobs, stops aiogram polling and drains its handlers, then closes the Bot session and browser context.

## Telegram Web selector limitation

Telegram Web exposes no supported automation DOM contract. Role/ARIA and `data-*` selector fallbacks are centralized in `TelegramWebSelectors`; they deliberately avoid positional/nth-child-only selection. Selector misses, authentication requirements, timeouts, and download failures are surfaced as safe classified errors. The included selectors are **staging-verification-required** and have not been live-verified in this repository. Before production, use a private staging account and verify chat search, baseline capture, new media detection, and download after every Telegram Web update.

## Docker Compose

The image is based on the matching Playwright `v1.55.0` image, runs as non-root UID 10001, drops all Linux capabilities, does not expose a remote-debugging port, and launches Chromium with container-safe `--no-sandbox` and `--disable-dev-shm-usage` flags. The browser profile has its own persistent volume.

```bash
docker compose build

# Headless login screenshot in the private state volume
docker compose run --rm bridge python scripts/bootstrap_telegram_web.py \
  --headless-qr-screenshot /data/state/login/telegram-login.png
# Copy/view that file only through a trusted local workflow, scan, then:
docker compose run --rm bridge python scripts/bootstrap_telegram_web.py --check-login
docker compose run --rm bridge music-bridge --check-config
docker compose up -d
```

For a headed container bootstrap, explicitly provide a trusted display socket; the default Compose service does not expose one. SQLite is for one replica only. Back up `/data/state/music_bridge.db` and the `telegram-profile` volume while stopped, and protect the profile backup as a secret.

## Verification

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m compileall -q src scripts
sh -n scripts/docker-entrypoint.sh
docker compose config
```

Unit tests use injected fake browser drivers and fake Bot API clients; they require no real Telegram credentials. A real staging run remains mandatory: verify the selectors, one source response after the request baseline, disk download, exact destination topic, moderation forwarding to exact General, delete-after-forward behavior, and clean temporary directories.
