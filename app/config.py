from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_prefix="DOLA_",
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8787
    env: str = "production"  # production | development
    log_level: str = "INFO"
    # Optional protect UI/API when exposed (Bearer token). Empty = open (local only).
    api_token: str = ""

    data_dir: Path = ROOT / "data"
    download_dir: Path = ROOT / "downloads"
    upload_dir: Path = ROOT / "uploads"
    db_path: Path = ROOT / "data" / "app.db"

    # Dola / ByteDance Flow (live)
    base_url: str = "https://www.dola.com"
    # Real web client uses 495671 (verified live with session)
    app_id: str = "495671"
    app_id_alt: str = "482431"
    bot_id: str = "7339470689562525703"
    # Seedance model id from /samantha/skill/pack skill_type=17
    default_model: str = "seedance_v2.0"
    region: str = "VN"
    pc_version: str = "3.27.1"
    version_code: str = "20800"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    # Batch / worker
    max_concurrent_per_account: int = 1
    max_global_concurrent: int = 3
    poll_interval_sec: float = 10.0
    job_timeout_sec: int = 1200
    request_timeout_sec: float = 90.0
    max_job_attempts: int = 3
    account_cooldown_sec: float = 3.0
    retry_backoff_sec: float = 8.0

    # Live only — never enable in production deploy
    demo_mode: bool = False

    @field_validator("demo_mode", mode="before")
    @classmethod
    def _parse_bool(cls, v):  # type: ignore[no-untyped-def]
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on"}
        return bool(v)

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"production", "prod", "live"}


settings = Settings()
# Force live in production env unless explicitly overridden after load is already done via env
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.download_dir.mkdir(parents=True, exist_ok=True)
settings.upload_dir.mkdir(parents=True, exist_ok=True)
