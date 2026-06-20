# Worx+ Knowledge Base — MCP Server

A remote MCP server that gives Claude direct access to the `worx-techtalk-markdown-kb`
markdown tree (one level up: `../worx-techtalk-markdown-kb`). No database — every tool
reads the markdown files (and `catalogue/products.csv`) directly off disk.

Implemented and smoke-tested against the real data (330 indexed documents, 867
catalogue rows). Not yet deployed to Railway.

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
├── requirements.txt
├── railway.json          Railway start command.
└── .env.example
```

## Why no database

The earlier version of this project had a SQLite layer built from the markdown via
`build.py`. It was dropped: the whole corpus is ~330 files / ~700KB, small enough
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
the data isn't sensitive, but note that on a public Railway URL, the URL itself
is the only thing gating access. Easy to add a shared-secret header check in
`server.py` later if that changes.

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
   `mcp-server` (so Railway only builds/runs this subfolder, while `KB_ROOT` still
   reaches the markdown tree via the relative `../worx-techtalk-markdown-kb` path
   inside the checked-out repo).
3. Railway auto-detects Python from `requirements.txt`; `railway.json` pins the
   start command explicitly so it doesn't have to guess.
4. Railway injects `PORT` automatically — `server.py` reads it via `os.environ`.
5. Once deployed, add the Railway URL (`https://<service>.up.railway.app/mcp` or
   whatever path the SDK mounts) as a custom connector in Claude.
