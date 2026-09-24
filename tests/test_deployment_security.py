from pathlib import Path


def test_container_startup_and_docs_enforce_private_secret_permissions() -> None:
    root = Path(__file__).parents[1]
    entrypoint = (root / "scripts" / "docker-entrypoint.sh").read_text()
    dockerfile = (root / "Dockerfile").read_text()
    readme = (root / "README.md").read_text()
    assert "umask 077" in entrypoint
    assert "chmod 700" in entrypoint
    assert "chmod 600" in entrypoint
    assert "ENTRYPOINT" in dockerfile
    assert "chmod 600 .env" in readme
    compose = (root / "docker-compose.yml").read_text()
    pyproject = (root / "pyproject.toml").read_text()
    assert "yt-dlp" in pyproject
    assert "playwright" in pyproject.lower()
    assert "telethon" not in pyproject.lower()
    assert "USER bridge" in dockerfile
    assert "telegram-profile" not in compose
    assert "chromium" in dockerfile.lower()
    assert "HOME=/tmp" in dockerfile
    assert "ffmpeg" in dockerfile.lower()
    assert "TELEGRAM_WEB" not in compose
    assert "remote-debugging" not in dockerfile + compose
    assert "TELEGRAM_API_ID" not in (root / ".env.example").read_text()


def test_private_topics_are_excluded_from_build_and_image_uses_sandbox() -> None:
    root = Path(__file__).parents[1]
    dockerignore = (root / ".dockerignore").read_text()
    dockerfile = (root / "Dockerfile").read_text()
    compose = (root / "docker-compose.yml").read_text()
    apparmor = (root / "security" / "apparmor" / "hootbeatbot-chromium").read_text()
    seccomp = (root / "security" / "seccomp" / "chromium.json").read_text()

    assert "config/topics.yml" in dockerignore.splitlines()
    assert "COPY --chown=bridge:bridge config/topics.example.yml" in dockerfile
    assert "COPY --chown=bridge:bridge config ./config" not in dockerfile
    assert "chromium-sandbox" in dockerfile
    assert "--no-sandbox" not in dockerfile + compose
    assert "no-new-privileges" not in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "cap_add:\n      - SYS_CHROOT" in compose
    assert "apparmor=hootbeatbot-chromium" in compose
    assert "seccomp=./security/seccomp/chromium.json" in compose
    assert "apparmor=unconfined" not in compose
    assert "read_only: true" in compose
    assert "profile hootbeatbot-chromium" in apparmor
    assert "userns," in apparmor
    assert "deny mount," in apparmor
    assert '"clone"' in seccomp
    assert '"setns"' in seccomp
    assert '"unshare"' in seccomp
