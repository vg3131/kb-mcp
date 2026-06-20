"""
Entry point for the Worx+ Knowledge Base MCP server.

Exposes four tools backed directly by the markdown tree in
worx-techtalk-markdown-kb/ — no database, no separate fetch step. search()
in particular returns full document bodies inline, so Claude gets the actual
.md content it needs to answer a question in one call.

Run locally:
    pip install -r requirements.txt
    python server.py

Deploy: see README.md (Railway, Root Directory = mcp-server/).
"""

import os

from mcp.server.fastmcp import FastMCP

from kb.index import build_index
from kb import search as search_mod
from kb import documents as documents_mod
from kb import browse as browse_mod
from kb import catalogue as catalogue_mod

# --- Configuration ----------------------------------------------------------

# Root of the markdown knowledge base. Defaults to worx-techtalk-markdown-kb/
# *inside* this folder (not a sibling) — Railway's Root Directory=mcp-server
# setting only pulls this subfolder into the deploy checkout, so the data has
# to live inside it too, or KB_ROOT silently resolves to a path that doesn't
# exist and the index ends up empty (every tool call "succeeds" with 0 results).
KB_ROOT = os.environ.get(
    "KB_ROOT",
    os.path.join(os.path.dirname(__file__), "worx-techtalk-markdown-kb"),
)

# 0.0.0.0 is required on Railway (and most PaaS) so the process accepts
# connections from the platform's reverse proxy, not just localhost.
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# Built once at startup. kb/index.py walks KB_ROOT and returns a list of
# DocEntry records (path, title, content_type, category, frontmatter, body)
# that search.py and browse.py both query against instead of re-walking the
# filesystem on every tool call.
DOC_INDEX = build_index(KB_ROOT)

# host/port are constructor args on FastMCP itself, not on .run() — verified
# against the installed SDK (mcp 1.28.0).
#
# Note: FastMCP only auto-enables DNS-rebinding / Host-header protection when
# host is "127.0.0.1"/"localhost"/"::1". Binding 0.0.0.0 (required for Railway)
# turns that off unless transport_security is set explicitly. Leaving it off
# for now, consistent with the no-auth decision — revisit together if that
# decision changes.
mcp = FastMCP("worx-techtalk-kb", host=HOST, port=PORT)


# --- Tools -------------------------------------------------------------------


@mcp.tool()
def search(query: str, content_type: str | None = None, category: str | None = None, limit: int = 5) -> list[dict]:
    """
    Keyword search across the knowledge base. Returns the FULL content of
    whichever documents look relevant — title, path, content_type, category,
    frontmatter, and the entire markdown body — so you can read and answer
    directly from the result, no follow-up get_document call needed.

    Args:
        query: 2-4 keyword terms work better than a full sentence
            (e.g. "densifier saturation" rather than "tell me about densifiers").
        content_type: optional filter, e.g. "knowledge_card", "application_guide",
            "process_guide", "machine_doc", "catalogue_category", "presenter_profile".
            consolidated/* mega-files (the whole knowledge base concatenated)
            are excluded unless you pass content_type="consolidated" explicitly.
        category: optional filter on the folder-derived category, e.g. "machines",
            "polishing", "business", "worxplus".
        limit: max number of documents to return. Kept small by default since
            each result is a full document body, not a snippet.
    """
    return search_mod.search(DOC_INDEX, query, content_type=content_type, category=category, limit=limit)


@mcp.tool()
def get_document(path: str) -> dict:
    """
    Return the full markdown content (including frontmatter) of one document.

    Args:
        path: a repo-relative path as returned by search() or list_by_category(),
            e.g. "knowledge-cards/densifiers/potassium-early-lithium-late.md".
    """
    return documents_mod.get_document(KB_ROOT, path)


@mcp.tool()
def list_by_category(content_type: str | None = None, category: str | None = None) -> list[dict]:
    """
    Browse the knowledge base by structure instead of searching — e.g. every
    knowledge card under "machines", or every process guide. Useful when the
    user names a topic area rather than a specific question.

    Args:
        content_type: e.g. "knowledge_card", "application_guide", "process_guide",
            "machine_doc", "catalogue_category", "presenter_profile". Omit to list all.
        category: folder-derived category, e.g. "machines", "polishing", "worxplus".
            Omit to list all categories for the given content_type.
    """
    return browse_mod.list_by_category(DOC_INDEX, content_type=content_type, category=category)


@mcp.tool()
def list_products(
    category: str | None = None,
    max_price: float | None = None,
    query: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Filter the product catalogue (867 SKUs from catalogue/products.csv) directly —
    no search index involved, just in-memory filtering of the CSV.

    Args:
        category: e.g. "Grinding Tools", "Sealer", "Vacuum Parts".
        max_price: only return products at or below this price.
        query: optional keyword match against the product description.
        limit: max rows to return (default 200). An unfiltered call would
            otherwise return all 867 rows, which is too large for one
            response — narrow with category/max_price/query first if you
            need rows outside the first `limit`.
    """
    return catalogue_mod.list_products(
        KB_ROOT, category=category, max_price=max_price, query=query, limit=limit
    )


# --- Run ----------------------------------------------------------------------

if __name__ == "__main__":
    # Streamable HTTP — not SSE. SSE is the older transport (mcp.run also
    # accepts transport="sse" for backwards compatibility, listening on
    # /sse + /messages/ instead of /mcp), but it's the one Anthropic's docs
    # describe as being phased out. Streamable HTTP is also what's required
    # for remote/custom connectors, since Claude connects from Anthropic's
    # cloud rather than from this machine — this has to be reachable over
    # public HTTPS once deployed (see README.md).
    mcp.run(transport="streamable-http")
