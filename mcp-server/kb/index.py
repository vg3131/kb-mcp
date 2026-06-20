"""
Builds an in-memory index of every markdown file in the knowledge base, once,
at server startup. search.py and browse.py both query this list instead of
re-walking the filesystem on every tool call.
"""

import os
from dataclasses import dataclass, field

import frontmatter

# Top-level folder -> content_type, used as a fallback when a file's own
# frontmatter doesn't carry a `type` field. Checked against the real files:
# knowledge-cards/, applications/, process-guides/, and machines/ files don't
# have a `type` field (they use `category` instead, which is for humans, not
# filtering). catalogue/, presenters/, videos/, consolidated/, and the index.md
# files all DO carry their own `type` (e.g. "product_category",
# "presenter_profile", "video_index", "consolidated", "index") — those are read
# straight from frontmatter instead of being hard-coded here.
FOLDER_CONTENT_TYPE = {
    "knowledge-cards": "knowledge_card",
    "applications": "application_guide",
    "process-guides": "process_guide",
    "machines": "machine_doc",
}


@dataclass
class DocEntry:
    """One row per markdown file."""

    path: str  # repo-relative, forward-slash separated, e.g.
    # "knowledge-cards/machines/680-rc-the-goldilocks-machine.md"
    title: str
    content_type: str
    category: str | None
    frontmatter: dict = field(default_factory=dict)
    body: str = ""  # markdown body, frontmatter stripped


def title_from_filename(path: str) -> str:
    """Fallback title for the rare file with no frontmatter `title`."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.replace("-", " ").replace("_", " ").title()


def classify_path(relpath: str, meta: dict) -> tuple[str, str | None]:
    """
    Derive (content_type, category) from a file's repo-relative path and its
    parsed frontmatter. Shared between build_index() and documents.get_document()
    so a file looks the same whether it was found via search/browse or fetched
    directly by path.

    content_type: frontmatter's own `type` field wins when present; otherwise
    falls back to the top-level folder via FOLDER_CONTENT_TYPE, or "other" for
    anything unmapped (e.g. leftover files outside the expected layout).

    category: the subfolder directly under the top-level folder, when there is
    one — knowledge-cards/<category>/file.md, applications/<category>/file.md,
    machines/<category>/file.md. This is the same value frontmatter's own
    category_key / application_category fields carry; reading it from the path
    avoids chasing a differently-named frontmatter key per content type.
    Two-segment paths (catalogue/*.md, presenters/*.md, top-level index.md)
    have no subfolder, so category is None for those.
    """
    parts = relpath.split("/")
    top = parts[0]

    fm_type = meta.get("type")
    content_type = fm_type if fm_type else FOLDER_CONTENT_TYPE.get(top, "other")

    category = parts[1] if len(parts) >= 3 else None

    return content_type, category


def build_index(kb_root: str) -> list[DocEntry]:
    """Walk kb_root recursively and parse every .md file into a DocEntry."""
    kb_root = os.path.abspath(kb_root)
    entries: list[DocEntry] = []

    for dirpath, dirnames, filenames in os.walk(kb_root):
        # Skip hidden dirs (.git etc.) in case KB_ROOT ever points at a repo root.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in sorted(filenames):
            if not filename.endswith(".md"):
                continue

            full_path = os.path.join(dirpath, filename)
            relpath = os.path.relpath(full_path, kb_root).replace(os.sep, "/")

            with open(full_path, "r", encoding="utf-8") as f:
                post = frontmatter.load(f)

            meta = dict(post.metadata)
            content_type, category = classify_path(relpath, meta)
            title = meta.get("title") or title_from_filename(filename)

            entries.append(
                DocEntry(
                    path=relpath,
                    title=title,
                    content_type=content_type,
                    category=category,
                    frontmatter=meta,
                    body=post.content,
                )
            )

    return entries
