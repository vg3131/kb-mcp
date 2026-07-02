# Worx+ Knowledge Base — MCP Server

A remote MCP server that gives Claude direct access to the `worx-techtalk-markdown-kb`
markdown tree (now nested inside this folder: `mcp-server/worx-techtalk-markdown-kb`).
No database — every tool reads the markdown files (and `catalogue/products.csv`)
directly off disk.

Implemented, smoke-tested against the real data (329 indexed documents, 867
catalogue rows), and deployed to Railway. Also deployable to Vercel — see below.

**Note on the nested data folder:** `worx-techtalk-markdown-kb/` lives *inside*
`mcp-server/` rather than as a sibling, even though that means it's no longer next
to `mkdocs.yml` at the repo root. This isn't cosmetic — Railway's Root
Directory=`mcp-server` setting only pulls that subfolder into the deploy checkout,
so a sibling folder silently doesn't exist in the deployed container (the index
builds with 0 documents and every tool call "succeeds" with empty results, no
error). Keeping the data inside the isolated subtree is what makes the deploy
actually work. `mkdocs.yml`'s `docs_dir` was updated to point at the new path.

## Layout

```
mcp-server/
├── server.py            Entry point. Defines the MCP tools and starts the
│                         Streamable HTTP server.
├── kb/
│   ├── index.py          Walks the markdown tree once at startup, builds an
│   │                      in-memory list of every document (path, frontmatter,
│   │                      title, category derived from folder structure).
│   ├── search.py          search tool — keyword search over the in-memory index.
│   ├── documents.py       get_document tool — reads one file by path and returns
│   │                      its content.
│   ├── browse.py          list_by_category tool — lists documents by folder /
│   │                      content type, no search needed.
│   └── catalogue.py       list_products tool — reads products.csv directly,
│                          filters in memory (category, max price, keyword).
├── worx-techtalk-markdown-kb/   The markdown corpus itself (moved here from the
│                                 repo root so it deploys with Root Directory isolation).
├── requirements.txt
├── railway.json          Railway start command.
├── vercel.json           Vercel function config (maxDuration).
└── .env.example
```

## Why no database

The earlier version of this project had a SQLite layer built from the markdown via
`build.py`. It was dropped: the whole corpus is ~329 files / ~700KB, small enough
that reading files directly is simpler and removes a second source of truth that
needed rebuilding every time the markdown changed. The one thing the database did
that file structure doesn't — a cross-reference graph between content items, people,
and products — isn't implemented here yet. If it turns out to matter, the right fix
is a small precomputed JSON file built by the same kind of script `build.py` was,
not bringing a live database back.

## Tools

| Tool | Purpose |
| --- | --- |
| `search(query, content_type=None, category=None, limit=5)` | Keyword search across titles/body/frontmatter. Returns the **full document** (frontmatter + body) for each match, not a snippet — so Claude can read and answer directly without a follow-up call. `consolidated/*` mega-files are excluded by default (they're the whole KB concatenated into one giant file and would crowd out specific results); pass `content_type="consolidated"` to fetch one on purpose. A defensive ~100K-char total-body budget caps how much a single call can return. |
| `get_document(path)` | Returns the full markdown content (with frontmatter) for one file, given a path returned by `search` or `list_by_category`. |
| `list_by_category(content_type=None, category=None)` | Browse without searching — e.g. "everything in knowledge-cards/machines" or "all process guides." Lightweight rows (no body). |
| `list_products(category=None, max_price=None, query=None, limit=200)` | Filters the 867-row product catalogue (`catalogue/products.csv`) directly, no search index needed. Capped at 200 rows by default — narrow with the filters to see more specific results. |

## Auth

None. Running authless per the decision to keep this simple for personal use —
the data isn't sensitive, but note that on a public Railway/Vercel URL, the URL
itself is the only thing gating access. Easy to add a shared-secret header check
in `server.py` later if that changes.

## Running locally

```bash
cd mcp-server
pip install -r requirements.txt
cp .env.example .env   # adjust KB_ROOT if needed
python server.py
```

## Deploying to Railway

1. Push this repo to GitHub.
2. In Railway, create a new service from the repo, and set **Root Directory** to
   `mcp-server`. Railway only pulls files from that subfolder into the deploy
   checkout — this is why `worx-techtalk-markdown-kb/` lives inside `mcp-server/`
   rather than next to it; a sibling folder would never make it into the container.
3. Set **Config File Path** (same Settings → Source section) to
   `mcp-server/railway.json` if it isn't picked up automatically — Railway looks
   for the config file relative to the repo root regardless of Root Directory.
4. Railway auto-detects Python from `requirements.txt`; `railway.json` pins the
   start command explicitly so it doesn't have to guess.
5. Railway injects `PORT` automatically — `server.py` reads it via `os.environ`.
6. Once deployed, generate a public domain (Settings → Networking → Generate
   Domain) and add `https://<service>.up.railway.app/mcp` (note the `/mcp` path)
   as a custom connector in Claude.

## Deploying to Vercel

Works because this server has no session state and no database — `server.py`
exposes a module-level `app = mcp.streamable_http_app()` (an ASGI app) with
`stateless_http=True`, which is what Vercel's Python runtime needs, since it
doesn't guarantee repeat requests land on the same instance. `python server.py`
still works locally/on Railway too — that path is unaffected, `mcp.run()` just
builds its own copy of the same app internally.

1. Push this repo to GitHub (or deploy straight from the CLI without git —
   see step 4).
2. In the Vercel dashboard: **Add New → Project**, import the repo, and set
   **Root Directory** to `mcp-server` (Settings → General during import). Same
   reason as Railway: only that subfolder — including
   `worx-techtalk-markdown-kb/` — needs to ship, and Vercel bundles everything
   reachable under the Root Directory by default.
3. Framework Preset: Vercel auto-detects Python from `requirements.txt` and
   picks `server.py` up as the entrypoint (it's one of the filenames the
   Python runtime scans for: `app.py`, `index.py`, `server.py`, `main.py`,
   `wsgi.py`, `asgi.py`). No build command needed.
4. Or skip the dashboard entirely with the CLI:
   ```bash
   npm i -g vercel
   cd mcp-server
   vercel login
   vercel        # links/creates the project, deploys a preview
   vercel --prod # promotes to production
   ```
   Run from inside `mcp-server/`, the CLI treats that directory as project
   root — no separate Root Directory setting needed in this path.
5. No environment variables required. `KB_ROOT` already resolves relative to
   `server.py`'s own location; `HOST`/`PORT` are unused by Vercel (it doesn't
   call `mcp.run()`).
6. Once deployed, add `https://<project>.vercel.app/mcp` (same `/mcp` path,
   Vercel serves the whole app at the project root) as a custom connector in
   Claude.

Smoke-tested locally by importing `server.app` into a Starlette `TestClient`
and sending an `initialize` + `tools/list` call through it (bypassing
`mcp.run()`/uvicorn entirely, the same way Vercel would invoke it) — both
returned 200 with the expected JSON-RPC bodies.
