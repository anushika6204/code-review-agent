"""Environment-driven settings. Secrets come from `.env`, never from code."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ConfigurationError(RuntimeError):
    """Raised lazily when a required secret is missing at the moment it is needed."""


@dataclass(frozen=True)
class Settings:
    github_token: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    github_api_url: str = "https://api.github.com"
    github_webhook_secret: str = ""
    checkpointer: str = "sqlite"  # "sqlite" | "memory"
    sqlite_path: str = "data/checkpoints.sqlite"
    mock_mode: bool = False
    max_revisions: int = 5
    max_files: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = os.environ.get
        return cls(
            github_token=env("GITHUB_TOKEN", ""),
            openai_api_key=env("OPENAI_API_KEY", ""),
            openai_model=env("OPENAI_MODEL", cls.openai_model),
            github_api_url=env("GITHUB_API_URL", cls.github_api_url),
            github_webhook_secret=env("GITHUB_WEBHOOK_SECRET", ""),
            checkpointer=env("CHECKPOINTER", cls.checkpointer).lower(),
            sqlite_path=env("SQLITE_PATH", cls.sqlite_path),
            mock_mode=_as_bool(env("MOCK_MODE")),
            max_revisions=int(env("MAX_REVISIONS", cls.max_revisions)),
            max_files=int(env("MAX_FILES", cls.max_files)),
        )
