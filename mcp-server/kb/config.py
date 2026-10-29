"""
Settings shared by the server and the scripts.

Locally, values come from mcp-server/.env. On Vercel, they come from the
project's Environment Variables. load_env() never overrides a variable that's
already set, so platform-provided values always win over the file.
"""

import os

MCP_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(MCP_SERVER_DIR, ".env")


def load_env(path: str = DEFAULT_ENV_PATH) -> None:
    """Minimal KEY=VALUE .env loader (no python-dotenv dependency). Silently
    does nothing if the file doesn't exist, e.g. on Vercel."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mongodb_uri() -> str:
    uri = os.environ.get("MONGODB_URI", "")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set (mcp-server/.env locally, Environment Variables on Vercel)")
    return uri


def mongodb_db() -> str:
    return os.environ.get("MONGODB_DB", "worx_kb")


def voyage_api_key() -> str:
    key = os.environ.get("VOYAGE_API_KEY", "")
    if not key:
        raise RuntimeError("VOYAGE_API_KEY is not set")
    return key


# Name of the one collection everything lives in (markdown chunks + product
# rows). One collection = one vector index + one keyword index, which keeps us
# inside the free M0 tier's limit of 3 search indexes.
CHUNKS_COLLECTION = "chunks"
