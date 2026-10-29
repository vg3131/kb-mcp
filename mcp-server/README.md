# Worx+ Knowledge Base — MCP Server

A remote MCP server that gives Claude access to the Worx+ TechTalk knowledge
base for the concrete polishing industry: 219 knowledge cards, application and
process guides, GK-series machine docs, presenter profiles, and an 867-SKU
product catalogue.

Search uses **hybrid retrieval on MongoDB Atlas**: semantic vector search
(Voyage AI embeddings) plus BM25 keyword search, merged with Reciprocal Rank
Fusion, with exact product codes matched deterministically. The markdown in
`worx-techtalk-markdown-kb/` stays the single source of truth; Atlas is a
derived index that `scripts/ingest.py` can always rebuild.

Deployed on Vercel (stateless Streamable HTTP). If Atlas or Voyage is
unreachable, the tools fall back automatically to the original on-disk
keyword search, so they degrade instead of failing.

## Results

Retrieval was measured on 43 test questions (`eval/questions.jsonl`) before
and after the migration. "hit@5" = the right document is in the top 5 results
(what `search` returns by default); MRR = how close to first place it ranks
(1.0 = always first).

| Retriever | hit@5 | MRR |
|---|---|---|
| Original keyword scorer | 30% | 0.27 |
| Vector search only | 93% | 0.83 |
| Hybrid, one tool for docs + products | 95% | 0.83 |
| **Hybrid, separate doc / product search (shipped)** | **100%** | **0.89** |

By question type (shipped configuration vs original):

| Question type | n | Original hit@5 | Shipped hit@5 | Shipped MRR |
|---|---|---|---|---|
| Paraphrased ("floor looks cloudy after sealing") | 22 | 0% | 100% | 0.83 |
| Exact terms ("GK-850", "Tempo XD", "Redi Loc") | 9 | 89% | 100% | 1.00 |
| Products and SKUs ("HEPA filter for the 55T vacuum") | 12 | 42% | 100% | 0.90 |

The original scorer counted query-word substrings, so it could not connect a
contractor's description of a symptom to the card that answers it. Reproduce
with `python scripts/eval_search.py`.

## Architecture

```
                         ┌───────────────────────────── query time ─┐
User ─► Claude ─► MCP server (Vercel, us-east-1)                     │
                    │                                                │
                    ├─ SKU in query? ──► plain indexed find ── pinned 1st
                    ├─ Voyage embed ──► Atlas $vectorSearch  ┐       │
                    └─ query text ────► Atlas $search (BM25) ┴► RRF  │
                                                               │     │
                    full bodies from bundled markdown ◄────────┘     │
                         └───────────────────────────────────────────┘

markdown + products.csv ─► scripts/ingest.py ─► Voyage ─► Atlas `chunks`
        (source of truth)    (chunk, hash)      (embed)   (+ 2 search indexes)
                                               └──────── offline, on change ─┘
```

Atlas decides **which** documents are relevant; the document bodies Claude
receives still come from the markdown bundled with the deployment, so the
response shape is unchanged from the pre-Atlas server.

## Tools

| Tool | What it does |
|---|---|
| `search(query, content_type=None, category=None, limit=5)` | Hybrid search over **knowledge documents only** (cards, guides, machine docs, presenters). Returns full documents, not snippets. Accepts natural-language questions. Product rows are deliberately excluded so they can't crowd out answers. Content types that aren't in Atlas (e.g. `consolidated`) are searched on disk. |
| `list_products(category=None, max_price=None, query=None, limit=200)` | With a `query`: hybrid search over the catalogue, top 25, best match first. Product codes (`NNNN-NNNN`) in the query are matched exactly and pinned first; a query that is just a code returns only that product (no embedding call). Without a query: plain category / price filter. |
| `get_document(path)` | One document by path, read from disk. |
| `list_by_category(content_type=None, category=None)` | Browse document titles and paths by type/category, read from disk. |

Every `search` / `list_products` result carries a `retrieval` field —
`hybrid`, `exact_code`, `filter`, `keyword`, or `*_fallback` — showing which
path answered.

## How retrieval works

**Chunking** (`kb/chunking.py`). Built around the real corpus rather than a
generic chunk size: knowledge cards (~1 KB), application guides (~2 KB),
machine docs and presenter profiles are each already one answer, so each is
**one chunk**. Documents over 4,000 characters (only the process guide) are
split on `##` headings, with the document title prefixed to each section.
Each of the 867 catalogue rows is its own chunk. Attribution boilerplate
(`## Source`, `## Category`, URLs) is stored but not embedded, so vectors
reflect the actual advice. Skipped: `consolidated/*` (duplicates), index
pages, catalogue table pages (duplicates of `products.csv`), the video index.
Result: **1,130 chunks**.

**Two retrievers, one collection** (`kb/hybrid.py`, `kb/atlas.py`). Every
chunk lives in one Atlas collection with two search indexes, inside the free
M0 tier's limit of three:

- `chunks_vector` — 1024-dim `voyage-3.5` embeddings, cosine, with filter
  fields for `content_type`, `category` and `price`.
- `chunks_keyword` — BM25 with English stemming on title / heading / text,
  and a keyword-analyzed `code` field so SKUs match as whole tokens.

**Fusion.** Reciprocal Rank Fusion, done in Python (Atlas's `$rankFusion`
isn't available on the MongoDB 8.0 cluster). Settings were chosen with a grid
search (`eval_search.py --sweep`) rather than defaults:

| Setting | Value | Why |
|---|---|---|
| RRF `k` | 5 | The textbook `k=60` let documents that were mediocre in *both* lists outrank the vector search's 1st place; paraphrase hit@5 fell to 77%. All of the top 12 sweep configurations used `k=5`. |
| Keyword weight | 0.7 | 0.5 scored slightly higher on paraphrases but pushed bare-SKU lookups out of the top 5. |
| Keyword depth | top 10 | 5, 10 and 20 scored identically, a stable plateau; 10 is its middle. Stops BM25's long tail of one-word matches from outvoting a strong vector hit. |

**Deterministic exact match.** A product code in the query is looked up with
an ordinary indexed `find` (no relevance scoring) and pinned to first place,
so look-alike products can never outvote it in fusion.

**Separate document and product search.** Evaluated against a single
combined search (with and without capping product rows): combined search let
products fill 19% of top-5 slots on non-product questions, and capping
products dropped legitimate product answers. Splitting them was the only
configuration with 100% hit@5, and the tool descriptions tell Claude which to
use (or to call both).

**Fallback** (`kb/retrieval.py`). Query-time embedding calls time out after
~8 s with one retry; Atlas server selection times out after 5 s. Any failure
logs the reason to stderr and serves the request from the original on-disk
search, including offline exact-SKU matching from `products.csv`.

## Layout

```
mcp-server/
├── server.py              MCP tool definitions; Streamable HTTP entry point
├── kb/
│   ├── retrieval.py       What the tools call: Atlas first, on-disk fallback
│   ├── hybrid.py          Vector + keyword retrieval, RRF fusion, SKU pinning
│   ├── atlas.py           Cached MongoClient, search index definitions
│   ├── embeddings.py      Voyage API over plain HTTP (httpx), batching, retries
│   ├── chunking.py        Markdown + CSV -> retrieval chunks
│   ├── config.py          Settings; minimal .env loader
│   ├── index.py           In-memory index of the markdown tree (disk)
│   ├── search.py          Original keyword scorer (fallback + eval baseline)
│   ├── documents.py       get_document
│   ├── browse.py          list_by_category
│   └── catalogue.py       products.csv loading, filters, exact code lookup
├── scripts/
│   ├── check_setup.py     Verifies .env, Atlas connection, write access, Voyage
│   ├── ingest.py          Syncs markdown + catalogue into Atlas
│   ├── eval_search.py     Retrieval evaluation (+ --sweep for fusion tuning)
│   └── smoke_server.py    Calls the tools as the server does
├── eval/questions.jsonl   43 labelled test questions
├── worx-techtalk-markdown-kb/   The corpus (source of truth)
├── requirements.txt       Pinned: mcp, python-frontmatter, uvicorn, pymongo
├── vercel.json            Vercel function config (maxDuration)
└── .env.example
```

## Setup

### 1. Atlas and Voyage

- MongoDB Atlas cluster (the free M0 tier is enough), ideally in the same
  region as the server (`us-east-1` for Vercel's default `iad1`). Claude
  calls the connector from Anthropic's US infrastructure, so co-locating the
  server and database matters more than where end users are.
- Two database users: a **read-write** user for `ingest.py` (local only) and
  a **read-only** user for the deployed server.
- Network Access: your IP for ingesting; `0.0.0.0/0` for Vercel (no fixed
  IPs — credentials and the read-only role are the protection).
- A Voyage AI API key. Add a payment method for production use: the
  unbilled tier is limited to ~3 requests/minute, and every search makes one
  embedding call. Ingesting this corpus is ~72K tokens.

### 2. Local environment

```bash
cd mcp-server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in the values below
python scripts/check_setup.py
```

`.env` (gitignored — never commit it):

```
MONGODB_URI=mongodb+srv://<read-write user>:<password>@<cluster>/?retryWrites=true&w=majority&appName=kb-mcp
MONGODB_DB=kb-mcp
VOYAGE_API_KEY=...
```

### 3. Ingest

```bash
python scripts/ingest.py --dry-run   # chunk and report, no network calls
python scripts/ingest.py             # embed, upsert, create indexes
python scripts/ingest.py --throttle  # Voyage account without billing
```

Re-run after **any** change to the markdown or `products.csv`. It is
incremental and idempotent: each chunk stores a hash of its embedded text and
the model name, so unchanged chunks cost nothing; changed chunks are
re-embedded; chunks whose source disappeared are deleted. Each batch is saved
as it completes, so an interrupted run resumes where it stopped. `--force`
re-embeds everything (needed after changing the embedding model).

The deployed server only reads from Atlas, and it serves bodies from the
markdown in its own deployment. Deploy and ingest from the same commit; if
they drift, the server logs and skips documents it doesn't have.

### 4. Evaluate

```bash
python scripts/eval_search.py                 # per-question ranks + metrics
python scripts/eval_search.py --show-misses   # what came back on misses
python scripts/eval_search.py --sweep         # grid-search fusion settings
```

Query embeddings are cached in `eval/.query_cache.json` (gitignored), so
re-runs make no Voyage calls; only new or reworded questions are embedded.
Search results are always recomputed live against Atlas. Add a line to
`eval/questions.jsonl` whenever a real query misses.

## Running locally

```bash
python server.py                 # Streamable HTTP on :8000/mcp
python scripts/smoke_server.py   # exercise the tools directly
```

## Deploying (Vercel)

`server.py` exposes a module-level ASGI `app` with `stateless_http=True` and
`json_response=True`, which Vercel's Python runtime serves directly (no
session affinity between invocations). The Mongo client is created once per
instance and reused across warm invocations.

1. Import the repo in Vercel with **Root Directory** = `mcp-server`. The
   corpus must live inside that folder, since only the Root Directory is
   deployed.
2. Settings → Environment Variables (Production and Preview):
   - `MONGODB_URI`: the **read-only** user's connection string
   - `MONGODB_DB`: `kb-mcp`
   - `VOYAGE_API_KEY`
3. Push. Environment variable changes apply on the next deployment.
4. Add `https://<project>.vercel.app/mcp` as a custom connector in Claude.
   After changing tool descriptions, start a new chat (or reconnect the
   connector) so Claude reloads them.

## Auth

None at the MCP layer, as before: the content is public TechTalk material
and a public catalogue, and the URL is the only gate. Database access uses a
read-only user, so a leaked deployment credential cannot modify data. A
shared-secret header check in `server.py` is the easy next step if needed.

## Design notes

- **Why not the `voyageai` SDK?** It pulls in a large dependency tree that
  would bloat the Vercel bundle; one HTTPS endpoint via `httpx` (already a
  dependency of `mcp`) is enough.
- **Why synchronous PyMongo?** The tools are synchronous and FastMCP runs
  them in worker threads. A module-level sync client is safe to reuse across
  serverless invocations; an async client is bound to the event loop it was
  created on. Revisit if this becomes an async FastAPI backend.
- **Why return whole documents instead of chunks?** The corpus is mostly
  answer-sized cards, and Claude answers better from a complete card than
  from a fragment. Matching is per chunk; results roll up to the parent
  document by its best chunk.
- **Moving to a custom frontend later.** Retrieval lives in `kb/` with no MCP
  imports, so a FastAPI + web UI with its own agent loop can call
  `kb.retrieval` directly. The embeddings in Atlas don't depend on the client.
