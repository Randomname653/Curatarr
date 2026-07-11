"""
ARR Suite LLM - Embedding Generator (Phase A)

Uses Ollama nomic-embed-text to generate embeddings for text.
"""

import logging
from typing import List, Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class EmbeddingGenerator:
    """
    Generate embeddings using Ollama nomic-embed-text model.

    Usage:
        generator = EmbeddingGenerator()
        embedding = await generator.generate_embedding("text")
    """

    def __init__(self):
        """Initialize embedding generator."""
        self.endpoint = f"{settings.effective_ollama}/api/embeddings"
        self.model = settings.EMBEDDING_MODEL
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"Content-Type": "application/json"}
        )

    async def generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Input text

        Returns:
            Embedding vector (list of floats)
        """
        payload = {
            "model": self.model,
            "prompt": text,
            # CPU-only: a GPU-resident nomic pushes the packed 27B curator into
            # partial offload (0.5 t/s measured) — see episodic_memory._embed.
            # All embed callsites use num_gpu=0 so ollama never flip-reloads
            # the embedder between GPU and CPU configurations.
            "options": {"num_gpu": 0},
        }

        try:
            response = await self.client.post(self.endpoint, json=payload)
            response.raise_for_status()

            data = response.json()
            return data.get("embedding", [])

        except httpx.HTTPStatusError as e:
            # A 404 here almost always means the embedding model isn't pulled.
            # raise_for_status() raises HTTPStatusError, which is NOT a subclass
            # of httpx.RequestError — so this case used to escape the handler
            # below and propagate as an opaque error.
            logger.warning(
                "Embedding generation failed: HTTP %s from %s — is the model "
                "'%s' pulled?  (ollama pull %s)",
                e.response.status_code, self.endpoint, self.model, self.model,
            )
            return []
        except httpx.RequestError as e:
            logger.warning("Embedding generation failed: %s", e)
            return []

    # Pass 48: removed ``generate_embeddings`` (batch wrapper) and
    # ``generate_media_embedding`` (multi-field composer). Both had zero
    # callers — chat.py and media_enricher.py only ever called
    # ``generate_embedding`` on pre-composed text. Compose-and-batch
    # logic that's needed lives in the caller, where the text-build
    # rules are domain-specific anyway.

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
