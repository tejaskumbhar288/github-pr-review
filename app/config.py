"""Central configuration, loaded from environment variables.

Settings are built by an explicit call rather than at import time. The CLI, the
webhook service and the worker all need to load ``.env`` *before* the first
read, and tests need to build a Settings with overrides without mutating global
state. A module-level singleton constructed at import made both impossible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

VALID_PROVIDERS = frozenset({"gemini", "ollama"})


def _str(env: dict[str, str], key: str, default: str) -> str:
    raw = env.get(key)
    return raw.strip() if raw and raw.strip() else default


def _opt(env: dict[str, str], key: str) -> str | None:
    raw = env.get(key)
    return raw.strip() or None if raw else None


def _int(env: dict[str, str], key: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(key)
    if not raw or not raw.strip():
        return default
    try:
        return max(minimum, int(raw.strip()))
    except ValueError:
        return default


def _bool(env: dict[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- LLM provider selection ---
    llm_provider: str = "gemini"

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:7b"
    ollama_num_ctx: int = 16384

    # --- GitHub ---
    github_token: str | None = None
    github_api: str = "https://api.github.com"

    # GitHub App credentials (webhook mode). A PEM either inline or on disk.
    github_app_id: str | None = None
    github_private_key: str | None = None
    github_private_key_path: str | None = None
    github_webhook_secret: str | None = None

    # --- Review budget knobs ---
    max_patch_lines: int = 800
    max_files: int = 40
    max_retries: int = 5
    # Files larger than this are sent as diff-only; whole-file context is the
    # biggest quality lever but also the biggest token sink.
    context_char_limit: int = 60_000
    # Cap on concurrent whole-file fetches, to stay polite to the REST API.
    context_concurrency: int = 8
    request_timeout: float = 180.0

    # --- Publishing (roadmap 1) ---
    # "COMMENT" never blocks a merge; "REQUEST_CHANGES" does. Default to the
    # non-blocking one: a bot that blocks merges on a hallucination gets removed.
    review_event: str = "COMMENT"
    post_reviews: bool = False

    # --- Queue / worker (roadmap 3) ---
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "reviews"
    # Idempotency window for (pr, head_sha); GitHub redelivers webhooks.
    idempotency_ttl: int = 86_400
    worker_concurrency: int = 2
    # How many times a job may fail before it stops being retried and lands in
    # the dead-letter list instead.
    max_attempts: int = 3

    # --- Observability (roadmap 4) ---
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    log_level: str = "INFO"

    # --- Static analysis pre-pass (roadmap 5) ---
    static_analysis: bool = True
    ruff_path: str = "ruff"
    semgrep_path: str = "semgrep"
    # NOT "auto". Semgrep refuses `--config auto` unless metrics are on, because
    # auto asks the registry which rules to run - and the pre-pass runs with
    # `--metrics off`, which is the whole point of the local path. The two are
    # mutually exclusive, so the default is a concrete ruleset that works with
    # telemetry off.
    semgrep_config: str = "p/default"
    static_analysis_timeout: float = 60.0
    max_known_issues: int = 60

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        return cls(
            llm_provider=_str(e, "LLM_PROVIDER", "gemini").lower(),
            gemini_api_key=_opt(e, "GEMINI_API_KEY"),
            gemini_model=_str(e, "GEMINI_MODEL", "gemini-3.6-flash"),
            ollama_host=_str(e, "OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=_str(e, "OLLAMA_MODEL", "qwen2.5-coder:7b"),
            ollama_num_ctx=_int(e, "OLLAMA_NUM_CTX", 16384, minimum=2048),
            github_token=_opt(e, "GITHUB_TOKEN"),
            github_api=_str(e, "GITHUB_API", "https://api.github.com").rstrip("/"),
            github_app_id=_opt(e, "GITHUB_APP_ID"),
            github_private_key=_opt(e, "GITHUB_PRIVATE_KEY"),
            github_private_key_path=_opt(e, "GITHUB_PRIVATE_KEY_PATH"),
            github_webhook_secret=_opt(e, "GITHUB_WEBHOOK_SECRET"),
            max_patch_lines=_int(e, "MAX_PATCH_LINES", 800, minimum=1),
            max_files=_int(e, "MAX_FILES", 40, minimum=1),
            max_retries=_int(e, "MAX_RETRIES", 5, minimum=1),
            context_char_limit=_int(e, "CONTEXT_CHAR_LIMIT", 60_000, minimum=0),
            context_concurrency=_int(e, "CONTEXT_CONCURRENCY", 8, minimum=1),
            request_timeout=float(_int(e, "REQUEST_TIMEOUT", 180, minimum=5)),
            review_event=_str(e, "REVIEW_EVENT", "COMMENT").upper(),
            post_reviews=_bool(e, "POST_REVIEWS", False),
            redis_url=_str(e, "REDIS_URL", "redis://localhost:6379/0"),
            queue_name=_str(e, "QUEUE_NAME", "reviews"),
            idempotency_ttl=_int(e, "IDEMPOTENCY_TTL", 86_400, minimum=60),
            worker_concurrency=_int(e, "WORKER_CONCURRENCY", 2, minimum=1),
            max_attempts=_int(e, "MAX_ATTEMPTS", 3, minimum=1),
            langfuse_public_key=_opt(e, "LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=_opt(e, "LANGFUSE_SECRET_KEY"),
            langfuse_host=_str(e, "LANGFUSE_HOST", "https://cloud.langfuse.com"),
            log_level=_str(e, "LOG_LEVEL", "INFO").upper(),
            static_analysis=_bool(e, "STATIC_ANALYSIS", True),
            ruff_path=_str(e, "RUFF_PATH", "ruff"),
            semgrep_path=_str(e, "SEMGREP_PATH", "semgrep"),
            semgrep_config=_str(e, "SEMGREP_CONFIG", "p/default"),
            static_analysis_timeout=float(_int(e, "STATIC_ANALYSIS_TIMEOUT", 60, minimum=5)),
            max_known_issues=_int(e, "MAX_KNOWN_ISSUES", 60, minimum=0),
        )

    def with_overrides(self, **kwargs: Any) -> Settings:
        return replace(self, **kwargs)

    # --- validation -------------------------------------------------------

    def validate(self) -> None:
        """Fail fast with a message that says what to actually do."""
        if self.llm_provider not in VALID_PROVIDERS:
            raise ConfigError(
                f"Unknown LLM_PROVIDER {self.llm_provider!r}. "
                f"Expected one of: {', '.join(sorted(VALID_PROVIDERS))}."
            )
        if self.llm_provider == "gemini" and not self.gemini_api_key:
            raise ConfigError(
                "LLM_PROVIDER=gemini but GEMINI_API_KEY is unset. Get a free key at "
                "https://aistudio.google.com/apikey and put it in .env, or run with "
                "LLM_PROVIDER=ollama for a fully local review."
            )
        if self.review_event not in {"COMMENT", "REQUEST_CHANGES", "APPROVE"}:
            raise ConfigError(
                f"REVIEW_EVENT must be COMMENT, REQUEST_CHANGES or APPROVE, "
                f"got {self.review_event!r}"
            )

    def validate_for_webhook(self) -> None:
        """Extra requirements that only the GitHub App path has."""
        self.validate()
        if not self.github_webhook_secret:
            raise ConfigError(
                "GITHUB_WEBHOOK_SECRET is required to verify webhook signatures. "
                "Set it to the same value configured on the GitHub App."
            )
        if not self.github_app_id:
            raise ConfigError("GITHUB_APP_ID is required in webhook mode.")
        if not (self.github_private_key or self.github_private_key_path):
            raise ConfigError(
                "Set GITHUB_PRIVATE_KEY (inline PEM) or GITHUB_PRIVATE_KEY_PATH "
                "(path to the .pem downloaded from the GitHub App settings)."
            )

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


class ConfigError(RuntimeError):
    """Configuration is missing or contradictory."""


_dotenv_loaded = False


def _candidate_env_files() -> list[Path]:
    """Where a .env may live, in priority order.

    Deliberately bounded. python-dotenv's default is to walk up from the calling
    module until it finds a .env, which can reach the user's home directory and
    silently pick up an unrelated file - a genuinely confusing failure when the
    key you think you set is not the key being used.
    """
    return [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]


def load_dotenv_once(path: str | Path | None = None) -> None:
    """Load ``.env`` exactly once, without clobbering real environment vars.

    The previous version listed python-dotenv as a dependency but never called
    it, so every value in ``.env`` was silently ignored.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is declared
        return

    candidates = [Path(path)] if path is not None else _candidate_env_files()
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached; call ``get_settings.cache_clear()`` in tests."""
    load_dotenv_once()
    return Settings.from_env()
