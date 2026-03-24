"""Optional cross-encoder reranking service.

Wraps a ``sentence-transformers`` ``CrossEncoder`` model for query-result
pair scoring.  Runs inference in a thread to avoid blocking the event loop.
Lazy-loads the model on first use; disabled gracefully if the model is not
cached and the system is offline.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from pathlib import Path

from pipecat_context_hub.shared.types import IndexResult

logger = logging.getLogger(__name__)

# Allowed cross-encoder models to prevent arbitrary model loading.
# Add new models here as they are vetted for safety and quality.
_ALLOWED_MODELS = frozenset({
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "cross-encoder/ms-marco-MiniLM-L-12-v2",
    "cross-encoder/ms-marco-TinyBERT-L-2-v2",
})


class CrossEncoderReranker:
    """Optional async cross-encoder reranking stage.

    Instantiate with a ``RerankerConfig`` and inject into ``HybridRetriever``.
    If the model is not available (disabled, uncached, import error), all
    methods gracefully degrade — ``rerank()`` returns candidates unchanged.
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        top_n: int = 20,
        enabled: bool = False,
    ) -> None:
        if model_name not in _ALLOWED_MODELS:
            logger.warning(
                "Cross-encoder model '%s' not in allowlist — disabling. "
                "Allowed models: %s",
                model_name,
                ", ".join(sorted(_ALLOWED_MODELS)),
            )
            enabled = False
        self._model_name = model_name
        self._top_n = top_n
        self._enabled = enabled
        self._model: object | None = None  # Lazy-loaded CrossEncoder instance
        self._available = enabled  # False if model fails to load
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """Whether cross-encoder reranking is active."""
        return self._enabled and self._available

    def _load_model(self) -> None:
        """Load the cross-encoder model (synchronous, called from thread).

        Guarded by a lock to prevent double-loading under concurrency.
        """
        with self._lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(self._model_name)
                logger.info("Cross-encoder loaded: %s", self._model_name)
            except Exception:
                logger.warning(
                    "Cross-encoder model '%s' not available — disabling. "
                    "Run 'pipecat-context-hub refresh' to pre-download.",
                    self._model_name,
                )
                self._available = False

    def _score(
        self, candidates: list[IndexResult], query: str
    ) -> list[IndexResult]:
        """Score candidates against query using the cross-encoder (sync)."""
        self._load_model()
        if self._model is None:
            return candidates

        # Build query-document pairs for the cross-encoder
        top = candidates[: self._top_n]
        rest = candidates[self._top_n :]

        pairs = [(query, r.chunk.content[:1000]) for r in top]
        scores = self._model.predict(pairs)  # type: ignore[union-attr]

        # Rebuild results with sigmoid-normalized cross-encoder scores.
        # ms-marco models output unbounded logits (~-11 to +11); sigmoid
        # maps them to (0, 1) while preserving ordering.
        scored = sorted(
            zip(scores, top),
            key=lambda x: float(x[0]),
            reverse=True,
        )
        reranked = [
            IndexResult(
                chunk=result.chunk,
                score=1.0 / (1.0 + math.exp(-float(ce_score))),
                match_type=result.match_type,
            )
            for ce_score, result in scored
        ]
        # Append un-scored tail (beyond top_n) at original order
        reranked.extend(rest)
        return reranked

    async def rerank(
        self, candidates: list[IndexResult], query: str
    ) -> list[IndexResult]:
        """Async cross-encoder reranking. Returns candidates unchanged if disabled."""
        if not self.enabled or not candidates:
            return candidates
        return await asyncio.to_thread(self._score, candidates, query)

    def ensure_model(self) -> None:
        """Pre-download the model (called during ``refresh`` CLI command).

        Safe to call even if the model is already cached — ``CrossEncoder``
        will use the cached version. Runs synchronously.
        """
        if not self._enabled:
            return
        self._load_model()

    @staticmethod
    def is_model_cached(model_name: str) -> bool:
        """Check if a model is likely cached in the HuggingFace hub cache."""
        cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        if not cache_dir.exists():
            return False
        # HuggingFace cache uses models--org--name format
        safe_name = model_name.replace("/", "--")
        return any(d.name.endswith(safe_name) for d in cache_dir.iterdir() if d.is_dir())
