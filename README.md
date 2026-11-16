# Scroopy-Luu 🕸️

A local Flask + Streamlit scraper for collecting webpage and PDF text for AI data
workflows. Explore websites, keep source metadata, and refresh saved pulls without
silently deleting pages that failed to refresh.

**Status: early local-use release.** This is a learning project with automated tests,
not a public multi-user scraping service or a ready-made training dataset.

## What it does

- Full-page text by default, or optional main-content extraction.
- Breadth-first crawling with selectable levels and optional unlimited page count.
- Repeated links and URL fragments are deduplicated within the webpage crawl.
- Optional linked-PDF extraction, preserving PDF page order.
- Image references or optional JPEG/PNG/WebP/GIF downloads, deduplicated on disk.
- MongoDB storage grouped by a stable pull ID derived from URLs and all settings.
- Successful refreshes update documents; failed, empty and unvisited pages retain
  their previous content. Each document identifies its last successful run.
- Source IDs, content hashes, extraction metadata and ordered text ranges.
- Per-file/run download limits, public-destination checks and compressed recovery
  files when MongoDB saving fails.
- Streamlit previews and a downloadable JSON **run report**.

Full mode preserves extracted text and repetition; it does not preserve raw HTML,
render JavaScript, reconstruct visual layouts or perform OCR. Dataset export,
saved-pull browsing, chunking, embeddings and training are future work.

### Images

Choose **Images → references** to save image URLs, alt text, captions and page order
without downloading pictures, or **download** to retain local image files. Images
are off by default. Limits default to 5 MiB per image, a separate 25 MiB image budget
per run, and 100 unique download attempts. The image budget is additional to the
webpage/PDF budget. Repeated image URLs are fetched once per run; identical bytes
from different URLs or later runs share a single hash-named file.

Files live in `resources/images/` and survive working-file cleanup. MongoDB stores
their metadata and relative paths, not duplicate binary copies. Keep that directory
with the database when backing up or moving the project. Failed image refreshes keep
previous resource references. There is no automatic garbage collection of old image
versions yet; do not delete files still referenced by documents or recovery envelopes.

Discovery inspects up to 100 `<img>` elements per page, from the fetched HTML;
supports `src`, common lazy-source attributes and a smallest-candidate `srcset`
fallback. It does not render JavaScript, fetch CSS backgrounds, interpret every
`<picture>` variant, or extract images from PDFs. Image collection covers the whole
HTML page even in main-text mode. SVG, inline data images, audio and video are excluded.
Images with more than 40 million pixels are rejected. No OCR/descriptions or remote
image previews run automatically.

API settings: `image_mode` (`off`, `references`, `download`), `max_image_mb`,
`max_images_mb`, `max_images`. Image collection requires webpage extraction.
Default off settings preserve existing pull IDs; other image settings define new pulls.

## Quick start

Use Python **3.11 or 3.12**, a running local MongoDB server, and two terminals.
Run every command below from the repository root. MongoDB is a separate install;
Python dependencies do not install the database server.

```powershell
git clone https://github.com/mohamed1249/scroopy-luu-v2.git
cd scroopy-luu-v2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

On macOS/Linux, activate with `source .venv/bin/activate` instead.

### Terminal 1: Flask API

Generate a private local key and keep this terminal open:

```powershell
$env:SCROOPY_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:MONGODB_URI = "mongodb://127.0.0.1:27017/"
$env:SCROOPY_API_KEY
python -m flask --app API:app --no-debug run --host 127.0.0.1 --port 8080
```

Copy the generated key into the UI when prompted. It is printed only in your local
terminal; do not commit or share it. A missing key disables scraping (HTTP 503).
There is no built-in default key. The `.env.example` file is a template only;
the application does **not** automatically load `.env` files.

On macOS/Linux:

```sh
export SCROOPY_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MONGODB_URI="mongodb://127.0.0.1:27017/"
printf '%s\n' "$SCROOPY_API_KEY"
python -m flask --app API:app --no-debug run --host 127.0.0.1 --port 8080
```

### Terminal 2: Streamlit

Activate the same virtual environment, then:

```powershell
python -m streamlit run streamlit_app.py --server.address 127.0.0.1
```

Open <http://127.0.0.1:8501>, paste your key, and try
<https://quotes.toscrape.com/> with PDF downloads off and linked pages off.
For a small crawl, enable linked pages, use two levels and a five-page limit.

If MongoDB was installed as a Windows service, check `Get-Service MongoDB`.
If its status is Stopped, run `Start-Service MongoDB` from an administrator shell.
If the service is missing, starting it will not install MongoDB.

## Pulls, runs and storage

A **pull** is a saved selection of URLs and settings. A **run** is one attempt to
collect that pull. Changing any setting—including output flags and download
limits—creates a different pull. URL order matters for bounded crawling.

MongoDB collections are `pulls`, `runs`, `JSONs` and `pull_locks`. Documents keep
their extracted text once, with character offsets for sections or PDF pages.
Refreshes merge successful documents; they are **not atomic snapshots**. Removed
or unreachable pages stay until explicitly managed, so check document run IDs for
freshness. Two refreshes of the same pull cannot run concurrently.

Temporary files are removed by default. Requested working files and failed-save
recovery files live under `runs/<pull_id>/<run_id>/`. Defaults are 25 MiB per file
and 100 MiB per run. Unlimited pages does not disable byte limits.

- [Storage contract](docs/PULL_STORAGE.md)
- [API settings and reports](docs/SCRAPING_REPORTS.md)
- [Dataset preparation roadmap](docs/DATASET_ROADMAP.md)
- [Recovery and troubleshooting](docs/OPERATIONS.md)

## API example

The API uses the `API-KEY` header. See [examples/scrape.json](examples/scrape.json).

```powershell
$key = Read-Host "Your configured API key"
$result = Invoke-RestMethod -Uri "http://127.0.0.1:8080/scrape" -Method Post `
  -Headers @{"API-KEY" = $key} -ContentType "application/json" `
  -Body (Get-Content -Raw examples/scrape.json)
$result | ConvertTo-Json -Depth 30
```

## Tests

```powershell
python -m pip install -r requirements-dev.txt
python -B -m unittest discover -s tests -q
python -B scripts/verify_local_transport.py
```

The unit/UI tests mock websites and MongoDB. Optional local database integration
checks require MongoDB; they create and delete only their own generated databases:

```powershell
python -B scripts/verify_pull_storage.py
python -B scripts/verify_local_mongodb.py
```

## Project layout

```text
API.py, streamlit_app.py       API and local UI entry points
crawler.py, scr_prp.py         Crawling and extraction
pull_schema.py, storage.py    Document identity and persistence
download_limits.py           Streaming byte limits
url_safety.py                 Public-only, DNS-pinned HTTP transport
run_workspace.py, recover.py  Owned files and recovery
tests/                       Offline regression and Streamlit tests
scripts/                     Optional local integration checks
docs/, examples/             Contracts, operations and sample request
```

## Boundaries

Keep Flask and Streamlit on localhost. A shared key is not tenant isolation:
authenticated users can select database names. Global concurrency quotas, automatic
stale-lock recovery, crawler pacing and robots.txt handling are not implemented.
Use modest limits and respect site terms and access restrictions. Collection does
not establish permission to train on or redistribute the content.

The legacy `/drive` route is optional and has not received the new pull schema.
See [operations](docs/OPERATIONS.md) before enabling it. Do not expose this early
release directly to the internet.

## Contributing and license

Small, tested changes are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). Code is MIT-licensed; collected third-party content is
not covered by this repository's license.
