# Changelog

## 0.2.1 — 2026-11-23

- Blank or omitted database names now use `scroopyluu` in the UI and API.
- Existing names are reused; new databases are created by MongoDB on first write.
- The sidebar shows the effective destination before scraping.

## 0.2.0 — 2026-11-16

- Optional image references and bounded JPEG/PNG/WebP/GIF downloads.
- Per-run URL deduplication and hash-based file deduplication across pulls/runs.
- Alt text, captions, dimensions, source URLs and local paths attached to documents.
- Previous resource references retained on image download failure.
- Separate image limits and resource tables in Streamlit; default behavior unchanged.

## 0.1.0 — 2026-11-16

Initial public local-use release.

- Flask scraping API and Streamlit interface.
- Full/main text extraction, recursive page levels and optional unlimited page count.
- Run-wide webpage URL deduplication and bounded downloads.
- Stable saved pulls, source metadata, content hashes and ordered text ranges.
- Partial-refresh preservation and compressed failed-write recovery.
- Configurable API keys, localhost startup and optional legacy Drive dependencies.
- Regression tests and Windows/Linux CI.

Public hosting, JavaScript rendering, OCR, dataset export and model training are
outside this release's scope. See README and docs/OPERATIONS.md for limitations.
