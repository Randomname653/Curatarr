"""
Curatarr - Re-Embed Backfill
=============================

One-off recovery for items that were text-enriched while the embedding model
(nomic-embed-text) was missing from Ollama. Those items have
``EnrichmentStatus.enriched = 1`` but ``vector_ready = 0`` and no vector in
ChromaDB, because every ``/api/embeddings`` call 404'd during their run.

This script re-runs ONLY the embedding step (no LLM, no API re-fetch) for each
such item, reusing the exact storage logic the app uses:

  1. Load the cached enrichment profile  (MetadataCache: ``enriched:{cat}:{key}``)
  2. Embed ``profile["embedding_text"]``  (EmbeddingGenerator -> nomic-embed-text)
  3. Store the vector in ChromaDB         (chroma_db.add_documents, same doc_id
                                           + metadata as media_enricher ~L2474)
  4. Flag the row ``vector_ready = True``  (same update taste_engine.py ~L552 does)

Run with the Curatarr app STOPPED — ChromaDB's PersistentClient is single-writer
and the SQLite DB shouldn't be written by two processes at once.

Usage
-----
    python scripts/revectorize_backfill.py --dry-run        # inspect, no writes
    python scripts/revectorize_backfill.py --limit 20       # do only 20 (smoke test)
    python scripts/revectorize_backfill.py                  # full backfill
    python scripts/revectorize_backfill.py --category movie # one category only
"""

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, ".")

# Windows consoles default to cp1252 and choke on the progress glyphs.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s  %(levelname)s  %(name)s — %(message)s")

from src.cache.metadata_cache import MetadataCache
from src.database.connection import get_db_session
from src.database.models import EnrichmentStatus
from src.embeddings.embedding_generator import EmbeddingGenerator


def _as_list(v):
    """Coerce a profile field to a list of strings (genres/themes/mood)."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x]
    return [str(v)]


def _load_targets(category: str | None) -> list[dict]:
    """All rows that are text-enriched but have no vector yet."""
    with get_db_session() as db:
        q = db.query(
            EnrichmentStatus.id,
            EnrichmentStatus.plex_rating_key,
            EnrichmentStatus.title,
            EnrichmentStatus.media_category,
        ).filter(
            EnrichmentStatus.enriched == True,          # noqa: E712
            EnrichmentStatus.vector_ready == False,      # noqa: E712
        )
        if category:
            q = q.filter(EnrichmentStatus.media_category == category)
        return [
            {"id": r.id, "plex_rating_key": r.plex_rating_key,
             "title": r.title, "media_category": r.media_category}
            for r in q.all()
        ]


def _chroma_payload(profile: dict, row: dict):
    """Replicate media_enricher's doc_id + metadata for a profile."""
    media_type = profile.get("media_type") or row["media_category"] or "movie"
    metadata = {
        "title":      profile.get("title") or row["title"] or "",
        "media_type": media_type,
        "domain":     media_type,   # hard quarantine key for gated retrieval
        "genres":     ", ".join(_as_list(profile.get("genres"))),
        "themes":     ", ".join(_as_list(profile.get("themes"))),
        "mood":       ", ".join(_as_list(profile.get("mood"))),
        "year":       profile.get("year") or 0,
    }
    doc_id = str(
        row["plex_rating_key"]
        or profile.get("tmdb_id")
        or profile.get("anilist_id")
        or profile.get("title")
        or row["title"]
    )
    return doc_id, metadata


def _store_vector(chroma_db, doc_id, text, vec, metadata):
    """Upsert into ChromaDB — add, falling back to delete+re-add (media_enricher)."""
    try:
        chroma_db.add_documents(documents=[text], embeddings=[vec],
                                metadatas=[metadata], ids=[doc_id])
    except Exception:
        try:
            chroma_db.delete_by_id(doc_id)
        except Exception:
            pass
        chroma_db.add_documents(documents=[text], embeddings=[vec],
                                metadatas=[metadata], ids=[doc_id])


async def main():
    ap = argparse.ArgumentParser(description="Re-embed text-enriched items that have no vector.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report cache/text coverage + smoke-test 3 embeds, write nothing.")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N items (0 = all).")
    ap.add_argument("--category", choices=["movie", "show", "anime", "music"],
                    help="Restrict to one category.")
    ap.add_argument("--batch", type=int, default=100, help="Commit vector_ready every N successes.")
    args = ap.parse_args()

    targets = _load_targets(args.category)
    if args.limit:
        targets = targets[:args.limit]

    total = len(targets)
    print(f"\nBackfill targets (enriched=1, vector_ready=0): {total}"
          + (f"  [category={args.category}]" if args.category else ""))
    if total == 0:
        print("Nothing to do.\n")
        return

    cache = MetadataCache()

    # ── Dry run: coverage + a tiny embed smoke test, no writes ────────────────
    if args.dry_run:
        have_cache = have_text = 0
        missing_examples = []
        for row in targets:
            cached = cache.get_cache(f"enriched:{row['media_category']}:{row['plex_rating_key']}")
            prof = cached["response"] if cached else None
            if prof:
                have_cache += 1
                if prof.get("embedding_text"):
                    have_text += 1
                elif len(missing_examples) < 5:
                    missing_examples.append(f"{row['title']} (no embedding_text)")
            elif len(missing_examples) < 5:
                missing_examples.append(f"{row['title']} (no cache entry)")

        print(f"  cached profile present : {have_cache}/{total}")
        print(f"  has embedding_text     : {have_text}/{total}  <- backfillable")
        if missing_examples:
            print("  examples not backfillable:")
            for m in missing_examples:
                print(f"    - {m}")

        # Smoke-test the embed pipeline on the first few that have text.
        gen = EmbeddingGenerator()
        tested = 0
        for row in targets:
            if tested >= 3:
                break
            cached = cache.get_cache(f"enriched:{row['media_category']}:{row['plex_rating_key']}")
            prof = cached["response"] if cached else None
            text = (prof or {}).get("embedding_text")
            if not text:
                continue
            vec = await gen.generate_embedding(text)
            doc_id, meta = _chroma_payload(prof, row)
            print(f"  embed test: '{row['title']}' -> dim={len(vec)}  doc_id={doc_id}  "
                  f"genres={meta['genres'][:40]!r}")
            tested += 1
        await gen.close()
        cache.close()
        print("\nDRY RUN — nothing written. Re-run without --dry-run to apply.\n")
        return

    # ── Real run ──────────────────────────────────────────────────────────────
    from src.vector_store.chromadb_wrapper import chroma_db

    gen = EmbeddingGenerator()
    done = no_cache = no_text = embed_fail = err = 0
    pending_ids: list[int] = []

    def _flush(ids):
        if not ids:
            return
        with get_db_session() as db:
            db.query(EnrichmentStatus).filter(
                EnrichmentStatus.id.in_(ids)
            ).update({"vector_ready": True}, synchronize_session=False)
            db.commit()

    for idx, row in enumerate(targets, 1):
        try:
            cached = cache.get_cache(f"enriched:{row['media_category']}:{row['plex_rating_key']}")
            prof = cached["response"] if cached else None
            if not prof:
                no_cache += 1
                continue
            text = prof.get("embedding_text")
            if not text:
                no_text += 1
                continue
            vec = await gen.generate_embedding(text)
            if not vec:
                embed_fail += 1
                continue
            doc_id, meta = _chroma_payload(prof, row)
            _store_vector(chroma_db, doc_id, text, vec, meta)
            pending_ids.append(row["id"])
            done += 1
        except Exception as e:
            err += 1
            logging.warning("backfill failed for '%s': %s", row.get("title"), e)

        if len(pending_ids) >= args.batch:
            _flush(pending_ids)
            pending_ids = []
        if idx % 50 == 0 or idx == total:
            print(f"  [{idx}/{total}]  vectorized={done}  no_cache={no_cache}  "
                  f"no_text={no_text}  embed_fail={embed_fail}  err={err}", flush=True)

    _flush(pending_ids)
    await gen.close()
    cache.close()

    print(f"\nDONE. vectorized={done}/{total}  "
          f"(no_cache={no_cache}, no_text={no_text}, embed_fail={embed_fail}, err={err})")
    if no_cache:
        print(f"  Note: {no_cache} items had no cached profile — they need a full "
              f"re-enrichment (force) to regenerate their data, not just a re-embed.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
