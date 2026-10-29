"""
Sync the markdown knowledge base + product catalogue into MongoDB Atlas.

The markdown files stay the single source of truth; Atlas is a derived copy
that this script can always rebuild. Safe to run as often as you like:

  - Unchanged chunks are NOT re-embedded (content hash comparison), so a
    normal re-run costs zero Voyage tokens.
  - Changed / new chunks are embedded and upserted.
  - Chunks whose source file or product row disappeared are deleted.
  - Both search indexes are created (or updated) if needed.

Usage, from mcp-server/ with the venv active:

    python scripts/ingest.py --dry-run   # chunk + report, no network calls
    python scripts/ingest.py             # sync for real
    python scripts/ingest.py --force     # re-embed everything (e.g. new model)
    python scripts/ingest.py --throttle  # Voyage account without a payment method

Each embedded batch is written to Atlas immediately, so if a run is
interrupted, the next run picks up where it stopped (already-stored chunks
match their hash and are skipped).
"""

import argparse
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.config import load_env  # noqa: E402

load_env()

from pymongo import DeleteMany, UpdateOne  # noqa: E402

from kb.atlas import chunks_collection, ensure_regular_indexes, ensure_search_indexes, wait_until_queryable  # noqa: E402
from kb.chunking import Chunk, build_chunks  # noqa: E402
from kb.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, embed_batches, estimate_tokens  # noqa: E402

KB_ROOT = os.environ.get(
    "KB_ROOT", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worx-techtalk-markdown-kb")
)
if not os.path.isabs(KB_ROOT):
    KB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), KB_ROOT)


def to_document(c: Chunk) -> dict:
    doc = {
        "doc_path": c.doc_path,
        "chunk_index": c.chunk_index,
        "title": c.title,
        "heading": c.heading,
        "content_type": c.content_type,
        "category": c.category,
        "text": c.text,
        "embed_hash": c.embed_hash,
        "embedding_model": EMBEDDING_MODEL,
    }
    doc.update(c.extra)
    return doc


def report(chunks: list[Chunk]) -> None:
    by_type = Counter(c.content_type for c in chunks)
    chars = sum(len(c.embed_text) for c in chunks)
    print(f"{len(chunks)} chunks from {KB_ROOT}")
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {t:<18} {n}")
    split = Counter(c.doc_path for c in chunks if c.doc_path and c.chunk_index > 0)
    for path in split:
        n = sum(1 for c in chunks if c.doc_path == path)
        print(f"  split into {n} sections: {path}")
    print(f"  ~{chars // 4:,} tokens to embed on a full run (rough chars/4 estimate)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="chunk and report only; no Atlas or Voyage calls")
    ap.add_argument("--force", action="store_true", help="re-embed every chunk even if unchanged")
    ap.add_argument("--throttle", action="store_true", help="small, paced batches for Voyage free-tier rate limits")
    ap.add_argument("--no-wait", action="store_true", help="don't wait for the search indexes to become queryable")
    args = ap.parse_args()

    chunks = build_chunks(KB_ROOT)
    ids = [c.id for c in chunks]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    if dupes:
        sys.exit(f"Duplicate chunk ids (fix before ingesting): {dupes[:5]}")
    report(chunks)
    if args.dry_run:
        return

    coll = chunks_collection()
    t0 = time.perf_counter()

    # What's already stored, so unchanged chunks skip the embedding call.
    stored = {
        d["_id"]: (d.get("embed_hash"), d.get("embedding_model"))
        for d in coll.find({}, {"embed_hash": 1, "embedding_model": 1})
    }
    to_embed = [
        c for c in chunks
        if args.force or stored.get(c.id) != (c.embed_hash, EMBEDDING_MODEL)
    ]
    print(f"\n{len(to_embed)} new/changed chunks to embed, {len(chunks) - len(to_embed)} unchanged")

    # 1. Embed new/changed chunks batch by batch, writing each batch straight
    #    to Atlas so progress survives an interruption.
    inserted = updated = 0
    if to_embed:
        est = sum(estimate_tokens(c.embed_text) for c in to_embed)
        mode = "throttled" if args.throttle else "normal"
        print(f"Embedding with {EMBEDDING_MODEL} (~{est:,} tokens, {mode} mode) ...")
        done = 0
        for start, vectors in embed_batches([c.embed_text for c in to_embed], "document", args.throttle):
            ops = []
            for c, v in zip(to_embed[start : start + len(vectors)], vectors):
                if len(v) != EMBEDDING_DIMENSIONS:
                    sys.exit(f"Unexpected vector size {len(v)} for {c.id}")
                fields = to_document(c)
                fields["embedding"] = v
                ops.append(UpdateOne({"_id": c.id}, {"$set": fields}, upsert=True))
            r = coll.bulk_write(ops, ordered=False)
            inserted += r.upserted_count
            updated += r.modified_count
            done += len(vectors)
            print(f"  embedded + saved {done}/{len(to_embed)}")

    # 2. Refresh metadata on unchanged chunks (cheap; catches e.g. a price
    #    change that doesn't alter the embedded text) and delete stale chunks.
    embedded_ids = {c.id for c in to_embed}
    ops = [
        UpdateOne({"_id": c.id}, {"$set": to_document(c)}, upsert=True)
        for c in chunks
        if c.id not in embedded_ids
    ]
    stale = set(stored) - set(ids)
    if stale:
        ops.append(DeleteMany({"_id": {"$in": sorted(stale)}}))
    deleted = 0
    if ops:
        r = coll.bulk_write(ops, ordered=False)
        updated += r.modified_count
        deleted = r.deleted_count
    print(f"Atlas: {inserted} inserted, {updated} updated, {deleted} deleted (stale)")

    print("Indexes:")
    ensure_regular_indexes(coll)
    ensure_search_indexes(coll)
    if not args.no_wait:
        ready = wait_until_queryable(coll)
        print("  both indexes queryable" if ready else "  indexes still building — check Atlas → Search")

    print(f"Done in {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
