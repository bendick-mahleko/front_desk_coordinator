"""The vector store (R1-T2, T3, T5, T6).

A port with a Chroma implementation and an in-memory one, so the test suite can
exercise retrieval without a database directory or a network call.

Two properties matter more than the storage engine:

**Tier filtering happens in the query.** ``search`` requires the caller to name
the tiers it is allowed to read, and that becomes a ``where`` clause. Restricted
vectors are never candidates. There is no post-filtering step to forget.

**A weak match returns nothing.** Sixty-six records means every query has a
nearest neighbour, and the nearest neighbour to "my knee hurts" is *something*.
Below the score floor the honest answer is "no confident match", and the callers
are built to handle that rather than to present the least-bad row.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.knowledge.chunking import Chunk, Tier
from app.knowledge.embedding import Embedder, cosine

logger = logging.getLogger("frontdesk.knowledge")

DEFAULT_MIN_SCORE = 0.25
"""Below this, a hit is not reported. Tuned in evals/retrieval, not guessed."""


class Hit(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    disease: str
    field: str
    tier: Tier
    text: str
    score: float


class TierViolation(RuntimeError):
    """A search asked for a tier the caller is not permitted to read.

    Raised rather than filtered, because a caller asking for the wrong tier is a
    programming error and should fail loudly in a test, not degrade quietly in
    production.
    """


@runtime_checkable
class KnowledgeBase(Protocol):
    def index(self, chunks: list[Chunk]) -> int: ...

    def search(
        self, query: str, tiers: Iterable[Tier], k: int = 3, min_score: float = DEFAULT_MIN_SCORE
    ) -> list[Hit]: ...

    def get(self, chunk_id: str) -> Hit | None: ...

    def count(self) -> int: ...


def _validate_tiers(tiers: Iterable[Tier]) -> list[str]:
    values = [Tier(t).value for t in tiers]
    if not values:
        raise TierViolation("a search must name at least one tier")
    return values


class InMemoryKnowledgeBase:
    """Brute-force cosine search. Used by the tests, and adequate for 264 chunks."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []

    def index(self, chunks: list[Chunk]) -> int:
        self._chunks = list(chunks)
        self._vectors = self._embedder.embed([c.text for c in chunks])
        return len(self._chunks)

    def search(
        self, query: str, tiers: Iterable[Tier], k: int = 3, min_score: float = DEFAULT_MIN_SCORE
    ) -> list[Hit]:
        allowed = set(_validate_tiers(tiers))
        if not self._chunks:
            return []
        vector = self._embedder.embed([query])[0]

        scored = [
            Hit(
                chunk_id=chunk.chunk_id,
                disease=chunk.disease,
                field=chunk.field,
                tier=chunk.tier,
                text=chunk.text,
                score=cosine(vector, candidate),
            )
            for chunk, candidate in zip(self._chunks, self._vectors, strict=True)
            if chunk.tier.value in allowed
        ]
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return [hit for hit in scored[:k] if hit.score >= min_score]

    def get(self, chunk_id: str) -> Hit | None:
        for chunk in self._chunks:
            if chunk.chunk_id == chunk_id:
                return Hit(
                    chunk_id=chunk.chunk_id,
                    disease=chunk.disease,
                    field=chunk.field,
                    tier=chunk.tier,
                    text=chunk.text,
                    score=1.0,
                )
        return None

    def count(self) -> int:
        return len(self._chunks)


class ChromaKnowledgeBase:
    """Persistent vector store backed by ChromaDB.

    Embeddings are supplied explicitly rather than letting Chroma pick a default
    embedding function, so the same ``Embedder`` serves both implementations and
    the index cannot silently be built with something other than what queries
    use.
    """

    COLLECTION = "disease_knowledge"

    def __init__(self, embedder: Embedder, path: Path | str = "data/vectors") -> None:
        import chromadb

        self._embedder = embedder
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self._path))
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION,
            metadata={"hnsw:space": "cosine", "embedder": embedder.name},
        )

    def index(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        vectors = self._embedder.embed([chunk.text for chunk in chunks])
        self._collection.upsert(
            ids=[chunk.chunk_id for chunk in chunks],
            embeddings=cast("Any", vectors),
            documents=[chunk.text for chunk in chunks],
            metadatas=[chunk.metadata() for chunk in chunks],
        )
        return len(chunks)

    def search(
        self, query: str, tiers: Iterable[Tier], k: int = 3, min_score: float = DEFAULT_MIN_SCORE
    ) -> list[Hit]:
        allowed = _validate_tiers(tiers)
        vector = self._embedder.embed([query])[0]

        # The tier filter is part of the query. Restricted vectors are never
        # candidates, so there is no filtered-out result to leak.
        where: dict[str, Any] = (
            {"tier": allowed[0]} if len(allowed) == 1 else {"tier": {"$in": allowed}}
        )
        result = self._collection.query(
            query_embeddings=cast("Any", [vector]),
            n_results=k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        # Every field of a Chroma result is typed Optional even when `include`
        # asked for it. An empty result is a legitimate outcome, not an error.
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        distances = result.get("distances") or []
        if not documents or not metadatas or not distances:
            return []

        hits: list[Hit] = []
        for chunk_id, document, metadata, distance in zip(
            result["ids"][0],
            documents[0],
            metadatas[0],
            distances[0],
            strict=True,
        ):
            score = 1.0 - float(distance)  # cosine distance -> similarity
            if score < min_score:
                continue
            hits.append(
                Hit(
                    chunk_id=chunk_id,
                    disease=str(metadata["disease"]),
                    field=str(metadata["field"]),
                    tier=Tier(str(metadata["tier"])),
                    text=str(document),
                    score=score,
                )
            )
        return hits

    def get(self, chunk_id: str) -> Hit | None:
        result = self._collection.get(ids=[chunk_id], include=["documents", "metadatas"])
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        if not documents or not metadatas:
            return None
        metadata = metadatas[0]
        return Hit(
            chunk_id=chunk_id,
            disease=str(metadata["disease"]),
            field=str(metadata["field"]),
            tier=Tier(str(metadata["tier"])),
            text=str(documents[0]),
            score=1.0,
        )

    def count(self) -> int:
        return int(self._collection.count())


def build_knowledge_base(embedder: Embedder, path: Path | str | None = None) -> KnowledgeBase:
    """Chroma when it is available, in-memory when it is not."""
    try:
        return ChromaKnowledgeBase(embedder, path or "data/vectors")
    except Exception as exc:  # noqa: BLE001 - degrade rather than fail to start
        logger.warning("chromadb unavailable (%s); using the in-memory store", type(exc).__name__)
        return InMemoryKnowledgeBase(embedder)
