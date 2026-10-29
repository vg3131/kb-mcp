"""
Pre-flight check for the Atlas + Voyage setup. Run from mcp-server/:

    python scripts/check_setup.py

Checks, in order:
  1. .env is present and has no leftover placeholders
  2. The Atlas cluster is reachable and the credentials work (ping)
  3. The user can write to MONGODB_DB (insert + delete a throwaway doc)
  4. VOYAGE_API_KEY works (one tiny embedding call), if it's set

Never prints secrets.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "..", ".env")


def load_env(path: str) -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments) so this script has no
    extra dependency. Doesn't override variables already set in the shell."""
    if not os.path.exists(path):
        sys.exit(f"✗ No .env found at {os.path.abspath(path)}")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ok(msg: str) -> None:
    print(f"✓ {msg}")


def fail(msg: str) -> None:
    sys.exit(f"✗ {msg}")


def main() -> None:
    load_env(ENV_PATH)

    # 1. Placeholders
    uri = os.environ.get("MONGODB_URI", "")
    db_name = os.environ.get("MONGODB_DB", "worx_kb")
    if not uri:
        fail("MONGODB_URI is empty in .env")
    if "USERNAME:PASSWORD" in uri or "<db_" in uri:
        fail("MONGODB_URI still has placeholder username/password — edit .env and save it")
    ok(".env loaded, MONGODB_URI filled in")

    # 2. Connect + ping
    try:
        from pymongo import MongoClient
        from pymongo.errors import OperationFailure, ServerSelectionTimeoutError
    except ImportError:
        fail("pymongo not installed — run: pip install -r requirements.txt")

    client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
    try:
        t0 = time.perf_counter()
        client.admin.command("ping")
        ms = (time.perf_counter() - t0) * 1000
    except OperationFailure as e:
        fail(f"Reached the cluster but authentication failed — check username/password in .env ({e.code})")
    except ServerSelectionTimeoutError:
        fail(
            "Couldn't reach the cluster in 10s. Most likely your IP isn't on the "
            "Atlas Network Access list (Security → Network Access → Add Current IP)."
        )
    info = client.server_info()
    ok(f"Connected to Atlas (MongoDB {info.get('version')}, first ping {ms:.0f} ms incl. connection setup)")

    # 3. Write test
    coll = client[db_name]["_setup_check"]
    try:
        res = coll.insert_one({"check": True})
        coll.delete_one({"_id": res.inserted_id})
        client[db_name].drop_collection("_setup_check")
        ok(f"Read/write OK on database '{db_name}'")
    except OperationFailure:
        print(f"! Connected, but this user can't write to '{db_name}'. "
              "Fine for the server's read-only user, but ingest needs read/write.")

    # 4. Voyage — through the same kb.embeddings code the ingest script uses
    if not os.environ.get("VOYAGE_API_KEY"):
        print("– VOYAGE_API_KEY not set yet — skipping embedding check")
        return
    sys.path.insert(0, os.path.join(HERE, ".."))
    from kb.embeddings import EMBEDDING_MODEL, embed_query
    try:
        v = embed_query("potassium early, lithium late")
        ok(f"Voyage embedding OK ({EMBEDDING_MODEL}, {len(v)} dimensions)")
    except Exception as e:  # auth / network / quota
        fail(f"Voyage call failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
