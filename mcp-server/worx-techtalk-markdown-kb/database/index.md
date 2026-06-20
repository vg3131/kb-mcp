---
title: "Knowledge Base Database"
category: "Database"
generated: "2026-06-13"
---

# Knowledge Base Database

This site is also published as a **queryable SQLite database**. Every Markdown
page you can read here is parsed into a relational model so the same knowledge can
be searched, filtered, joined and reported on with SQL — while staying in lock-step
with the Markdown source.

The database is generated directly from the Markdown files in this site (plus
`catalogue/products.csv`) by `database/build.py`. Re-running the build after editing
any page refreshes the database.

## What's inside

| Content | Rows |
| --- | --- |
| Markdown documents (full provenance) | 329 |
| Content items (cards, guides, machines, catalogue, presenters) | 294 |
| Knowledge cards | 219 |
| Application guides (+ 87 steps) | 18 |
| Process guides (+ 13 phased steps) | 1 |
| Machine docs (+ 22 spec rows) | 7 |
| Catalogue category pages | 36 |
| Presenter profiles | 13 |
| Products (from `products.csv`) | 867 |
| Videos (TechTalk index) | 211 |
| People / Organisations / Locations | 81 / 13 / 11 |
| Sources (episodes & talks) | 172 |
| Full-text search rows | 294 |

## How it maps to the Markdown

Every record traces back to the file it came from. The `document` table holds one
row per Markdown file (with its repo-relative `path`), and each `content_item`
points at its `document_id`. So you can always go from a database row back to the
exact page on this site, and vice-versa.

```sql
-- Find the Markdown file behind any content item
SELECT ci.title, d.path
FROM content_item ci
JOIN document d ON d.id = ci.document_id
WHERE ci.title LIKE '%Cut Sikk%';
```

## The model at a glance

- **Entities** — `person`, `organization`, `location`, `product`, `source`, `video`.
- **Content spine** — `content_item` is a universal row for every primary page,
  with typed detail tables: `knowledge_card`, `application_guide` (+ `application_step`),
  `process_guide` (+ `process_step`), `machine_doc` (+ `machine_spec`),
  `catalogue_category`, `presenter_profile`.
- **Relationship graph** — `content_person`, `content_product`, `catalogue_product`,
  `content_organization`, and a generic `relationship` table for entity-to-entity edges.
- **Governance** — `alias` (name-variant normalisation), `data_quality_issue`
  (flagged inconsistencies), `audit_log` (build runs). Every content and entity row
  carries a `confidence` grade (`confirmed` / `likely` / `unclear` / `needs_verification`).
- **Search** — an FTS5 `search_index` over every content item, plus reporting views
  (`v_card_full`, `v_application_full`, `v_product_catalogue`, `v_presenter_directory`,
  `v_video_usage`, `v_data_quality`, `v_content_overview`).

## Example queries

```sql
-- Full-text search across every kind of content at once
SELECT content_type, title
FROM search_index
WHERE search_index MATCH 'densifier lithium';

-- Which TechTalk videos are cited by the most knowledge cards?
SELECT title, cited_by_cards FROM v_video_usage
WHERE cited_by_cards > 0 ORDER BY cited_by_cards DESC LIMIT 10;

-- Every product on the "Densifier" catalogue page, with price
SELECT p.code, p.description, p.price
FROM v_product_catalogue p
WHERE p.catalogue_pages LIKE '%Densifier%';

-- Application guides ordered by number of steps
SELECT title, application_category, steps, related_products
FROM v_application_full ORDER BY steps DESC;

-- GK-700 vs GK-850 specifications, side by side
SELECT spec_name, model, value FROM machine_spec ORDER BY spec_name, model;
```

## Data quality & confidence

The build cross-checks the corpus and records what it finds in
`data_quality_issue` rather than silently guessing — currently name variants
(e.g. *TechTalk* / *Tech Talk*, *Works Plus* / *Worx+*), people parsed from
free-text source labels (flagged `needs_verification`), and product names in
guides that don't resolve to a catalogue code. The `alias` table maps known
spelling variants to a canonical form.

## Building it yourself

The database lives in the repository's `database/` folder (alongside this site,
not served by it):

```bash
cd database
python3 build.py            # writes concrete_kb.db and concrete_kb_data.sql
```

No third-party packages are required. See `database/README.md` for the full
schema reference and rebuild instructions.
