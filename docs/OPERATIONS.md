# Operations and recovery

Run from the repository root. `.env.example` documents variables but is not loaded
automatically. No application API key is required. Keep the services on localhost.
The UI sends requests only to localhost:8080. UI timeout does not cancel server work.

## Failed database writes

Failed saves retain compact `.json.gz` recovery envelopes under the run directory.
Preview an explicitly selected directory before applying:

```sh
python recover.py runs/PULL_ID/RUN_ID --db scroopy_dev
python recover.py runs/PULL_ID/RUN_ID --db scroopy_dev --apply
```

Existing differing content is a conflict and is preserved. `--overwrite` is an
explicit choice that can replace newer data; use only after inspection. Successful
recovery restores documents but does not rewrite the historical run report.

## Interrupted runs and locks

HTTP 409 means that pull has an ownership lock. Wait for the active run. If the server
was killed, a lock can remain. Stop all Flask processes using that database, inspect
`pull_locks` in MongoDB, and remove only the `_id` matching the confirmed interrupted
pull. Never clear all locks or remove one while a worker might still be active.
Restart Flask afterward. Documents written before interruption remain available.

## Optional legacy Google Drive endpoint

Install `requirements-drive.txt`, configure an absolute
GOOGLE_APPLICATION_CREDENTIALS path to your own service-account file, and restart
Flask. The service account needs access to the selected folder. Never place its
credential file in the repository. The route requests Drive scope and retains its
legacy listing, storage and response behavior; it is not the recommended starting
point, has no pull grouping and has not been live-tested against Google Drive.

## Known limits

- HTML extraction does not render JavaScript; scanned PDFs need separate OCR.
- PDF discovery examines links on starting pages and can retry fetching those pages.
  Webpage deduplication does not mean one network request across every PDF operation.
- No automatic deletion of stale documents or retention limit for run summaries.
- Crawl reports accumulate in memory. Unlimited pages should be used cautiously.
- Stored run metadata is written before final cleanup; the returned report includes
  the final cleanup outcome. It is not a complete transactional audit log.
- Custom MongoDB database names are for trusted local use, not authorization boundaries.
