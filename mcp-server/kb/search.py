"""
search tool implementation — keyword search over the in-memory DocEntry index
built by index.py.

Returns the FULL body of whichever documents look relevant, not just a
snippet. That's deliberate: the point of this tool is to hand back the actual
.md files Claude should read for a given question, in one call, rather than
making the model pick from a list of titles and then call get_document again
for each one.
"""

from kb.index import DocEntry

# Title and frontmatter matches are much stronger relevance signals than a
# single mention buried in a 2KB body, so they're weighted accordingly.
TITLE_WEIGHT = 5
FRONTMATTER_WEIGHT = 2
BODY_WEIGHT = 1

# Extra credit for matching every query term somewhere, not just piling up
# repeats of one term — keeps a document about "lithium densifier saturation"
# from losing to one that just says "densifier" fifty times.
ALL_TERMS_BONUS = 10

# consolidated/*.md are the whole knowledge base concatenated into a handful
# of giant files (one of them is 130KB+ on its own) — meant for bulk-uploading
# into an AI tool's knowledge store, per their own README. Left in the search
# index they match almost any query (they contain nearly every term in the
# corpus) and crowd out the specific files that actually answer the question,
# while also risking the ~150K char tool-result limit by themselves. Excluded
# from default search; still reachable via content_type="consolidated"
# explicitly, or list_by_category()/get_document().
#
# "other" is the fallback bucket for files with no recognized frontmatter
# `type` and no mapped top-level folder — packaging/meta artifacts like
# MANIFEST.md, or stale docs describing tooling that's since been removed
# from the repo (e.g. a leftover page about a SQLite build script that no
# longer exists). They're real files and still reachable via
# list_by_category()/get_document() if something specifically needs them,
# but they shouldn't surface as an answer to an unrelated keyword search.
EXCLUDED_BY_DEFAULT = {"consolidated", "other"}

# Defensive budget on total body characters returned in one search() call.
# Guards against a handful of large-but-legitimate files (e.g. a big catalogue
# page) adding up past the ~150K char tool-result limit. The first result is
# always included even if it alone exceeds the budget — better to return one
# oversized-but-relevant document than nothing.
MAX_TOTAL_BODY_CHARS = 100_000


def _score(doc: DocEntry, terms: list[str]) -> float:
    title_lower = doc.title.lower()
    body_lower = doc.body.lower()
    fm_text = " ".join(str(v) for v in doc.frontmatter.values()).lower()

    score = 0.0
    terms_matched = 0
    for term in terms:
        hits = (
            TITLE_WEIGHT * title_lower.count(term)
            + FRONTMATTER_WEIGHT * fm_text.count(term)
            + BODY_WEIGHT * body_lower.count(term)
        )
        if hits:
            terms_matched += 1
        score += hits

    if terms and terms_matched == len(terms):
        score += ALL_TERMS_BONUS

    return score


def search(
    index: list[DocEntry],
    query: str,
    content_type: str | None = None,
    category: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Args:
        index: the full DocEntry list from build_index().
        query: keyword(s) to match — a few terms work better than a sentence.
        content_type: optional exact-match filter.
        category: optional exact-match filter.
        limit: max documents to return (kept full bodies, so default is
            deliberately small — 5 full knowledge-base files is already
            ~10-15KB, plenty for one answer without overloading the response).

    Returns:
        A list of dicts, each a full document: {"path", "title", "content_type",
        "category", "score", "frontmatter", "body"}. Empty list if no term in
        the query matched anything (no fallback to unrelated results).
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []

    candidates = index
    if content_type:
        candidates = [d for d in candidates if d.content_type == content_type]
    else:
        candidates = [d for d in candidates if d.content_type not in EXCLUDED_BY_DEFAULT]
    if category:
        candidates = [d for d in candidates if d.category == category]

    scored = [(_score(d, terms), d) for d in candidates]
    scored = [(s, d) for s, d in scored if s > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].title))

    results = []
    total_chars = 0
    for score, doc in scored[:limit]:
        if results and total_chars + len(doc.body) > MAX_TOTAL_BODY_CHARS:
            break
        total_chars += len(doc.body)
        results.append(
            {
                "path": doc.path,
                "title": doc.title,
                "content_type": doc.content_type,
                "category": doc.category,
                "score": score,
                "frontmatter": doc.frontmatter,
                "body": doc.body,
            }
        )

    return results
