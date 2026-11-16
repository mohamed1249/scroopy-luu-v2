# Saved pulls and source records

The `/scrape` endpoint identifies a pull using SHA-256 of canonical JSON containing
all submitted settings and explicit defaults. This includes database, output flags,
download budgets and crawl settings. URL fragments/default ports are normalized.
Starting URL order remains significant because bounded crawls depend on that order.
Settings are stored alongside the ID; API authentication headers are not settings.

MongoDB collections:

- `pulls`: settings, latest attempt ID/status, update time and refresh policy.
- `runs`: small run summaries and processing events, without extracted text.
- `JSONs`: source records under `content`, keyed by `file_name = pull_id:source_id`.
  Existing legacy records remain readable and untouched; no automatic migration.
- `pull_locks`: exclusive ownership during refresh. A duplicate refresh returns 409.

Refreshes merge successful documents. Failed, empty or unvisited pages never delete
previous documents. This is a current collection with per-document freshness, not
an atomic snapshot: successful pages can update during a partial run. Compare each
document's `run_id` with the pull's `last_run_id` to identify retained earlier data.
Removed pages are retained too; automatic deletion requires a separate explicit policy.
No full history of document bodies is stored. Run summaries currently have no retention
limit. Process termination can leave a lock: verify no run is active before manually
removing that pull's lock in MongoDB. Locks are never automatically stolen.

Version 1 source records include `source_id` (source type plus normalized requested
URL hash), `pull_id`, `run_id`, `content_hash` (SHA-256 of exact UTF-8 text), collection
time, requested/final URL where available, parent URL and extraction metadata.
Redirect aliases remain separate source IDs across runs. Hashes do not automatically
deduplicate different sources or remove repetition within documents.

`content` stores text once. `sections` is ordered and contains Python Unicode character
ranges `[start, end)` into that text (not UTF-8 byte offsets). HTML ranges carry tag
types and element IDs; table cells also carry row/table IDs and span attributes.
These are DOM text fragments, not visual-layout reconstruction or complete HTML.
PDF ranges carry page numbers, including empty pages; PDF extraction uses no OCR.
An entirely empty PDF extraction is reported as `EmptyPDFText` and never overwrites
previous text. Binary PDFs and unsupported response media types are rejected by the
webpage extractor instead of being stored as webpage text.
Collection timestamps describe successful extraction, not HTTP Last-Modified.

Files, when retained or needed for recovery, live under `runs/<pull_id>/<run_id>/`.
Recovery envelopes preserve the new keys and documents and use the existing recovery
command. Recovery restores documents; it does not rewrite historical run summaries.
The separate legacy `/drive` workflow retains its previous schema in this increment.

Restart Flask and refresh Streamlit after updating. Reports expose pull IDs and resolved
settings. Chunks, embeddings, dataset exports and model training remain separate steps.

## Image resources (0.2.0)

Opt-in `image_mode` values are `references` and `download`; default is `off`.
Documents and page reports include `images`, an ordered array of source URLs, alt
text and captions. `position` is the image's zero-based order among HTML img elements,
not a character offset into extracted text. Download records add a relative local
path, SHA-256, dimensions, format and size. Shared files live in `resources/images/`
and are not removed by run cleanup. Failed saves retain recovery metadata pointing
to those files; back up or move both together. Image failure details are in the page
records and activity events. Previous file references survive failed refreshes.

Image-only pages can be stored even with empty text. If a previously successful text
extraction becomes empty while images remain, old text/sections/extraction metadata
are retained and `text_retained_from_run` identifies their last successful text run.
Download failures do not erase the old image files. Old unreferenced files are not
automatically deleted. Across runs URLs are refetched to detect changes; bytes are
deduplicated on disk, not skipped using a potentially stale URL cache.

The separate image budget is additional to the webpage/PDF byte budget. Defaults are
5 MiB/image, 25 MiB/run and 100 unique download attempts; at most 100 img elements
are inspected per page. No processing model is involved.
