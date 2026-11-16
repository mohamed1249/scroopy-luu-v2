"""Bounded response bodies for sequential scraping runs.

Limits count decoded body bytes, not TCP traffic. A rejected final chunk can
overshoot accounting by at most CHUNK_SIZE; it is never retained or written.
"""
from dataclasses import dataclass
import requests
from urllib.parse import urljoin
from url_safety import safe_get as request_get

MIB = 1024 * 1024
CHUNK_SIZE = 64 * 1024


class DownloadTooLarge(requests.exceptions.RequestException):
    pass


class RunDownloadLimitExceeded(requests.exceptions.RequestException):
    pass


@dataclass
class DownloadBudget:
    per_file_bytes: int = 25 * MIB
    total_bytes: int = 100 * MIB
    received_bytes: int = 0

    def __post_init__(self):
        for value in (self.per_file_bytes, self.total_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("Download limits must be positive integers")

    @classmethod
    def from_payload(cls, payload):
        values = [payload.get("max_download_mb", 25), payload.get("max_run_download_mb", 100)]
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("max_download_mb and max_run_download_mb must be positive integers (MiB)")
        return cls(*(value * MIB for value in values))

    def report(self):
        return {"per_file_limit_bytes": self.per_file_bytes, "run_limit_bytes": self.total_bytes,
                "received_body_bytes": self.received_bytes}


def fetch_bounded(url, timeout, *, budget=None, sink=None, request_guard=None):
    """Return a closed response with buffered HTML, or stream directly to sink."""
    budget = budget if budget is not None else DownloadBudget()
    if budget.received_bytes >= budget.total_bytes:
        raise RunDownloadLimitExceeded("Run download budget exhausted")
    # Each redirect gets fresh destination validation and a pinned connection.
    for redirect_count in range(6):
        if request_guard is not None:
            request_guard(url)
        response = request_get(url, timeout=timeout, stream=True, allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308) or not response.headers.get("Location"):
            break
        location = response.headers["Location"]
        response.close()
        if redirect_count == 5:
            raise requests.exceptions.TooManyRedirects("Redirect limit exceeded")
        url = urljoin(url, location)
    try:
        response.raise_for_status()
        # Content-Length describes the encoded body when compression is used.
        # Only compare it to decoded-byte limits for identity encoding.
        if response.headers.get("Content-Encoding", "identity").lower() == "identity":
            try:
                advertised = int(response.headers.get("Content-Length", ""))
            except (ValueError, TypeError):
                advertised = -1
            if advertised > budget.per_file_bytes:
                raise DownloadTooLarge("Advertised response exceeds file limit", response=response)
            if advertised > budget.total_bytes - budget.received_bytes:
                raise RunDownloadLimitExceeded("Advertised response exceeds remaining budget", response=response)
        chunks = []
        file_bytes = 0
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            file_bytes += len(chunk)
            budget.received_bytes += len(chunk)
            if file_bytes > budget.per_file_bytes:
                raise DownloadTooLarge("Response exceeds file limit", response=response)
            if budget.received_bytes > budget.total_bytes:
                raise RunDownloadLimitExceeded("Run download budget exceeded", response=response)
            if sink is None:
                chunks.append(chunk)
            else:
                sink.write(chunk)
        if sink is None:
            response._content = b"".join(chunks)
            response._content_consumed = True
        return response
    finally:
        response.close()


class BudgetWriter:
    """Bound bytes written by chunked clients such as Google Drive."""
    def __init__(self, handle, budget):
        self.handle = handle
        self.budget = budget
        self.file_bytes = 0

    def write(self, chunk):
        self.file_bytes += len(chunk)
        self.budget.received_bytes += len(chunk)
        if self.file_bytes > self.budget.per_file_bytes:
            raise DownloadTooLarge("Response exceeds file limit")
        if self.budget.received_bytes > self.budget.total_bytes:
            raise RunDownloadLimitExceeded("Run download budget exceeded")
        return self.handle.write(chunk)
