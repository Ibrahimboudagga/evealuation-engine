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
    app_environment: str = "development"
    bootstrap_secret: Optional[str] = None
    backup_guidance_url: str = "https://www.postgresql.org/docs/current/backup.html"

    # LLM provider API keys
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    cohere_api_key: Optional[str] = None

    # Workspace credential encryption. Generate a Fernet key for production;
    # provider connections cannot be created without it.
    workspace_encryption_key: Optional[str] = None

    # Evaluation engine defaults
    default_candidate_provider: str = "mock"
    default_candidate_model: str = "mock"
    default_evaluator_provider: str = "mock"
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


def deployment_health() -> dict[str, Any]:
    """Return safe deployment readiness information without exposing secrets."""
    settings = get_settings()
    issues: list[str] = []
    if not settings.workspace_encryption_key:
        issues.append("WORKSPACE_ENCRYPTION_KEY is not configured; encrypted provider connections cannot be saved.")
    else:
        from cryptography.fernet import Fernet
        try:
            Fernet(settings.workspace_encryption_key.encode())
        except (ValueError, TypeError):
            issues.append("WORKSPACE_ENCRYPTION_KEY is not a valid Fernet key.")
    if settings.app_environment.lower() == "production" and not settings.bootstrap_secret:
        issues.append("BOOTSTRAP_SECRET is required for production setup.")
    if settings.app_environment.lower() == "production" and settings.database_url.startswith("sqlite"):
        issues.append("Production deployments require a managed PostgreSQL DATABASE_URL; SQLite is only for local development.")
    return {
        "status": "ok" if not issues else "degraded",
        "environment": settings.app_environment,
        "database_backend": settings.database_url.split(":", 1)[0],
        "issues": issues,
        "backup_guidance_url": settings.backup_guidance_url,
    }


def require_production_configuration() -> None:
    health = deployment_health()
    if health["environment"].lower() == "production" and health["issues"]:
        raise RuntimeError("Invalid production configuration: " + " ".join(health["issues"]))
