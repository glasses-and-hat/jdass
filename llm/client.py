"""
Ollama LLM client with retry logic and a clean interface.
All LLM calls in the codebase go through this module — swap models or
providers here without touching business logic.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class OllamaClient:
    """
    Thin wrapper around the Ollama REST API.

    Usage:
        client = OllamaClient()
        text = client.generate("Summarise this job description: ...")
        vec  = client.embed("Python Kafka AWS distributed systems")
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        primary_model: str = "llama3.1:8b",
        fast_model: str = "mistral:7b",
        embed_model: str = "nomic-embed-text",
        timeout: float = 180.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.primary_model = primary_model
        self.fast_model = fast_model
        self.embed_model = embed_model
        self._client = httpx.Client(timeout=timeout)

    # ── Text generation ───────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.3,
        fast: bool = False,
    ) -> str:
        """
        Generate a completion. Blocks until the full response is ready.

        Args:
            prompt:      User prompt.
            model:       Override model name. Defaults to primary_model.
            system:      Optional system prompt.
            temperature: 0.0 = deterministic, 1.0 = creative.
            fast:        Use fast_model instead of primary_model.
        """
        resolved_model = model or (self.fast_model if fast else self.primary_model)
        payload: dict = {
            "model": resolved_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        logger.debug("LLM generate | model={} prompt_len={}", resolved_model, len(prompt))
        t0 = time.perf_counter()

        resp = self._client.post(f"{self.base_url}/api/generate", json=payload)
        resp.raise_for_status()

        elapsed = time.perf_counter() - t0
        result = resp.json()["response"].strip()
        logger.debug("LLM response | model={} elapsed={:.1f}s len={}", resolved_model, elapsed, len(result))
        return result

    # ── Embeddings ────────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def embed(self, text: str, model: Optional[str] = None) -> list[float]:
        """Return an embedding vector for `text`."""
        resolved_model = model or self.embed_model
        resp = self._client.post(
            f"{self.base_url}/api/embeddings",
            json={"model": resolved_model, "prompt": text},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    # ── Health check ──────────────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        """Return True if Ollama is reachable."""
        try:
            resp = self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """Return names of locally available models."""
        try:
            resp = self._client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        except Exception as exc:
            logger.warning("Could not list Ollama models: {}", exc)
            return []

    def __del__(self):
        try:
            self._client.close()
        except Exception:
            pass


# ── Module-level singleton ────────────────────────────────────────────────────
# Import and use `llm` directly in other modules:
#   from llm.client import llm
#   text = llm.generate("...")

_llm_instance: Optional[OllamaClient] = None


def get_llm_client(
    base_url: str = "http://localhost:11434",
    primary_model: str = "llama3.1:8b",
    fast_model: str = "mistral:7b",
    embed_model: str = "nomic-embed-text",
) -> OllamaClient:
    """Return the module-level singleton, creating it if necessary."""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = OllamaClient(
            base_url=base_url,
            primary_model=primary_model,
            fast_model=fast_model,
            embed_model=embed_model,
        )
    return _llm_instance
