# Scroopy-Luu: data collection for model training and AI systems

Purpose: collect webpage/PDF data for training models and building AI systems.
Current output is source text and processing reports, not a ready-made supervised
training dataset. Training, fine-tuning, evaluation, and retrieval have different
requirements. Keep collection reusable and preparation explicit.

This is a proposed roadmap, not implemented functionality.

Update: `/scrape` now has deterministic pull IDs, merge-safe refreshes, and versioned
source records with hashes, extraction metadata and ordered HTML/PDF text ranges.
See `PULL_STORAGE.md` for the implemented contract and limitations. Dataset export,
embeddings and training preparation below remain proposals. The legacy `/drive`
endpoint and previously stored records have not been migrated.

## Recommended next increment: dataset export and provenance

Export a selected stored collection as compressed JSON Lines (`.jsonl.gz`), one
source document or PDF page per record. Stream records from MongoDB rather than
loading the collection into the Streamlit session. Generate an export only when
requested. Do not mix report events/errors into training rows. Combine webpage and
PDF records under a common schema. Include stable document ID, source/final URL,
fetch time in UTC, extraction mode/region, extractor version, content hash and text.
For PDF pages, preserve the PDF source URL and page number. New `/scrape` PDF records
carry this association; legacy records need explicit migration or recollection.

Create a small manifest with export version, filters, record count, content hashes,
and byte counts. This identifies the dataset used by an experiment without keeping
another permanent raw-content copy. Store any supplied license/permission metadata
and mark unknown information explicitly; do not infer permission from accessibility.

## Priority 2: content deduplication and incremental collection

Our visited set deduplicates URLs within a crawl. It does not deduplicate text across
different URLs or avoid downloading unchanged pages on later runs. Add exact content
hashes first; keep source-to-content references so attribution is not lost. Preserve
repetition inside documents by default. Near-duplicate detection is a later optional
feature, not an automatic destructive edit. Conditional requests using stored ETag
or Last-Modified values can reduce repeat downloads when servers support them.

## Priority 3: dataset review before export

Show language, length, empty/scanned PDF indicators, and simple repetition or
boilerplate signals as reviewable labels. Let users select filters and see excluded
counts before exporting. Optional personal-data detection/redaction belongs to the
export stage; preserve source records and document transformations. Avoid presenting
one quality score or detector as universally correct. No LLM is required for the first
version; scanned PDFs can be flagged for optional OCR later.

## Priority 4: appropriate chunks and evaluation splits

Offer token-aware chunking with a selected tokenizer when the downstream task needs
it. Keep document ID, source, section/page and offsets on every chunk. Group originals
and near duplicates before train/validation/test splitting, so chunks from the same
source do not leak into different splits. A plain-text corpus does not automatically
provide valid instruction-response pairs or labels; do not generate those silently.

## Priority 5: targeted and resumable crawling

Add URL path include/exclude rules and sitemap discovery to spend the crawl budget
on relevant pages. Persist a small frontier/checkpoint for pause/resume, without
copying page bodies. Add per-host pacing and robots.txt handling before larger crawls.
Robots directives control crawling, not training permission or dataset licensing.

## Design constraints

- Full-text collection and repetition remain available; filtering is opt-in.
- Prefer CPU-friendly deterministic steps before optional LLM processing.
- Avoid automatic multi-format exports, redundant permanent copies, or blanket
  acceleration that burdens source sites.
- Keep collection, preparation, and model training as separate learning steps.

## References

- Hugging Face Datasets: https://huggingface.co/docs/datasets/main/stream
- JSON loading: https://github.com/huggingface/datasets/blob/main/docs/source/loading.mdx
- DataTrove filtering/deduplication reference: https://github.com/huggingface/datatrove
- Robots Exclusion Protocol: https://www.rfc-editor.org/rfc/rfc9309.html
