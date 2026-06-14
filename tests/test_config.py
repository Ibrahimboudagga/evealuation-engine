from app.config import get_setting, get_settings


def test_settings_load_from_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=sqlite:///custom.db",
                "OPENAI_API_KEY=test-openai-key",
                "DEFAULT_CONCURRENCY=7",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.database_url == "sqlite:///custom.db"
    assert settings.openai_api_key == "test-openai-key"
    assert settings.default_concurrency == 7
    assert get_setting("DATABASE_URL") == "sqlite:///custom.db"
    assert get_setting("openai_api_key") == "test-openai-key"
    assert get_setting("missing", "fallback") == "fallback"

    get_settings.cache_clear()
