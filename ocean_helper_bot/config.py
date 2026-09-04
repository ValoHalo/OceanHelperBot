import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    owner_id: int
    proxy_url: str | None = None
    github_token: str | None = None
    github_watch_interval_seconds: int = 300
    github_state_path: Path = Path("data/github_watcher.sqlite3")

    @classmethod
    def from_environment(cls) -> "Settings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        owner_id_text = os.environ.get("OWNER_ID", "").strip()
        try:
            owner_id = int(owner_id_text)
        except ValueError as exc:
            raise RuntimeError("OWNER_ID must be a Telegram user ID") from exc
        if owner_id <= 0:
            raise RuntimeError("OWNER_ID must be a positive Telegram user ID")
        proxy_url = os.environ.get("PROXY_URL", "").strip() or None
        github_token = os.environ.get("GITHUB_TOKEN", "").strip() or None
        interval_text = os.environ.get("GITHUB_WATCH_INTERVAL_SECONDS", "300").strip()
        try:
            interval = int(interval_text)
        except ValueError as exc:
            raise RuntimeError("GITHUB_WATCH_INTERVAL_SECONDS must be an integer") from exc
        if interval < 30:
            raise RuntimeError("GITHUB_WATCH_INTERVAL_SECONDS must be at least 30")
        state_path = Path(
            os.environ.get("GITHUB_STATE_PATH", "data/github_watcher.sqlite3").strip()
            or "data/github_watcher.sqlite3"
        )
        return cls(
            telegram_bot_token=token,
            owner_id=owner_id,
            proxy_url=proxy_url,
            github_token=github_token,
            github_watch_interval_seconds=interval,
            github_state_path=state_path,
        )
