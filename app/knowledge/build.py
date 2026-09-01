"""Build the knowledge base — `uv run build-kb` (R1-T4).

Idempotent: chunk ids are derived from the disease name and field, so a rebuild
upserts rather than duplicating.

The source hash is reported on every build. If the CSV changes and the index is
not rebuilt, the two are out of step and nothing else in the system would
notice.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from app.config import get_settings
from app.knowledge.chunking import chunk_all
from app.knowledge.corpus import DEFAULT_SOURCE, load
from app.knowledge.embedding import build_embedder
from app.knowledge.store import build_knowledge_base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the disease knowledge base.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--path", default=None, help="where to write the vector store")
    parser.add_argument(
        "--embedder",
        choices=["openrouter", "hashing"],
        default=None,
        help="override EMBEDDING_PROVIDER",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.embedder:
        settings = settings.model_copy(update={"embedding_provider": args.embedder})

    report = load(Path(args.source))
    print(report.render())
    if not report.ok:
        print("nothing to index")
        return 1

    chunks = chunk_all(report.records)
    tiers = Counter(chunk.tier.value for chunk in chunks)

    embedder = build_embedder(settings)
    store = build_knowledge_base(embedder, args.path or settings.vector_store_path)

    print(f"\nembedder : {embedder.name}")
    print(f"store    : {type(store).__name__}")
    print(f"chunks   : {len(chunks)}")
    for tier, count in sorted(tiers.items()):
        print(f"  {tier:<16} {count}")

    indexed = store.index(chunks)
    print(f"\nindexed {indexed} chunk(s); collection now holds {store.count()}")
    print(f"source sha256: {report.source_sha256}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
