"""
Entry point for the Worx+ Knowledge Base MCP server.

Exposes four tools over the markdown tree in worx-techtalk-markdown-kb/.
search() and list_products(query=...) rank results with hybrid retrieval on
MongoDB Atlas (vector + BM25 keyword search, fused with RRF — see
kb/hybrid.py), then serve full bodies from the bundled markdown. If Atlas or
Voyage is unavailable they fall back to the original on-disk search
automatically (kb/retrieval.py). get_document and list_by_category read
straight from disk.

Run locally:
    pip install -r requirements.txt
    python server.py

Deploy: see README.md (Railway or Vercel, Root Directory = mcp-server/).
"""

import os

from mcp.server.fastmcp import FastMCP

from kb.config import load_env

# Local runs read mcp-server/.env; on Vercel this is a no-op and the
# project's Environment Variables are used instead.
load_env()

from kb.index import build_index  # noqa: E402
from kb import retrieval  # noqa: E402
from kb import documents as documents_mod  # noqa: E402
from kb import browse as browse_mod  # noqa: E402

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
#
# stateless_http=True: Vercel's Python runtime doesn't give session affinity
# between requests (invocations can land on different instances, or a fresh
# cold start), so the default session-based Streamable HTTP mode — which
# expects follow-up calls to hit the same server process — doesn't work
# there. Stateless mode re-derives everything from each request instead of
# relying on server-side session state. Harmless on Railway too, since this
# server has no session state to begin with (DOC_INDEX is read-only).
#
# json_response=True: return plain JSON instead of an SSE stream for each
# response. Simpler to serve correctly from a serverless function; not
# required by Vercel specifically, but pairs naturally with stateless_http.
mcp = FastMCP(
    "worx-techtalk-kb",
    host=HOST,
    port=PORT,
    stateless_http=True,
    json_response=True,
)

# ASGI app, used two different ways depending on where this runs:
#   - Vercel: server.py is one of the entrypoint filenames its Python
#     runtime auto-detects, and it looks for a module-level `app`. Vercel
#     serves this directly — mcp.run() below is never called there.
#   - Railway / local: unused. `python server.py` still goes through
#     mcp.run() at the bottom of this file, which builds its own copy of
#     this app internally and serves it with uvicorn.
app = mcp.streamable_http_app()


# --- Tools -------------------------------------------------------------------


@mcp.tool()
def search(query: str, content_type: str | None = None, category: str | None = None, limit: int = 5) -> list[dict]:
    """
    Search the Worx+ TechTalk knowledge base: knowledge cards, application
    guides, the polished-concrete process guide, GK-series machine docs and
    presenter profiles. Returns the FULL content of the best-matching
    documents (title, path, frontmatter and the entire markdown body), so you
    can answer directly from the result without calling get_document.

    Matches by meaning as well as by exact terms, so ask the way the user
    would: a full question or a description of the symptom works well
    ("floor looks cloudy after sealing", "why do stones pop out at resin
    stage"), as do model names and product names ("GK-850", "Tempo XD").

    This does NOT search the product catalogue. For prices, SKUs, product
    codes or "which product / how much", use list_products. For a question
    that needs both (e.g. which sealer for a humid site and what it costs),
    call both tools.

    Args:
        query: the question, symptom or term to search for.
        content_type: optional filter: "knowledge_card", "application_guide",
            "process_guide", "machine_doc", "presenter_profile". Other values
            (e.g. "consolidated", "product_category") search those on-disk
            files with plain keyword matching.
        category: optional filter on the folder-derived category, e.g.
            "machines", "polishing", "densifiers", "troubleshooting", "worxplus".
        limit: max documents to return (each is a full document, not a snippet).
    """
    return retrieval.search_documents(DOC_INDEX, query, content_type=content_type, category=category, limit=limit)


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
    Look up products in the Worx+ catalogue (867 SKUs): code, description,
    price (AUD), category and product page URL. Use this for anything about
    specific products, prices, SKUs or "what do you sell for X".

    With a query, results are ranked best match first and matched by meaning,
    so descriptive queries work ("HEPA filter for the 55T vacuum", "grinding
    accelerator for hard slabs", "potassium densifier 15 litre"). Product
    codes (format NNNN-NNNN, e.g. "0752-2901") anywhere in the query are
    matched exactly and always returned first. Ranked results are capped at
    the top 25.

    Without a query, it lists the catalogue filtered by category and/or price.

    Args:
        category: e.g. "Sealer", "Grinding Tools", "Vacuum Parts", "Densifier",
            "Respirators", "Leads", "Repair" (case-insensitive).
        max_price: only products at or below this price.
        query: what the user is looking for, in their words, or a product code.
        limit: max rows to return (default 200; the full unfiltered catalogue
            is too large for one response, so narrow with category/max_price).
    """
    return retrieval.list_products(KB_ROOT, category=category, max_price=max_price, query=query, limit=limit)


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
