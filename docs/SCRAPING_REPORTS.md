# Scraping reports

Scroopy-Luu collects webpage and PDF data for training models and building AI
systems. It currently collects and stores extracted text; task-specific dataset
preparation remains a separate step. See DATASET_ROADMAP.md for proposed additions.

`POST /scrape` requires no API key and retains the legacy `errors` response field,
PDF workflow, MongoDB writes, and cleanup. Intermediate JSON files are now opt-in.
The new `report` is returned in the HTTP response; it is not saved to a file.
`/drive` also uses an isolated run directory; its Google Drive listing behavior is otherwise unchanged.

Example request:

```json
{
  "URLs": ["https://example.com"],
  "download_pdfs": ["n"],
  "download_content": ["y"],
  "db": "development",
  "extraction_mode": "main",
  "include_subpages": true,
  "max_pages": 20,
  "max_levels": 3,
  "same_site": true,
  "keep_work_files": false,
  "max_download_mb": 25,
  "max_run_download_mb": 100
}
```

Defaults: `extraction_mode="full"`, `include_subpages=false`, `max_pages=20`.
The page limit is shared across all starting URLs in a crawl and includes the starting pages.
Set `max_pages` to null for unlimited page count. `max_levels` defaults to 2 for API
compatibility; the UI defaults to 3. Level 1 is a starting page, level 2 its links,
and level 3 links found on level-2 pages. Crawling is breadth-first. Relative links resolve
against the final URL after redirects. Repeated links and fragments are fetched
once across the webpage crawl; non-HTTP(S) links and links beyond the limit are reported as skipped.
By default, only the hostnames of the starting URLs are followed. Set `same_site=false`
to permit links to other public HTTP(S) hosts. Local/private/special-use IPs,
credential-bearing URLs, and nonstandard ports are blocked. Every redirect is
revalidated (five redirects maximum), and connections pin the checked IP while
retaining the original hostname for TLS. Environment proxies and netrc credentials
are disabled for scraping. No overall output-JSON size limit is imposed.

The three legacy input lists must have equal lengths and contain strings.
The flags retain legacy semantics: only `n` (case-insensitive) disables a step.
Invalid extraction options are rejected with HTTP 400 before any work.

## Response report

- `pages`: single-page results including text, requested mode, selected region,
  status, duration, outcome, error, parent URL, and depth.
- `summary`: `attempted`, `success`, `empty`, `failed`, `skipped` counts for webpage
  fetches only. PDF, persistence, and cleanup events are not webpage outcomes.
- `events`: legacy stage observations: PDF download, PDF extraction, JSON writing,
  MongoDB saves, cleanup, and disabled steps.

Success means extraction succeeded; it does not mean persistence succeeded.
A database save failure returns HTTP 500 with the report and retains a compressed recovery document.
Partial webpage failures retain the legacy HTTP 200 response with error details.
PDF events include discovery and every discovered link's download/validation
outcome: HTTP errors, timeouts, invalid URLs, unsupported schemes, duplicate links,
unreadable/zero-page PDFs, and file-write failures. Invalid downloads are removed
from the run's own PDF directory. Valid files continue to text extraction.
PDF discovery retains the old behavior of trying all HTTP(S) links, so ordinary
HTML links can produce `InvalidPDF` events. It does not crawl PDFs from subpages;
PDF discovery applies to each supplied starting URL, as before.

## Workspace ownership

Every API call creates `runs/<generated-run-id>/`. Webpage files are organized as:

```text
runs/<pull-id>/<run-id>/pages/<seed>/
  content.json
  PDFs/<safe-unique-name>.pdf
  <safe-unique-name>.pdf.json
  subpages/<child>/content.json
```

Run IDs are generated, never accepted from request input. Folder names are short,
sanitary labels plus hashes; PDF names also have unique suffixes. Paths are passed
explicitly: requests never change the process working directory. Only documents
created by that run are saved to MongoDB. `/drive` uses its own run's PDF/JSON
folders and ID-based filenames, ignoring unsafe remote filenames.

The response reports `run_id`, `workspace`, `files_retained`, and successful pages'
relative `output_file` paths. `keep_work_files=true` retains artifacts for inspection.
By default, documents go directly to MongoDB without a JSON write/read round trip.
Raw PDFs are removed after extraction, including when the resulting document must
be kept for recovery. Page/download/extraction failures remain visible in the report
but do not retain the run's artifacts. Database save failures retain only compressed
`*.json.gz` recovery envelopes containing `file_name` and `content`; successfully
saved documents do not get redundant disk copies. Recovery files can consume disk
until the operator resolves the failed saves. `keep_work_files=true` still preserves
raw PDFs and compact JSON exports for inspection. No separate report file is
written. Old shared folders and unrelated PDFs are untouched.

This isolates filesystem ownership, not database writes: simultaneous runs targeting
the same MongoDB document use a unique `file_name` index and an upsert. A concurrent
winner is retried as an update. The latest successful write wins; this is not
versioned storage. Public-destination restrictions now apply to scraped URLs. These controls are not
a complete public-deployment review: authentication, client-selected database names,
and resource/concurrency limits still need review.

## Python helpers

```python
report = scrape_page_and_subpages_report(url, mode="main", max_pages=20)
# Or use the existing helper with its new opt-in report format:
report = scrape_page_and_subpages_content(url, return_report=True)
```

The existing helper defaults to its legacy `(content, link_count)` shape and
returns `None` if the seed fails. Its link count now reflects unique resolved
non-seed targets, including skipped targets. Fetching is bounded to 20 pages and two levels by
default; the old Facebook-specific exclusion is replaced by general HTTP(S)
link handling. `main_content=False` still fetches the seed for link discovery but
omits its text from the report and legacy content output. The seed's measured
character count and extraction outcome remain visible.

## Offline verification

```powershell
python -B -m unittest discover -s tests -v
```

API tests execute the actual route function in an isolated Flask app. They exclude
module startup and Google imports, replace credentials and infrastructure with
mocks, and use temporary working directories. They do not verify live MongoDB,
Google Drive, CORS wiring, or full application startup.

PDF helper tests use generated PDFs and real parsing. Network and databases remain mocked.

## Download limits

Both `/scrape` and `/drive` accept positive integer `max_download_mb` (default 25)
and `max_run_download_mb` (default 100). Despite the short field names, units are
MiB: 1,048,576 bytes. Invalid values return HTTP 400 before creating run files.

`/scrape` shares one budget across every seed, subpage, PDF discovery request, and
PDF download. Re-fetches and failed/invalid bodies consume budget too. PDF bodies
stream to disk; HTML buffers are bounded before parsing. Drive uses 64 KiB download
chunks with the same budget policy. A stopped transfer removes its partial PDF;
already successful documents can still be stored.

`report.downloads` contains the configured byte limits and `received_body_bytes`.
Drive returns this information in its top-level `downloads` field. Web/PDF report
errors distinguish `DownloadTooLarge` from `RunDownloadLimitExceeded`. A webpage
rejected before a network request is skipped, not counted as attempted.

Limits count decoded response body bytes, not network traffic, headers, redirect
bodies, total parser memory, or output JSON size. Actual streamed chunks are checked;
Content-Length is only an early rejection hint for identity-encoded responses.
A rejected final chunk can increase the received counter beyond a limit by up to
64 KiB, but that chunk is never saved or included in extracted content. Raising
these settings raises resource exposure; no separate administrator ceiling exists
yet. Reports and parsed HTML can require more memory than the downloaded bytes.

## MongoDB identity repair

Saving uses `update_one({"file_name": ...}, {"$set": {"content": ...}}, upsert=True)`
and requires a unique index on `file_name`. Updates preserve identity and other
fields. Existing conflicting indexes, duplicate filenames, or multiple documents
missing filenames may prevent index creation: the save fails visibly and `/scrape`
retains its files. No existing documents are automatically deleted or repaired.

The index is requested when the application saves a document. Live verification
against local MongoDB 6.0.8 tested repeated/concurrent saves and duplicate rejection
in a generated disposable database, then removed that database. Existing databases
were not inspected or modified. Offline tests still use doubles.

## Local configuration and verification

Both API routes use `MONGODB_URI` when set, otherwise
`mongodb://127.0.0.1:27017/`. Server-selection and connect timeouts are five seconds.
The old embedded remote database URIs have been removed from the application.
`.env.example` documents the variable; the app does not automatically load `.env`.

```powershell
$env:MONGODB_URI = "mongodb://127.0.0.1:27017/"
python -B scripts/verify_local_mongodb.py
```

This opt-in script always uses local MongoDB and a UUID-named temporary database.
It verifies repeated/concurrent saves, duplicate rejection, the actual Flask route,
PDF extraction, nested files, and database writes. HTTP bodies are controlled
fixtures, not external downloads. It deletes only its own temporary database and
files. Local MongoDB remains running.

```powershell
python -B scripts/verify_local_transport.py
```

This optional check requires `cryptography`. It starts a temporary local HTTPS
fixture, tests original Host/TLS hostname behavior, and verifies that a hostname
mismatch is rejected. Only the test redirects pinned socket connections to the
local fixture; production transport continues to block loopback destinations.

## Space-saving behavior

- `/scrape` and `/drive` save extracted documents directly to MongoDB by default.
- `keep_work_files=true` opts into compact, uncompressed JSON exports and raw PDFs.
- Failed database saves produce compressed, self-contained recovery documents;
  the application does not silently discard extracted content after a save failure.
- `report.storage.retained_bytes` reports files actually remaining on disk;
  `recovery_needed` distinguishes recovery from requested retention. Drive returns
  these fields under its top-level `storage` key.
- `/scrape` accepts `include_text=false` for a metadata-only response. Extracted
  text is still stored intact in MongoDB; default `true` preserves existing clients.
- Repeated text in full extraction mode remains intact. No lossy shortening,
  deletion of old user files, or automatic database compaction is performed.

These changes reduce temporary disk use and response size. They do not eliminate
HTML parser memory overhead or automatically deduplicate identical content across
different URLs. Compressed recovery size depends on the content.

## Database readiness and recovery

Both `/scrape` and `/drive` now ping MongoDB, require acknowledged writes, and
ensure the unique `file_name` index before creating a run workspace or downloading
anything. Failure returns HTTP 503 with `stage: database_preflight` and an error
type. Reachability and index readiness do not guarantee that a later write will
succeed; those failures still create compressed recovery copies.

New recovery envelopes include the target database name. Older envelopes remain
supported, but the recovery command always requires an explicit `--db`. It uses
`MONGODB_URI` (local MongoDB by default), just like the API. Select a finished run
or one recovery file explicitly; there is no background scan or automatic replay.

Preview without database access or file deletion:

```powershell
python -B recover.py "runs/<pull-id>/<run-id>" --db development
```

Save to MongoDB and remove each recovery file only after acknowledged insertion,
acknowledged overwrite, or verification of identical already-stored content:

```powershell
python -B recover.py "runs/<pull-id>/<run-id>" --db development --apply
```

A different existing document is a conflict by default: its recovery copy remains.
Only when you deliberately intend to replace that document, add `--overwrite`.
Wrong-database envelopes, corrupt files, network errors, and unacknowledged writes
are retained and reported. A short-lived per-directory lock prevents competing
recovery commands from handling the same file. After a crash, an abandoned
`.replay.lock` requires manual inspection/removal before retrying. Normal JSON,
PDF files, and parent directories are never deleted by recovery.

The command prints JSON results and returns a nonzero exit code for any failure,
conflict, or saved-but-not-deleted file. Preview `ready` validates file structure
and database metadata, not server readiness or document size. Recovery reads at
most 64 MiB of decompressed data per file; larger envelopes remain untouched.
There are no custom automatic retry loops, queues, or server-side jobs.

Offline recovery tests: `python -B -m unittest discover -s tests -v`.
The opt-in `verify_local_mongodb.py` also verifies real preview/replay using only
its generated disposable database and temporary recovery files.

## Recursive crawl and unlimited page count

For a portfolio homepage -> tabs -> posts, enable linked pages, set 3 levels,
and select Unlimited pages. In JSON:

```json
{"include_subpages": true, "max_levels": 3, "max_pages": null, "same_site": true}
```

Unlimited removes only the page-count cap. Crawl levels, download budgets, URL
safety, HTTP timeouts, and the UI response timeout remain active. Links on the final
level are not expanded. Same-site means exact starting hostnames, not every
subdomain; redirects leaving those hosts are also blocked unless scope is widened.

One breadth-first frontier and visited set cover all starting URLs. Fragments,
host/scheme case, default ports, and percent-encoded unreserved path characters
are normalized. Query strings and potentially distinct trailing-slash URLs are
preserved; redirect targets are checked before another request. Distinct URLs with
identical text are not merged. Report parents identify the first discovered path,
and `crawl.duplicate_links_avoided` counts avoided repeated links.

When retaining work files, descendants nest beneath their first parent. To avoid
Windows path-length failures at deep levels, long paths use the run's `deep-pages`
folder; report parent URLs still preserve their crawl relationships. PDF discovery
still applies only to supplied starting URLs and is a separate downloader; it may
request a URL already fetched by the webpage crawler.

The UI now starts with a finite cap of 20 and 3 levels. Existing Flask processes
started without reload need restarting after this backend change. Refresh Streamlit
for the new controls. No live recursive portfolio crawl was launched automatically.
