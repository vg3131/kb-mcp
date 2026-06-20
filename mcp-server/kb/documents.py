"""
get_document tool implementation — reads one markdown file straight off disk
and returns its full parsed content. search() already embeds full bodies
inline for its matches; this is for when the model already knows the exact
path (e.g. from list_by_category, or re-reading a path search returned
earlier) and just wants that one file again.
"""

import os

import frontmatter

from kb.index import classify_path, title_from_filename


def get_document(kb_root: str, path: str) -> dict:
    """
    Args:
        kb_root: the KB_ROOT this server was started with.
        path: repo-relative path, e.g.
            "knowledge-cards/densifiers/potassium-early-lithium-late.md".

    Returns:
        {"path", "title", "content_type", "category", "frontmatter", "body"}

    Raises:
        ValueError: path isn't a .md file, or resolves outside kb_root.
        FileNotFoundError: no file at that path.
    """
    kb_root_abs = os.path.abspath(kb_root)

    if not path.endswith(".md"):
        raise ValueError(f"Not a markdown path: {path!r}")

    # This is the one tool that takes a path straight from model input, so
    # it's the one that needs the traversal check: resolve against kb_root
    # and confirm the result is still inside it before touching the filesystem.
    candidate = os.path.abspath(os.path.join(kb_root_abs, path))
    if os.path.commonpath([kb_root_abs, candidate]) != kb_root_abs:
        raise ValueError(f"Path escapes the knowledge base root: {path!r}")

    if not os.path.isfile(candidate):
        raise FileNotFoundError(
            f"No document at {path!r}. Use search() or list_by_category() to find a valid path."
        )

    with open(candidate, "r", encoding="utf-8") as f:
        post = frontmatter.load(f)

    meta = dict(post.metadata)
    relpath = os.path.relpath(candidate, kb_root_abs).replace(os.sep, "/")
    content_type, category = classify_path(relpath, meta)
    title = meta.get("title") or title_from_filename(candidate)

    return {
        "path": relpath,
        "title": title,
        "content_type": content_type,
        "category": category,
        "frontmatter": meta,
        "body": post.content,
    }
