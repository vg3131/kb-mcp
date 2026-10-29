"""
Calls the MCP tool functions exactly as the server does and shows which
retrieval path answered. Run from mcp-server/ with the venv active:

    python scripts/smoke_server.py

Expect retrieval=hybrid / exact_code. If you see *_fallback, the reason is
printed above the results (Atlas unreachable, Voyage rate limit, ...).
Each query costs one Voyage embedding call.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

SEARCHES = [
    "floor looks cloudy and white after I put the sealer down",
    "GK-850 specs",
]
PRODUCT_QUERIES = [
    "grinding accelerator for hard slabs",
    "how much is 0752-2901",
]


def main() -> None:
    for q in SEARCHES:
        t = time.perf_counter()
        results = server.search(q)
        ms = (time.perf_counter() - t) * 1000
        print(f"\nsearch({q!r})  [{ms:.0f} ms]")
        for r in results:
            print(f"  {r['retrieval']:<17} {r['title']}")
        time.sleep(21)  # stay under Voyage's free-tier 3 requests/min

    for q in PRODUCT_QUERIES:
        t = time.perf_counter()
        rows = server.list_products(query=q)
        ms = (time.perf_counter() - t) * 1000
        print(f"\nlist_products(query={q!r})  [{ms:.0f} ms]")
        for r in rows[:5]:
            print(f"  {r['retrieval']:<17} {r['code']}  ${r['price']}  {r['description'][:60]}")
        if q != PRODUCT_QUERIES[-1]:
            time.sleep(21)


if __name__ == "__main__":
    main()
