# Contributing

Install `requirements-dev.txt` in a virtual environment and run
`python -B -m unittest discover -s tests -q` before submitting a pull request.
Optional database checks use only generated local databases. Never use a production
database or real credentials in tests.

Keep full/repeated text available, filtering opt-in, and partial refreshes safe.
Add regression tests that demonstrate the failure your change fixes. Keep extraction,
storage and future dataset preparation separate. Do not commit scraped datasets,
API keys, credential files, logs, or personal documents.

Describe the concrete problem, changed behavior and validation in each pull request.
For schema changes, document compatibility and recovery behavior in docs/PULL_STORAGE.md.
