# Saved pulls and source records

The `/scrape` endpoint identifies a pull using SHA-256 of canonical JSON containing
all submitted settings and explicit defaults. This includes database, output flags,
download budgets and crawl settings. URL fragments/default ports are normalized.
Starting URL order remains significant because bounded crawls depend on that order.
Settings are stored alongside the ID; HTTP headers are not settings.

MongoDB collections:

- `JSONs`: one schema-3 document per unique nonempty exact text. Top-level `content`
  contains the text, `content_hash` its SHA-256, and `sources` the source metadata.
  Each source keeps requested/final URLs, source/pull/run IDs, collection time,
  extraction metadata, ordered sections, images and PDF references.
- `pulls`: settings, latest attempt status and refresh policy.
- `runs`: summaries, download/crawl metadata, compact failure/skip counts and up to
  200 issue examples (failures first). Final cleanup results are in the response.
- `pull_locks`: exclusive ownership during a pull refresh.

No separate text collection is created for new data. Older `contents` and schema-1/2
records remain readable; existing datasets are not automatically bulk-migrated.
Successfully refreshed legacy source rows are replaced by grouped records.

Exact text is grouped across URLs and pulls within a database; near-duplicates and
repetition inside a page are not removed. `sources` retains separate provenance for
sources collected with different settings. Use `storage.find_source(collection, key)`
to resolve one source for refresh or recovery.

Whitespace-only and empty text creates no document. An empty, failed or unvisited
source never erases earlier good text. On a successful changed-text refresh, the
source is attached to the new content before detaching it from the old content;
other sources are preserved. A group with no remaining sources is removed.
These operations are not a multi-document transaction: an interrupted move can
briefly leave a source attached to both groups; retrying that source completes it.

Retained JSON exports and recovery files are self-contained per-source copies and
may repeat text; database grouping does not change their portability. The legacy
Drive workflow keeps its older schema. Run summaries have no automatic retention.
Source arrays are subject to MongoDB's document size limit; a write that exceeds
it fails with a recovery file rather than discarding source metadata.

`sections` is ordered and contains Python Unicode character
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

Image-only pages with no previous text are not stored as content documents. A refresh
that has images but empty text preserves previously collected text when available.
Download failures do not erase the old image files. Old unreferenced files are not
automatically deleted. Across runs URLs are refetched to detect changes; bytes are
deduplicated on disk, not skipped using a potentially stale URL cache.

The separate image budget is additional to the webpage/PDF byte budget. Defaults are
5 MiB/image, 25 MiB/run and 100 unique download attempts; at most 100 img elements
are inspected per page. No processing model is involved.
