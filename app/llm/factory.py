"""Single place where a provider name becomes a provider object."""

from __future__ import annotations

from ..config import Settings
from .base import LLMProvider
from .gemini import GeminiProvider
from .ollama import OllamaProvider


def build_provider(settings: Settings) -> LLMProvider:
    settings.validate()
    if settings.llm_provider == "gemini":
        return GeminiProvider(
            api_key=settings.gemini_api_key or "",
            model=settings.gemini_model,
            max_retries=settings.max_retries,
            max_sampling_retries=settings.max_sampling_retries,
            timeout=settings.request_timeout,
        )
    return OllamaProvider(
        host=settings.ollama_host,
        model=settings.ollama_model,
        # Local retries are expensive in wall-clock time; cap them lower.
        max_retries=min(settings.max_retries, 3),
        max_sampling_retries=settings.max_sampling_retries,
        num_ctx=settings.ollama_num_ctx,
        timeout=max(settings.request_timeout, 600.0),
    )
