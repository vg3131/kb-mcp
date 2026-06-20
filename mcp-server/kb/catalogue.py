"""
list_products tool implementation — filters catalogue/products.csv (867 rows)
directly in memory. Separate data source from the markdown documents; no
index.py involvement.
"""

import csv
import os

CATALOGUE_CSV_RELPATH = os.path.join("catalogue", "products.csv")

# Parsed rows cached per kb_root after first load — 867 rows is cheap to keep
# around, and re-parsing the CSV on every call would be wasted work.
_cache: dict[str, list[dict]] = {}


def _load_rows(kb_root: str) -> list[dict]:
    csv_path = os.path.join(os.path.abspath(kb_root), CATALOGUE_CSV_RELPATH)
    if csv_path in _cache:
        return _cache[csv_path]

    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                price = float(row.get("price") or "")
            except ValueError:
                price = None
            rows.append(
                {
                    "code": row.get("code", ""),
                    "description": row.get("description", ""),
                    "price": price,
                    "category": row.get("category", ""),
                    "url": row.get("url", ""),
                }
            )

    _cache[csv_path] = rows
    return rows


def list_products(
    kb_root: str,
    category: str | None = None,
    max_price: float | None = None,
    query: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Args:
        kb_root: the KB_ROOT this server was started with.
        category: optional case-insensitive exact match on the CSV's
            `category` column, e.g. "Grinding Tools", "Sealer", "Vacuum Parts".
        max_price: optional inclusive upper bound on `price`.
        query: optional case-insensitive substring match against `description`.
        limit: max rows to return. Defaults to 200 (~40KB of JSON) — the
            catalogue is 867 rows and an unfiltered call serializes to
            ~175KB, over the tool-result size limit, so this caps it even
            when category/max_price/query aren't narrow enough on their own.

    Returns:
        A list of dicts: {"code", "description", "price", "category", "url"}.
        (category_url, direct_link, hierarchy from the CSV are dropped — not
        useful for this tool's purpose.)
    """
    rows = _load_rows(kb_root)

    if category:
        category_lower = category.lower()
        rows = [r for r in rows if r["category"].lower() == category_lower]
    if max_price is not None:
        rows = [r for r in rows if r["price"] is not None and r["price"] <= max_price]
    if query:
        query_lower = query.lower()
        rows = [r for r in rows if query_lower in r["description"].lower()]

    return rows[:limit]
