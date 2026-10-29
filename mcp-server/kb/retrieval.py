"""
What the MCP tools actually call: Atlas hybrid retrieval, with the original
on-disk search as an automatic fallback.

  search        -> kb.hybrid.search_documents (knowledge documents only)
  list_products -> kb.hybrid.search_products when a query is given
                   (products only, exact SKUs pinned first)

Atlas decides WHICH documents are relevant; the bodies are still served from
the markdown bundled with the server (the in-memory DOC_INDEX / products.csv),
so responses keep exactly the same shape as before and the markdown stays the
single source of truth.

If Atlas or Voyage is unavailable (no credentials, network error, rate limit),
each function logs why and falls back to the pre-Atlas implementation, so the
tools degrade to the old keyword search instead of erroring. Every response
says which path served it, in a `retrieval` field.
"""

import sys

from kb import catalogue as catalogue_mod
from kb import search as keyword_search
from kb.chunking import INCLUDED_CONTENT_TYPES
from kb.index import DocEntry

# Same defensive cap the original search() used: ~100K chars of bodies per
# call keeps responses under the tool-result size limit.
MAX_TOTAL_BODY_CHARS = keyword_search.MAX_TOTAL_BODY_CHARS

# Ranked product results beyond this are long-tail noise, not answers.
MAX_RANKED_PRODUCTS = 25


def _log(msg: str) -> None:
    print(f"[kb.retrieval] {msg}", file=sys.stderr, flush=True)


def _doc_result(doc: DocEntry, score: float | None, retrieval: str, matched_by: dict | None = None) -> dict:
    out = {
        "path": doc.path,
        "title": doc.title,
        "content_type": doc.content_type,
        "category": doc.category,
        "score": round(score, 4) if score is not None else None,
        "retrieval": retrieval,
        "frontmatter": doc.frontmatter,
        "body": doc.body,
    }
    if matched_by:
        out["matched_by"] = matched_by
    return out


def search_documents(
    index: list[DocEntry],
    query: str,
    content_type: str | None = None,
    category: str | None = None,
    limit: int = 5,
) -> list[dict]:
    if not query.strip():
        return []

    # Content types that were never ingested (consolidated/*, catalogue
    # pages, index pages...) only exist on disk — use the original search.
    if content_type and content_type not in INCLUDED_CONTENT_TYPES:
        return _keyword_fallback(index, query, content_type, category, limit, reason=None)

    try:
        from kb import hybrid  # imported lazily: needs pymongo + credentials

        hits = hybrid.search_documents(query, content_type=content_type, category=category, limit=limit)
    except Exception as e:  # Atlas/Voyage down, bad credentials, rate limit...
        return _keyword_fallback(index, query, content_type, category, limit, reason=f"{type(e).__name__}: {e}")

    by_path = {d.path: d for d in index}
    results, total = [], 0
    for h in hits:
        doc = by_path.get(h.key)
        if doc is None:
            # Atlas has a document the deployed files don't (ingest ran on a
            # newer/older checkout). Skip rather than return a stale body.
            _log(f"Atlas returned {h.key!r}, not in the bundled markdown — re-run ingest.py")
            continue
        if results and total + len(doc.body) > MAX_TOTAL_BODY_CHARS:
            break
        total += len(doc.body)
        results.append(_doc_result(doc, h.score, "hybrid", h.ranks))
    return results


def _keyword_fallback(index, query, content_type, category, limit, reason: str | None) -> list[dict]:
    if reason:
        _log(f"hybrid search unavailable, using keyword fallback ({reason[:200]})")
    results = keyword_search.search(index, query, content_type=content_type, category=category, limit=limit)
    for r in results:
        r["retrieval"] = "keyword_fallback" if reason else "keyword"
    return results


def _products_fallback(kb_root, category, max_price, query, limit) -> list[dict]:
    """Offline product lookup: exact SKU matches first (codes never appear in
    descriptions, so substring search alone can't find them), then the
    original substring match on whatever text is left in the query."""
    exact = catalogue_mod.exact_code_rows(kb_root, query, max_price)
    results = [{**r, "retrieval": "exact_code"} for r in exact]
    seen = {r["code"] for r in exact}
    remaining = " ".join(catalogue_mod.PRODUCT_CODE.sub(" ", query).split())
    if remaining and not (exact and catalogue_mod.is_code_lookup(query)):
        rows = catalogue_mod.list_products(kb_root, category=category, max_price=max_price, query=remaining, limit=limit)
        results += [{**r, "retrieval": "substring_fallback"} for r in rows if r["code"] not in seen]
    return results[:limit]


def _canonical_category(kb_root: str, category: str | None) -> str | None:
    """The original tool matched category case-insensitively; Atlas filters
    are exact. Map the caller's spelling onto the catalogue's own."""
    if not category:
        return None
    names = {r["category"].lower(): r["category"] for r in catalogue_mod._load_rows(kb_root) if r["category"]}
    return names.get(category.lower(), category)


def list_products(
    kb_root: str,
    category: str | None = None,
    max_price: float | None = None,
    query: str | None = None,
    limit: int = 200,
) -> list[dict]:
    # No query = browsing by category/price: a plain deterministic filter is
    # exactly right, no ranking needed.
    if not query or not query.strip():
        rows = catalogue_mod.list_products(kb_root, category=category, max_price=max_price, query=None, limit=limit)
        return [{**r, "retrieval": "filter"} for r in rows]

    try:
        from kb import hybrid

        hits = hybrid.search_products(
            query,
            category=_canonical_category(kb_root, category),
            max_price=max_price,
            limit=min(limit, MAX_RANKED_PRODUCTS),
        )
    except Exception as e:
        _log(f"hybrid product search unavailable, using substring fallback ({type(e).__name__}: {str(e)[:200]})")
        return _products_fallback(kb_root, category, max_price, query, limit)

    by_code = {r["code"]: r for r in catalogue_mod._load_rows(kb_root)}
    results = []
    for h in hits:
        row = by_code.get(h.key.removeprefix("product:"))
        if row is None:
            _log(f"Atlas returned {h.key!r}, not in the bundled products.csv — re-run ingest.py")
            continue
        results.append({**row, "retrieval": "exact_code" if "exact" in h.ranks else "hybrid"})
    return results
