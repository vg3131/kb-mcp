"""
MongoDB Atlas access: one cached client + the two search index definitions.

The client is created once per process and reused. On Vercel that means warm
invocations skip the (slow) DNS + TLS + auth handshake entirely.
"""

import time

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.operations import SearchIndexModel

from kb.config import CHUNKS_COLLECTION, mongodb_db, mongodb_uri
from kb.embeddings import EMBEDDING_DIMENSIONS

VECTOR_INDEX_NAME = "chunks_vector"
KEYWORD_INDEX_NAME = "chunks_keyword"

# Semantic side. Filter fields let $vectorSearch pre-filter by the same
# content_type / category arguments the search tool already exposes, plus
# price for product questions. Voyage vectors are normalised, so cosine.
VECTOR_INDEX_DEFINITION = {
    "fields": [
        {"type": "vector", "path": "embedding", "numDimensions": EMBEDDING_DIMENSIONS, "similarity": "cosine"},
        {"type": "filter", "path": "content_type"},
        {"type": "filter", "path": "category"},
        {"type": "filter", "path": "price"},
    ]
}

# Keyword (BM25) side — the part that keeps exact terms like "GK-850",
# "Tempo XD" or SKU "0131-0122" reliable. lucene.english adds stemming
# (grinding -> grind) on top of the old exact-substring behaviour. Product
# codes use the keyword analyzer so a code matches as one exact token.
KEYWORD_INDEX_DEFINITION = {
    "mappings": {
        "dynamic": False,
        "fields": {
            "title": {"type": "string", "analyzer": "lucene.english"},
            "heading": {"type": "string", "analyzer": "lucene.english"},
            "text": {"type": "string", "analyzer": "lucene.english"},
            "code": {"type": "string", "analyzer": "lucene.keyword"},
            "content_type": {"type": "token"},
            "category": {"type": "token"},
            "price": {"type": "number"},
        },
    }
}

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        # 5s: long enough for a cold connection from Vercel to an in-region
        # cluster, short enough that a tool call falls back promptly if
        # Atlas is unreachable.
        _client = MongoClient(mongodb_uri(), appname="kb-mcp", serverSelectionTimeoutMS=5_000)
    return _client


def chunks_collection() -> Collection:
    return get_client()[mongodb_db()][CHUNKS_COLLECTION]


def ensure_search_indexes(coll: Collection) -> None:
    """Create both indexes if missing; update them if the definition changed."""
    existing = {ix["name"]: ix for ix in coll.list_search_indexes()}
    wanted = [
        (VECTOR_INDEX_NAME, "vectorSearch", VECTOR_INDEX_DEFINITION),
        (KEYWORD_INDEX_NAME, "search", KEYWORD_INDEX_DEFINITION),
    ]
    for name, kind, definition in wanted:
        if name not in existing:
            coll.create_search_index(SearchIndexModel(definition=definition, name=name, type=kind))
            print(f"  created {kind} index '{name}'")
        elif existing[name].get("latestDefinition") != definition:
            coll.update_search_index(name, definition)
            print(f"  updated {kind} index '{name}'")
        else:
            print(f"  {kind} index '{name}' already up to date")


def ensure_regular_indexes(coll: Collection) -> None:
    """Ordinary (non-search) indexes. These don't count toward the M0 tier's
    3 search-index limit. `code` backs the deterministic exact-SKU lookup."""
    coll.create_index("code", name="code_1", sparse=True)
    coll.create_index("doc_path", name="doc_path_1", sparse=True)
    print("  regular indexes on code, doc_path ensured")


def wait_until_queryable(coll: Collection, timeout_s: int = 300) -> bool:
    names = {VECTOR_INDEX_NAME, KEYWORD_INDEX_NAME}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = {ix["name"]: (ix.get("status"), ix.get("queryable")) for ix in coll.list_search_indexes()}
        if all(status.get(n, (None, False))[1] for n in names):
            return True
        print("  waiting for indexes: " + ", ".join(f"{n}={status.get(n, ('missing',))[0]}" for n in sorted(names)))
        time.sleep(10)
    return False
