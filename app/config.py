from functools import lru_cache
from typing import Any, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Centralized application settings.

    Values are loaded from environment variables first, then from the local
    .env file. Keep every project-wide configuration value here so callers do
    not read os.environ directly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = "sqlite:///evals.db"

    # LLM provider API keys
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    cohere_api_key: Optional[str] = None

    # Evaluation engine defaults
    default_candidate_provider: str = "openai"
    default_candidate_model: str = "mock"
    default_evaluator_provider: str = "openai"
    default_evaluator_model: str = "mock"
    default_concurrency: int = 3

    # Semantic similarity
    similarity_model_name: str = "all-MiniLM-L6-v2"


@lru_cache()
def get_settings() -> Settings:
    """
    Return the cached application settings object.

    Use this for typed access, for example:
        get_settings().openai_api_key
    """
    return Settings()


def get_setting(name: str, default: Any = None) -> Any:
    """
    Return a single setting value by name.

    The lookup accepts either field names like "openai_api_key" or environment
    variable names like "OPENAI_API_KEY".
    """
    normalized_name = name.lower()
    return getattr(get_settings(), normalized_name, default)
