"""
Hybrid retrieval over the Atlas `chunks` collection.

Three layers, in order of authority:

  1. exact     A plain MongoDB query (ordinary index on `code`, no relevance
               scoring) for product codes that appear verbatim in the query.
               Deterministic: if someone types a real SKU, that product is
               pinned first, guaranteed — it never goes through fusion, where
               look-alike products could outvote it.
  2. vector    $vectorSearch on Voyage embeddings — finds matches by meaning
               ("floor looks cloudy after sealing" -> "sealer streaking / white haze")
  3. keyword   $search (BM25, English stemming) — finds exact terms the
               embedding is fuzzy on ("GK-850", "Tempo XD", "Redi Loc")

Layers 2 and 3 are merged with Reciprocal Rank Fusion (RRF), in Python rather
than Atlas's $rankFusion (not available on our MongoDB 8.0 cluster). RRF only
looks at each result's *position* in each list, so raw scores never need to
be comparable:

    fused(doc) = sum over lists of  weight / (RRF_K + rank_in_that_list)

Matching happens per chunk, but results are rolled up to the parent document
(doc_path) — or the product row — because whole documents are what Claude
reads best. A document's rank in a list is the rank of its best chunk.
"""

from dataclasses import dataclass, field

from kb.atlas import KEYWORD_INDEX_NAME, VECTOR_INDEX_NAME, chunks_collection
from kb.catalogue import PRODUCT_CODE, is_code_lookup
from kb.embeddings import embed_query

# Fusion settings, chosen with scripts/eval_search.py --sweep (34 questions).
# The textbook k=60 made "appears in both lists, mediocre in each" beat "1st
# in the vector list" — paraphrase hit@5 fell to 77%. k=5 rewards top ranks;
# every top-12 sweep config used it. Keyword weight 0.7 was the best balance:
# 0.5 scored slightly higher on paraphrases but dropped bare-SKU lookups out
# of the top 5. Keyword depth 5/10/20 scored identically (a stable plateau),
# so 10 sits in the middle of it.
#   old default  k=60, kw 1.0, depth 40 -> hit@5 82%, MRR 0.76
#   tuned        k=5,  kw 0.7, depth 10 -> hit@5 97%, MRR 0.85
RRF_K = 5
DEFAULT_WEIGHTS = {"vector": 1.0, "keyword": 0.7}
DEFAULT_DEPTH = {"keyword": 10}

CANDIDATES_PER_RETRIEVER = 40  # chunks pulled from each side before fusing
VECTOR_NUM_CANDIDATES = 200  # HNSW search breadth (>= 10x limit is the usual guidance)

_PROJECTION = {"_id": 1, "doc_path": 1, "content_type": 1, "title": 1}


@dataclass
class Hit:
    key: str  # doc_path for markdown, "product:<code>" for products
    content_type: str
    title: str
    score: float = 0.0
    ranks: dict = field(default_factory=dict)  # {"vector": 3, "keyword": 1} or {"exact": 1}


def _doc_key(chunk: dict) -> str:
    return chunk.get("doc_path") or chunk["_id"]


# --- Layer 1: deterministic exact match -------------------------------------


def exact_products(query: str, max_price: float | None = None) -> list[dict]:
    """Products whose code appears verbatim in the query. Plain indexed find —
    no scoring, no embedding call. Returned in the order the codes appear."""
    codes = list(dict.fromkeys(PRODUCT_CODE.findall(query)))
    if not codes:
        return []
    flt: dict = {"content_type": "product", "code": {"$in": codes}}
    if max_price is not None:
        flt["price"] = {"$lte": max_price}
    found = {d["code"]: d for d in chunks_collection().find(flt, {**_PROJECTION, "code": 1})}
    return [found[c] for c in codes if c in found]


def pin(hits: list["Hit"], exact: list[dict]) -> list["Hit"]:
    """Put exact matches first, then the fused results minus duplicates."""
    if not exact:
        return hits
    pinned = [
        Hit(key=d["_id"], content_type=d.get("content_type", "product"), title=d.get("title", ""), ranks={"exact": i})
        for i, d in enumerate(exact, 1)
    ]
    keys = {h.key for h in pinned}
    return pinned + [h for h in hits if h.key not in keys]


# --- Layers 2 + 3: vector and keyword retrieval -----------------------------


def _vector_filter(content_type, category, include_products, max_price) -> dict | None:
    clauses = []
    if content_type:
        clauses.append({"content_type": {"$eq": content_type}})
    elif not include_products:
        clauses.append({"content_type": {"$ne": "product"}})
    if category:
        clauses.append({"category": {"$eq": category}})
    if max_price is not None:
        clauses.append({"price": {"$lte": max_price}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def vector_ranked(
    query_vector: list[float],
    content_type: str | None = None,
    category: str | None = None,
    include_products: bool = True,
    max_price: float | None = None,
    limit: int = CANDIDATES_PER_RETRIEVER,
) -> list[dict]:
    stage = {
        "index": VECTOR_INDEX_NAME,
        "path": "embedding",
        "queryVector": query_vector,
        "numCandidates": max(VECTOR_NUM_CANDIDATES, limit * 5),
        "limit": limit,
    }
    flt = _vector_filter(content_type, category, include_products, max_price)
    if flt:
        stage["filter"] = flt
    pipeline = [
        {"$vectorSearch": stage},
        {"$project": {**_PROJECTION, "score": {"$meta": "vectorSearchScore"}}},
    ]
    return list(chunks_collection().aggregate(pipeline))


def keyword_ranked(
    query: str,
    content_type: str | None = None,
    category: str | None = None,
    include_products: bool = True,
    max_price: float | None = None,
    limit: int = CANDIDATES_PER_RETRIEVER,
) -> list[dict]:
    tokens = query.split()
    should = [
        {"text": {"query": query, "path": "title", "score": {"boost": {"value": 3}}}},
        {"text": {"query": query, "path": "heading", "score": {"boost": {"value": 2}}}},
        {"text": {"query": query, "path": "text"}},
        # code uses the keyword analyzer: each whitespace token is tried as a
        # whole SKU, so "price of 0131-0122" still hits product 0131-0122.
        {"text": {"query": tokens, "path": "code", "score": {"boost": {"value": 10}}}},
    ]
    compound: dict = {"should": should, "minimumShouldMatch": 1}
    filters, must_not = [], []
    if content_type:
        filters.append({"equals": {"path": "content_type", "value": content_type}})
    elif not include_products:
        must_not.append({"equals": {"path": "content_type", "value": "product"}})
    if category:
        filters.append({"equals": {"path": "category", "value": category}})
    if max_price is not None:
        filters.append({"range": {"path": "price", "lte": max_price}})
    if filters:
        compound["filter"] = filters
    if must_not:
        compound["mustNot"] = must_not

    pipeline = [
        {"$search": {"index": KEYWORD_INDEX_NAME, "compound": compound}},
        {"$limit": limit},
        {"$project": {**_PROJECTION, "score": {"$meta": "searchScore"}}},
    ]
    return list(chunks_collection().aggregate(pipeline))


# --- Fusion -----------------------------------------------------------------


def fuse(
    ranked_lists: dict[str, list[dict]],
    weights: dict[str, float] | None = None,
    k: int = RRF_K,
    depth: dict[str, int] | None = None,
) -> list[Hit]:
    """
    Reciprocal Rank Fusion at document level.

    weights: per-list multiplier, e.g. {"keyword": 0.5} to trust BM25 less.
    k:       RRF constant. Smaller = top positions matter more relative to
             simply appearing in both lists.
    depth:   per-list cutoff — only a list's top-N documents vote, e.g.
             {"keyword": 10} so BM25's long tail of one-word matches can't
             outvote a strong vector hit.
    """
    weights = DEFAULT_WEIGHTS if weights is None else weights
    depth = DEFAULT_DEPTH if depth is None else depth
    hits: dict[str, Hit] = {}
    for name, chunks in ranked_lists.items():
        w = weights.get(name, 1.0)
        seen: set[str] = set()
        rank = 0
        for chunk in chunks:
            key = _doc_key(chunk)
            if key in seen:  # only a document's best chunk counts
                continue
            seen.add(key)
            rank += 1
            if name in depth and rank > depth[name]:
                break
            hit = hits.setdefault(key, Hit(key=key, content_type=chunk.get("content_type", ""), title=chunk.get("title", "")))
            hit.score += w / (k + rank)
            hit.ranks[name] = rank
    return sorted(hits.values(), key=lambda h: -h.score)


def cap_products(hits: list[Hit], max_products: int | None) -> list[Hit]:
    """Keep at most max_products product rows, preserving order."""
    if max_products is None:
        return hits
    out, n = [], 0
    for h in hits:
        if h.content_type == "product":
            if n >= max_products:
                continue
            n += 1
        out.append(h)
    return out


# --- Entry points -------------------------------------------------------------


def search_documents(
    query: str,
    content_type: str | None = None,
    category: str | None = None,
    limit: int = 5,
    query_vector: list[float] | None = None,
) -> list[Hit]:
    """Knowledge documents only (cards, guides, machine docs, presenters) —
    product rows are excluded so they can't crowd out answers."""
    if query_vector is None:
        query_vector = embed_query(query)
    lists = {
        "vector": vector_ranked(query_vector, content_type, category, include_products=False),
        "keyword": keyword_ranked(query, content_type, category, include_products=False),
    }
    return fuse(lists)[:limit]


def search_products(
    query: str,
    category: str | None = None,
    max_price: float | None = None,
    limit: int = 10,
    query_vector: list[float] | None = None,
) -> list[Hit]:
    """Catalogue products only: exact SKU matches pinned first, then hybrid
    results, with optional category / max_price filters applied in Atlas."""
    exact = exact_products(query, max_price)
    if exact and is_code_lookup(query):
        # "0752-2901" / "how much is 0752-2901": the exact match is the
        # answer. Skips the embedding call too, so it's instant and works
        # even when Voyage is down.
        return pin([], exact)[:limit]
    if query_vector is None:
        query_vector = embed_query(query)
    lists = {
        "vector": vector_ranked(query_vector, "product", category, max_price=max_price),
        "keyword": keyword_ranked(query, "product", category, max_price=max_price),
    }
    return pin(fuse(lists), exact)[:limit]


def hybrid_search(
    query: str,
    content_type: str | None = None,
    category: str | None = None,
    include_products: bool = True,
    limit: int = 5,
    query_vector: list[float] | None = None,
    weights: dict[str, float] | None = None,
    max_products: int | None = None,
    pin_exact: bool = True,
) -> list[Hit]:
    """Single-tool variant: documents and products together, with exact SKU
    pinning and an optional cap on product rows."""
    if query_vector is None:
        query_vector = embed_query(query)
    lists = {
        "vector": vector_ranked(query_vector, content_type, category, include_products),
        "keyword": keyword_ranked(query, content_type, category, include_products),
    }
    hits = cap_products(fuse(lists, weights), max_products)
    if pin_exact and include_products:
        hits = pin(hits, exact_products(query))
    return hits[:limit]
