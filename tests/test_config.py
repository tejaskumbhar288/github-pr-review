from __future__ import annotations

import pytest

from app.config import ConfigError, Settings, get_settings


def test_defaults_apply_when_env_is_empty():
    s = Settings.from_env({})
    assert s.llm_provider == "gemini"
    assert s.max_files == 40
    assert s.review_event == "COMMENT"
    assert s.post_reviews is False


def test_invalid_ints_fall_back_instead_of_crashing():
    s = Settings.from_env({"MAX_FILES": "not-a-number", "MAX_RETRIES": ""})
    assert s.max_files == 40
    assert s.max_retries == 5


def test_ints_are_clamped_to_a_sane_minimum():
    assert Settings.from_env({"MAX_FILES": "-3"}).max_files == 1
    assert Settings.from_env({"CONTEXT_CONCURRENCY": "0"}).context_concurrency == 1


@pytest.mark.parametrize(
    "raw,expected",
    [("1", True), ("true", True), ("YES", True), ("0", False), ("off", False), ("", False)],
)
def test_bool_parsing(raw, expected):
    assert Settings.from_env({"POST_REVIEWS": raw}).post_reviews is expected


def test_gemini_without_a_key_is_rejected_with_actionable_advice():
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        Settings.from_env({"LLM_PROVIDER": "gemini"}).validate()


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigError, match="Unknown LLM_PROVIDER"):
        Settings.from_env({"LLM_PROVIDER": "gpt5"}).validate()


def test_ollama_needs_no_key():
    Settings.from_env({"LLM_PROVIDER": "ollama"}).validate()


def test_bad_review_event_is_rejected():
    with pytest.raises(ConfigError, match="REVIEW_EVENT"):
        Settings.from_env({"LLM_PROVIDER": "ollama", "REVIEW_EVENT": "MERGE"}).validate()


def test_webhook_mode_requires_a_secret_and_app_credentials():
    base = {"LLM_PROVIDER": "ollama"}
    with pytest.raises(ConfigError, match="GITHUB_WEBHOOK_SECRET"):
        Settings.from_env(base).validate_for_webhook()
    with pytest.raises(ConfigError, match="GITHUB_APP_ID"):
        Settings.from_env({**base, "GITHUB_WEBHOOK_SECRET": "s"}).validate_for_webhook()
    with pytest.raises(ConfigError, match="GITHUB_PRIVATE_KEY"):
        Settings.from_env(
            {**base, "GITHUB_WEBHOOK_SECRET": "s", "GITHUB_APP_ID": "1"}
        ).validate_for_webhook()


def test_with_overrides_does_not_mutate_the_original():
    s = Settings.from_env({"LLM_PROVIDER": "ollama"})
    other = s.with_overrides(max_files=5)
    assert other.max_files == 5
    assert s.max_files == 40


def test_tracing_needs_both_keys():
    assert not Settings.from_env({"LANGFUSE_PUBLIC_KEY": "pk"}).tracing_enabled
    assert Settings.from_env(
        {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}
    ).tracing_enabled


def test_dotenv_is_actually_loaded(tmp_path, monkeypatch):
    """Regression: python-dotenv was a dependency that was never called."""
    import app.config as config

    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=from-dotenv\nMAX_FILES=3\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("MAX_FILES", raising=False)
    monkeypatch.setattr(config, "_dotenv_loaded", False)
    monkeypatch.setattr(config, "_candidate_env_files", lambda: [env_file])
    get_settings.cache_clear()

    try:
        s = get_settings()
        assert s.gemini_api_key == "from-dotenv"
        assert s.max_files == 3
    finally:
        get_settings.cache_clear()
        monkeypatch.setattr(config, "_dotenv_loaded", False)


def test_real_env_wins_over_dotenv(tmp_path, monkeypatch):
    import app.config as config

    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "from-shell")
    monkeypatch.setattr(config, "_dotenv_loaded", False)
    monkeypatch.setattr(config, "_candidate_env_files", lambda: [env_file])
    get_settings.cache_clear()
    try:
        assert get_settings().gemini_api_key == "from-shell"
    finally:
        get_settings.cache_clear()
        monkeypatch.setattr(config, "_dotenv_loaded", False)
