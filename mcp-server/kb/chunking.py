"""
Turns the markdown tree + products.csv into retrieval chunks.

Chunking strategy (based on the real corpus, not a generic default):
  - Every included markdown file is small (knowledge cards ~1KB, application
    guides ~2KB, machine docs / presenter profiles ~1KB), so a whole file is
    already one coherent answer -> ONE chunk per file.
  - Anything longer than LONG_DOC_CHARS (today: only the 6KB process guide) is
    split on its "## " headings so each phase is retrievable on its own.
  - Each catalogue product row is its own tiny chunk. That makes exact SKU
    codes findable by the keyword index and product descriptions findable by
    meaning ("something for edge grinding").

Skipped on purpose: consolidated/* (the whole KB concatenated — duplicates),
index pages, catalogue/*.md tables (duplicates of products.csv), the video
index, and packaging files (README, MANIFEST, upload guide).

Each chunk keeps doc_path, so search can return the full parent document
(what Claude reads best) even though matching happens per chunk.
"""

import csv
import hashlib
import os
import re
from dataclasses import dataclass, field

from kb.index import build_index

INCLUDED_CONTENT_TYPES = {
    "knowledge_card",
    "application_guide",
    "process_guide",
    "machine_doc",
    "presenter_profile",
}

LONG_DOC_CHARS = 4000
PRODUCTS_CSV = os.path.join("catalogue", "products.csv")

# Sections that are attribution / navigation boilerplate. They stay in the
# stored text (Claude sees them, and the keyword index can match them) but are
# left out of what gets embedded, so every card's vector reflects its actual
# advice rather than "Timestamp: ... Video URL: ..." lines they all share.
EMBED_SKIP_SECTIONS = {"source", "category", "product url", "related pages"}

_MD_LINK_URL = re.compile(r"\]\((https?://[^)]+)\)")
_BARE_URL = re.compile(r"https?://\S+")


@dataclass
class Chunk:
    id: str  # stable: "<doc_path>#<n>" or "product:<code>"
    doc_path: str | None  # parent markdown file; None for products
    chunk_index: int
    title: str
    heading: str | None
    content_type: str
    category: str | None
    text: str  # what's stored and keyword-indexed
    embed_text: str  # what's sent to the embedding model
    extra: dict = field(default_factory=dict)  # frontmatter / product fields

    @property
    def embed_hash(self) -> str:
        return hashlib.sha256(self.embed_text.encode("utf-8")).hexdigest()


def _clean_for_embedding(s: str) -> str:
    """Drop boilerplate sections and URLs (pure noise to an embedding model)."""
    kept = [
        text
        for heading, text in _split_on_h2(s)
        if (heading or "").strip().lower() not in EMBED_SKIP_SECTIONS
    ]
    s = "\n\n".join(kept)
    s = _MD_LINK_URL.sub("]", s)
    s = _BARE_URL.sub("", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _split_on_h2(body: str) -> list[tuple[str | None, str]]:
    """[(heading, section_text)], preamble first with heading=None."""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in body.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), [line]))
        else:
            sections[-1][1].append(line)
    out = [(h, "\n".join(lines).strip()) for h, lines in sections]
    out = [(h, t) for h, t in out if t]
    # A preamble that's just the "# Title" line isn't worth its own chunk —
    # fold it into the first real section instead.
    if len(out) > 1 and out[0][0] is None and len(out[0][1]) < 300:
        out[1] = (out[1][0], out[0][1] + "\n\n" + out[1][1])
        out = out[1:]
    return out


def markdown_chunks(kb_root: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc in build_index(kb_root):
        if doc.content_type not in INCLUDED_CONTENT_TYPES:
            continue

        context = f"{doc.title}" + (f" ({doc.category})" if doc.category else "")
        pieces = (
            [(None, doc.body.strip())]
            if len(doc.body) <= LONG_DOC_CHARS
            else _split_on_h2(doc.body)
        )

        for i, (heading, text) in enumerate(pieces):
            # Prefixing the title (and section heading) gives every chunk its
            # context — a "## Phase 3 — Densification" section alone doesn't
            # say which guide it belongs to.
            prefix = context + (f" — {heading}" if heading else "")
            chunks.append(
                Chunk(
                    id=f"{doc.path}#{i}",
                    doc_path=doc.path,
                    chunk_index=i,
                    title=doc.title,
                    heading=heading,
                    content_type=doc.content_type,
                    category=doc.category,
                    text=text,
                    embed_text=_clean_for_embedding(f"{prefix}\n\n{text}"),
                    extra={"frontmatter": doc.frontmatter},
                )
            )
    return chunks


def product_chunks(kb_root: str) -> list[Chunk]:
    path = os.path.join(kb_root, PRODUCTS_CSV)
    chunks: list[Chunk] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip()
            desc = " ".join((row.get("description") or "").split())
            if not code or not desc or code in seen:
                continue
            seen.add(code)
            try:
                price = float(row.get("price") or "")
            except ValueError:
                price = None
            category = (row.get("category") or "").strip() or None
            hierarchy = (row.get("hierarchy") or "").replace("|", " > ").strip()

            text = f"{code} {desc}"
            embed_text = "\n".join(
                p for p in (desc, f"Category: {category}" if category else "", f"Type: {hierarchy}" if hierarchy else "") if p
            )
            chunks.append(
                Chunk(
                    id=f"product:{code}",
                    doc_path=None,
                    chunk_index=0,
                    title=desc,
                    heading=None,
                    content_type="product",
                    category=category,
                    text=text,
                    embed_text=embed_text,
                    extra={"code": code, "price": price, "url": row.get("url", ""), "hierarchy": hierarchy},
                )
            )
    return chunks


def build_chunks(kb_root: str) -> list[Chunk]:
    return markdown_chunks(kb_root) + product_chunks(kb_root)
