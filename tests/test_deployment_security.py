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
    assert "playwright==" in pyproject
    assert "telethon" not in pyproject.lower()
    assert "USER bridge" in dockerfile
    assert "telegram-profile" in compose
    assert "remote-debugging" not in dockerfile + compose
    assert "TELEGRAM_API_ID" not in (root / ".env.example").read_text()
