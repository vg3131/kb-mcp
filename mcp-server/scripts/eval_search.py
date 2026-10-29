"""
Retrieval evaluation: does the right document come back for a question?

Compares four retrievers on eval/questions.jsonl:
  current  - the existing in-memory scorer (kb/search.py) — the baseline
  keyword  - Atlas BM25 only
  vector   - Atlas vector search only
  hybrid   - both, fused with RRF using the tuned defaults in kb/hybrid.py
  cap2+pin - single tool: hybrid, at most 2 product rows, exact SKUs pinned 1st
  split    - two tools: product questions -> search_products (product-only
             hybrid + SKU pinning), everything else -> search_documents
             (documents only). Assumes Claude picks the right tool, which the
             tool descriptions are written to make easy.

Metrics, per question kind and overall:
  hit@1  - a relevant document is the top result
  hit@5  - a relevant document is in the top 5 (what search returns by default)
  MRR    - mean reciprocal rank of the first relevant result within the top 10
           (1.0 = always first, 0.5 = typically second, 0 = never in top 10)

Query embeddings are fetched in ONE batched Voyage call and cached in
eval/.query_cache.json, so re-runs (e.g. after tuning fusion weights) cost
nothing and don't hit free-tier rate limits.

Usage, from mcp-server/ with the venv active:
    python scripts/eval_search.py
    python scripts/eval_search.py --show-misses
    python scripts/eval_search.py --sweep     # try fusion settings (no extra API calls)
"""

import argparse
import glob
import itertools
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kb.config import load_env  # noqa: E402

load_env()

from kb import search as current_search  # noqa: E402
from kb.embeddings import EMBEDDING_MODEL, embed  # noqa: E402
from kb.hybrid import cap_products, exact_products, fuse, keyword_ranked, pin, vector_ranked  # noqa: E402
from kb.index import build_index  # noqa: E402

KB_ROOT = os.path.join(ROOT, "worx-techtalk-markdown-kb")
QUESTIONS = os.path.join(ROOT, "eval", "questions.jsonl")
CACHE = os.path.join(ROOT, "eval", ".query_cache.json")
K = 10
METHODS = ["current", "keyword", "vector", "hybrid", "cap2+pin", "split"]


def load_questions() -> list[dict]:
    with open(QUESTIONS, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def catalogue_pages_by_code() -> dict[str, set[str]]:
    """The current search has no per-product rows — it finds products via the
    catalogue/*.md table pages. Count such a page as a hit for a product
    question if it contains that SKU, so the baseline is judged fairly."""
    pages: dict[str, set[str]] = defaultdict(set)
    for path in glob.glob(os.path.join(KB_ROOT, "catalogue", "*.md")):
        rel = os.path.relpath(path, KB_ROOT).replace(os.sep, "/")
        text = open(path, encoding="utf-8").read()
        for line in text.splitlines():
            if line.startswith("| ") and not line.startswith("| Code") and not line.startswith("| ---"):
                pages[line.split("|")[1].strip()].add(rel)
    return pages


def query_vectors(queries: list[str]) -> dict[str, list[float]]:
    cache = {}
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE))
    key = lambda q: f"{EMBEDDING_MODEL}::{q}"  # noqa: E731
    missing = [q for q in queries if key(q) not in cache]
    if missing:
        print(f"Embedding {len(missing)} queries (one batched call) ...")
        for q, v in zip(missing, embed(missing, input_type="query")):
            cache[key(q)] = v
        json.dump(cache, open(CACHE, "w"))
    return {q: cache[key(q)] for q in queries}


def first_relevant_rank(results: list[str], relevant: set[str]) -> int | None:
    for i, r in enumerate(results[:K], 1):
        if r in relevant:
            return i
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-misses", action="store_true", help="print what each method returned when it missed")
    ap.add_argument("--sweep", action="store_true", help="grid-search RRF k / keyword weight / keyword depth")
    args = ap.parse_args()

    questions = load_questions()
    index = build_index(KB_ROOT)
    cat_pages = catalogue_pages_by_code()
    vectors = query_vectors([q["query"] for q in questions])

    rows = []
    for q in questions:
        relevant = set(q["relevant"])
        relevant_current = set(relevant)
        for r in q["relevant"]:
            if r.startswith("product:"):
                relevant_current |= cat_pages.get(r.split(":", 1)[1], set())

        qv = vectors[q["query"]]
        vec = vector_ranked(qv)
        kw = keyword_ranked(q["query"])
        exact = exact_products(q["query"])
        if q["kind"] == "product":
            split_lists = {"vector": vector_ranked(qv, "product"), "keyword": keyword_ranked(q["query"], "product")}
            split = pin(fuse(split_lists), exact)
        else:
            split_lists = {
                "vector": vector_ranked(qv, include_products=False),
                "keyword": keyword_ranked(q["query"], include_products=False),
            }
            split = fuse(split_lists)
        results = {
            "current": [d["path"] for d in current_search.search(index, q["query"], limit=K)],
            "keyword": [h.key for h in fuse({"keyword": kw})],
            "vector": [h.key for h in fuse({"vector": vec})],
            "hybrid": [h.key for h in fuse({"vector": vec, "keyword": kw})],
            "cap2+pin": [h.key for h in pin(cap_products(fuse({"vector": vec, "keyword": kw}), 2), exact)],
            "split": [h.key for h in split],
        }
        ranks = {
            m: first_relevant_rank(results[m], relevant_current if m == "current" else relevant)
            for m in METHODS
        }
        rows.append({"q": q, "ranks": ranks, "results": results, "lists": {"vector": vec, "keyword": kw}, "relevant": relevant})

    # Per-question table
    print(f"\n{'question':<16} {'kind':<11}" + "".join(f"{m:>9}" for m in METHODS))
    for row in rows:
        cells = "".join(f"{(str(row['ranks'][m]) if row['ranks'][m] else '–'):>9}" for m in METHODS)
        print(f"{row['q']['id']:<16} {row['q']['kind']:<11}{cells}")
    print("(number = rank of first relevant doc in top 10, – = not found)")

    # Aggregate metrics
    def metrics(subset):
        out = {}
        for m in METHODS:
            rs = [r["ranks"][m] for r in subset]
            n = len(rs)
            out[m] = (
                sum(1 for r in rs if r == 1) / n,
                sum(1 for r in rs if r and r <= 5) / n,
                sum(1 / r for r in rs if r) / n,
            )
        return out

    groups = {"overall": rows}
    for kind in sorted({r["q"]["kind"] for r in rows}):
        groups[kind] = [r for r in rows if r["q"]["kind"] == kind]

    print(f"\n{'group':<12} {'n':>3}  {'method':<8} {'hit@1':>6} {'hit@5':>6} {'MRR':>6}")
    for name, subset in groups.items():
        for i, (m, (h1, h5, mrr)) in enumerate(metrics(subset).items()):
            label = f"{name:<12} {len(subset):>3}" if i == 0 else " " * 16
            print(f"{label}  {m:<8} {h1:>6.0%} {h5:>6.0%} {mrr:>6.2f}")
        print()

    # Do product rows crowd out documents on non-product questions?
    non_product = [r for r in rows if r["q"]["kind"] != "product"]
    for m in ("hybrid", "cap2+pin", "split"):
        crowd = sum(sum(1 for k in r["results"][m][:5] if k.startswith("product:")) for r in non_product)
        print(f"Products in {m} top-5 for non-product questions: {crowd} of {5 * len(non_product)} slots")

    if args.sweep:
        sweep(rows)

    if args.show_misses:
        for row in rows:
            for m in METHODS:
                if not row["ranks"][m] or row["ranks"][m] > 5:
                    print(f"\n[{m}] {row['q']['id']}: {row['q']['query']}")
                    for k in row["results"][m][:5]:
                        print(f"    {k}")


def sweep(rows: list[dict]) -> None:
    """Re-fuse the already-retrieved lists under different settings. Only the
    fusion step changes, so this is instant and makes no API/Atlas calls.
    Beware overfitting: with 34 questions, prefer a setting from a *region*
    of good results over the single best cell."""
    kinds = sorted({r["q"]["kind"] for r in rows})
    results = []
    for k, kw_w, kw_depth in itertools.product([5, 10, 20, 60], [0.3, 0.5, 0.7, 1.0], [5, 10, 20, 40]):
        per = defaultdict(list)
        for r in rows:
            fused = [h.key for h in fuse(r["lists"], {"vector": 1.0, "keyword": kw_w}, k, {"keyword": kw_depth})]
            rank = first_relevant_rank(fused, r["relevant"])
            for g in ("overall", r["q"]["kind"]):
                per[g].append(rank)
        stats = {g: (sum(1 for x in v if x and x <= 5) / len(v), sum(1 / x for x in v if x) / len(v)) for g, v in per.items()}
        results.append(((k, kw_w, kw_depth), stats))

    results.sort(key=lambda r: (-r[1]["overall"][1], -r[1]["overall"][0]))
    head = f"{'k':>3} {'kw_w':>5} {'kw_n':>5} | {'all h@5':>7} {'MRR':>5}" + "".join(f" | {g[:5]:>5} h@5 {'MRR':>5}" for g in kinds)
    print("\nFusion sweep — top 12 by overall MRR (vector weight fixed at 1.0)")
    print(head)
    for (k, w, d), st in results[:12]:
        line = f"{k:>3} {w:>5} {d:>5} | {st['overall'][0]:>7.0%} {st['overall'][1]:>5.2f}"
        line += "".join(f" | {st[g][0]:>9.0%} {st[g][1]:>5.2f}" for g in kinds)
        print(line)
    for label, cfg in (("original default", (60, 1.0, 40)), ("tuned default", (5, 0.7, 10))):
        st = next(st for (c, st) in results if c == cfg)
        print(f"{label} (k={cfg[0]}, kw_w={cfg[1]}, kw_n={cfg[2]}): all h@5 {st['overall'][0]:.0%}, MRR {st['overall'][1]:.2f}")


if __name__ == "__main__":
    main()
