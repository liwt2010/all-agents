"""
Embedding backends + time decay for the memory system.

Provides:
  - EmbeddingBackend: protocol for text similarity
  - TfidfBackend: scikit-learn TF-IDF vectorizer (default, no model download)
  - SentenceTransformerBackend: sentence-transformers (optional, lazy import)
  - KeywordBackend: original Jaccard fallback (backward compat)
  - decay_factor: time-based decay for experience relevance
  - effective_score: similarity * decay

Selection priority (auto-detect at runtime):
  1. SentenceTransformerBackend (if `sentence_transformers` importable)
  2. TfidfBackend (if `sklearn` importable)
  3. KeywordBackend (always available)

Override via env var AGENT_MEMORY_BACKEND={tfidf|sentence|keyword}.
Override via env var AGENT_MEMORY_USE_KEYWORDS=1 to force keyword mode.
"""

import logging
import math
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from agent_system.memory.graph import GraphNode

logger = logging.getLogger(__name__)


# ── Time decay ──

DEFAULT_HALF_LIFE_DAYS = 30.0


def decay_factor(
    node: GraphNode,
    now: Optional[datetime] = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """
    Time-based decay. Returns 1.0 for a brand-new node, 0.5 for a node
    at the half-life age, 0.25 at 2x half-life, etc.
    """
    if half_life_days <= 0:
        return 1.0
    ts = now or datetime.now(timezone.utc)
    created = node.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (ts - created).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def effective_score(
    similarity: float,
    node: GraphNode,
    now: Optional[datetime] = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Combine similarity with time decay."""
    return similarity * decay_factor(node, now=now, half_life_days=half_life_days)


# ── Embedding protocol ──

class EmbeddingBackend(ABC):
    """Protocol for computing text similarity."""

    name: str = "base"

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Return a vector per text."""

    @abstractmethod
    def similarity(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return similarity in [0, 1]."""


# ── Keyword (Jaccard) backend ──

class KeywordBackend(EmbeddingBackend):
    """Original Jaccard overlap. Always available, no dependencies."""
    name = "keyword"

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        # Each text becomes a set of word-presence indicators.
        return [self._word_set(t) for t in texts]

    def _word_set(self, text: str) -> List[float]:
        words = (text or "").lower().split()
        # Truncate to keep vectors small.
        return [1.0 if w in words else 0.0 for w in set(words)]

    def similarity(self, a: Sequence[float], b: Sequence[float]) -> float:
        # Use set-based Jaccard for cleaner comparison.
        words_a = set((a or []))
        words_b = set((b or []))
        if not isinstance(a, set) and isinstance(a, list):
            # fall back to length-based compare
            return _jaccard_lists(a, b)
        return _jaccard_sets(set(a), set(b))

    def compute_similarity(self, text_a: str, text_b: str) -> float:
        return _jaccard_words(text_a, text_b)


def _jaccard_words(a: str, b: str) -> float:
    wa = set((a or "").lower().split()[:200])
    wb = set((b or "").lower().split()[:200])
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _jaccard_lists(a, b) -> float:
    sa = {x for x in a if x}
    sb = {x for x in b if x}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def _jaccard_sets(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


# ── TF-IDF backend (sklearn) ──

class TfidfBackend(EmbeddingBackend):
    """TF-IDF vectorizer over a small corpus. No model download required."""
    name = "tfidf"

    def __init__(self):
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
        except ImportError as e:
            raise ImportError(
                "TfidfBackend requires scikit-learn. Install with: pip install scikit-learn"
            ) from e
        self._TfidfVectorizer = TfidfVectorizer
        self._fitted = False
        self._vectorizer = None
        self._corpus_vectors = None
        self._corpus_texts: List[str] = []

    def _fit(self, texts: Sequence[str]):
        if not texts:
            self._vectorizer = self._TfidfVectorizer()
            self._corpus_vectors = []
            self._fitted = True
            return
        self._vectorizer = self._TfidfVectorizer(
            lowercase=True, stop_words="english", max_features=2048
        )
        self._corpus_vectors = self._vectorizer.fit_transform(texts)
        self._corpus_texts = list(texts)
        self._fitted = True

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        # If no corpus has been fit, fit on the given texts (single-text mode).
        if not self._fitted:
            self._fit(texts)
        if not self._vectorizer:
            return [[0.0] for _ in texts]
        vecs = self._vectorizer.transform(texts)
        return [v.toarray()[0].tolist() for v in vecs]

    def similarity(self, a: Sequence[float], b: Sequence[float]) -> float:
        # Cosine similarity between two sparse-ish vectors.
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def find_top_k(self, query: str, candidates: Sequence[str], k: int = 5) -> List[Tuple[int, float]]:
        """Convenience: return top-k (index, similarity) pairs."""
        if not candidates:
            return []
        self._fit(candidates)
        q_vec = self._vectorizer.transform([query])  # type: ignore
        # cosine similarity
        from sklearn.metrics.pairwise import cosine_similarity  # type: ignore
        scores = cosine_similarity(q_vec, self._corpus_vectors).flatten()
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:k]
        return [(i, float(s)) for i, s in ranked]


# ── Sentence-transformer backend (optional) ──

class SentenceTransformerBackend(EmbeddingBackend):
    """Local neural embeddings. Optional, requires sentence-transformers."""
    name = "sentence"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as e:
            raise ImportError(
                "SentenceTransformerBackend requires sentence-transformers. "
                "Install with: pip install sentence-transformers"
            ) from e
        self.model = SentenceTransformer(model_name)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        return [v.tolist() for v in self.model.encode(list(texts), show_progress_bar=False)]

    def similarity(self, a: Sequence[float], b: Sequence[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)


# ── Auto-select backend ──

_default_backend: Optional[EmbeddingBackend] = None


def get_backend(force: Optional[str] = None) -> EmbeddingBackend:
    """
    Return the best available backend. Priority:
      1. force override
      2. AGENT_MEMORY_BACKEND env var
      3. SentenceTransformer (if importable)
      4. Tfidf (if sklearn importable)
      5. Keyword (always)
    """
    global _default_backend
    if _default_backend is not None:
        return _default_backend

    if force is None:
        force = os.environ.get("AGENT_MEMORY_BACKEND")

    if force == "keyword" or os.environ.get("AGENT_MEMORY_USE_KEYWORDS") == "1":
        _default_backend = KeywordBackend()
        return _default_backend

    if force == "sentence":
        try:
            _default_backend = SentenceTransformerBackend()
            return _default_backend
        except ImportError as e:
            logger.warning(f"sentence-transformers not available, falling back: {e}")

    if force is None or force == "tfidf":
        try:
            _default_backend = TfidfBackend()
            return _default_backend
        except ImportError as e:
            logger.debug(f"sklearn not available, falling back: {e}")

    _default_backend = KeywordBackend()
    return _default_backend


def reset_backend():
    """Force re-selection on next get_backend() call (testing only)."""
    global _default_backend
    _default_backend = None
