"""Application configuration loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings sourced from environment variables.

    Extra env vars in .env are ignored rather than raising — this allows
    the .env file to hold keys for optional integrations without
    breaking the core settings loader.
    """

    # Razorpay
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    auto_recover: bool = False  # auto-create recovery links from webhooks

    # LLM (optional)
    opencode_zen_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # Database
    database_url: str = "sqlite:///./mandate_doctor.db"

    # App
    log_level: str = "DEBUG"
    max_retries_per_cycle: int = 3
    host: str = "0.0.0.0"
    port: int = 8000
    project_root: Path = PROJECT_ROOT

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
