"""Embedders (R1-T1).

A port with two implementations, for the same reason the model backend is a
port: the test suite must not touch the network.

* ``OpenRouterEmbedder`` — real semantic embeddings, used to build the index.
* ``HashingEmbedder``    — deterministic, offline, zero extra dependencies.
  Used by the tests, and available as a fallback when no credential exists.

The hashing embedder is a real lexical embedder, not a stub that returns zeros:
it produces stable vectors with meaningful cosine distances, so retrieval logic
can be tested end to end without a network call. It matches on shared wording
rather than shared meaning, which is weaker — that difference is measured in
``evals/retrieval`` rather than assumed away.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any, Protocol, runtime_checkable

from app.config import Settings, get_settings

logger = logging.getLogger("frontdesk.knowledge")

HASHING_DIMENSIONS = 512
_TOKEN = re.compile(r"[a-z0-9]+")

# Words that appear in almost every symptom description and carry no signal.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "or",
        "the",
        "of",
        "to",
        "in",
        "on",
        "with",
        "for",
        "is",
        "are",
        "be",
        "been",
        "being",
        "at",
        "by",
        "from",
        "as",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "often",
        "may",
        "can",
        "sometimes",
        "usually",
        "more",
        "most",
        "other",
        "such",
        "not",
        "no",
    ]
)


@runtime_checkable
class Embedder(Protocol):
    name: str
    dimensions: int

    confident_score: float
    """The similarity above which a match is *confident* in this space.

    A property of the embedding geometry, not of the corpus, which is why it
    lives on the embedder rather than as one constant somewhere. The two
    implementations here separate at measurably different points, and using one
    number for both would either make the real embedder credulous or the hashing
    one mute.

    Distinct from ``DEFAULT_MIN_SCORE``, which answers "is this a match at all"
    and was tuned for routing a patient's plain-language complaint to a visit
    type. This answers "is this strong enough to summarise for a clinician", and
    a clinician-facing summary earns a higher bar than a scheduling decision.
    """

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalise(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in vector))
    if length == 0.0:
        return vector
    return [value / length for value in vector]


class HashingEmbedder:
    """Hashed bag-of-words with sublinear term weighting, L2-normalised.

    Deterministic across runs and machines, which is what the hermetic test
    suite needs: the same corpus always produces the same index, so a retrieval
    assertion is a real assertion rather than a coin toss.
    """

    name = "hashing-v1"

    confident_score = 0.30
    """Measured on the 65-record corpus: real presentations score 0.41–0.66,
    invented clinical-sounding jargon 0.00–0.14, gibberish 0.00. A token-hash bag
    separates these unusually well, because an invented word hashes to buckets
    the corpus never fills."""

    def __init__(self, dimensions: int = HASHING_DIMENSIONS) -> None:
        self.dimensions = dimensions

    def _tokens(self, text: str) -> list[str]:
        return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 2]

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            counts: dict[int, float] = {}
            for token in self._tokens(text):
                index = int(hashlib.md5(token.encode()).hexdigest()[:8], 16) % self.dimensions
                counts[index] = counts.get(index, 0.0) + 1.0
            vector = [0.0] * self.dimensions
            for index, count in counts.items():
                # Sublinear scaling: a word repeated five times is not five
                # times as important.
                vector[index] = 1.0 + math.log(count)
            vectors.append(_normalise(vector))
        return vectors


class OpenRouterEmbedder:
    """Real semantic embeddings via OpenRouter's embeddings endpoint.

    Verified against the live API. Batched, because 264 chunks in one request is
    both faster and cheaper than 264 requests.
    """

    def __init__(self, settings: Settings | None = None, client: Any = None) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self.name = self._settings.embedding_model
        self.dimensions = 1536

        # Measured against the live endpoint on the 65-record corpus: real
        # presentations score 0.71–0.74, invented clinical-sounding jargon
        # 0.37–0.45, gibberish 0.18. A dense space puts invented morphemes near
        # the real words they are built from, so the confident bar sits far above
        # where a patient-routing floor would.
        self.confident_score = 0.55

    @property
    def client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=60.0)
        return self._client

    def embed(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        if not self._settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for OpenRouterEmbedder")

        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            response = self.client.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={"Authorization": f"Bearer {self._settings.openrouter_api_key}"},
                json={"model": self._settings.embedding_model, "input": batch},
            )
            response.raise_for_status()
            payload = response.json()["data"]
            # Results come back with an index; sorting guards against a provider
            # that does not preserve request order.
            vectors.extend(item["embedding"] for item in sorted(payload, key=lambda d: d["index"]))
        self.dimensions = len(vectors[0]) if vectors else self.dimensions
        return vectors


def build_embedder(settings: Settings | None = None) -> Embedder:
    """The configured embedder, falling back to hashing when offline."""
    settings = settings or get_settings()
    if settings.embedding_provider == "hashing":
        return HashingEmbedder()
    if not settings.openrouter_api_key:
        logger.warning("no OPENROUTER_API_KEY; falling back to the hashing embedder")
        return HashingEmbedder()
    return OpenRouterEmbedder(settings)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))
