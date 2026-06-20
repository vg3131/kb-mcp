"""
list_by_category tool implementation — browse the index by content_type/
category without searching. Returns lightweight rows only (no body); pair
with get_document, or just call search() instead if full content is wanted
directly.
"""

from kb.index import DocEntry


def list_by_category(
    index: list[DocEntry],
    content_type: str | None = None,
    category: str | None = None,
) -> list[dict]:
    """
    Args:
        index: the full DocEntry list from build_index().
        content_type: optional exact-match filter.
        category: optional exact-match filter.

    Returns:
        A list of dicts: {"path", "title", "content_type", "category"},
        sorted by content_type, then category, then title. ~330 documents
        total, so returning everything when both filters are omitted is fine
        size-wise — it's meant as a directory listing, not a content dump.
    """
    results = index
    if content_type:
        results = [d for d in results if d.content_type == content_type]
    if category:
        results = [d for d in results if d.category == category]

    results = sorted(results, key=lambda d: (d.content_type, d.category or "", d.title))

    return [
        {
            "path": d.path,
            "title": d.title,
            "content_type": d.content_type,
            "category": d.category,
        }
        for d in results
    ]
