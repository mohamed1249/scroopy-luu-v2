"""Breadth-first, run-wide URL deduplication for bounded-depth crawling."""
from collections import deque
from urllib.parse import urlsplit, urlunsplit, urljoin
import re
import requests
from bs4 import BeautifulSoup


class DuplicateCrawlURL(requests.exceptions.RequestException):
    pass


class OutsideCrawlSite(requests.exceptions.RequestException):
    pass


def canonical_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parts.port
    if port is not None and (parts.scheme.lower(), port) not in (("https", 443), ("http", 80)):
        host += ":" + str(port)
    if parts.username is not None:
        # Preserve credentials so the transport can reject them, never erase them.
        host = parts.netloc
    def percent(match):
        char = chr(int(match.group()[1:], 16))
        return char if char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~" else match.group().upper()
    path = re.sub(r"%[0-9a-fA-F]{2}", percent, parts.path or "/")
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def crawl_pages(urls, timeout, *, mode, main_content, max_pages, max_levels, same_site, budget, collect_images=False):
    from scr_prp import _scrape_page_result, summarize_page_reports, _validate_mode
    from download_limits import DownloadBudget
    _validate_mode(mode)
    if max_pages is not None and (type(max_pages) is not int or max_pages < 1):
        raise ValueError("max_pages must be a positive integer or null")
    if type(max_levels) is not int or max_levels < 1:
        raise ValueError("max_levels must be a positive integer")
    if type(same_site) is not bool:
        raise ValueError("same_site must be boolean")
    seeds = [urls] if isinstance(urls, str) else urls
    budget = budget if budget is not None else DownloadBudget()
    queue, discovered, fetched = deque(), set(), set()
    allowed_hosts = {urlsplit(url).hostname for url in seeds}
    duplicates = 0
    for url in seeds:
        key = canonical_url(url)
        if key in discovered:
            duplicates += 1
            continue
        discovered.add(key)
        queue.append((key, None, 0, url))
    pages = []
    attempted = 0

    def guard(url):
        key = canonical_url(url)
        if same_site and urlsplit(url).hostname not in allowed_hosts:
            raise OutsideCrawlSite("Redirect leaves the starting websites")
        if key in fetched:
            raise DuplicateCrawlURL("URL already fetched in this run")
        fetched.add(key)

    while queue:
        url, parent, depth, seed = queue.popleft()
        if url in fetched:
            duplicates += 1
            continue
        scheme = urlsplit(url).scheme
        reason = None
        if scheme not in ("http", "https"):
            reason = "unsupported_scheme"
        elif same_site and urlsplit(url).hostname not in allowed_hosts:
            reason = "outside_site"
        elif max_pages is not None and attempted >= max_pages:
            reason = "page_limit"
        elif budget.received_bytes >= budget.total_bytes:
            reason = "run_download_budget_exhausted"
        if reason:
            page = {"url": url, "outcome": "skipped", "skip_reason": reason,
                    "text": None, "character_count": 0, "http_status": None,
                    "selected_region": None, "requested_mode": mode,
                    "duration_ms": 0, "final_url": None, "error": None}
        else:
            page, response = _scrape_page_result(url, timeout, mode=mode, budget=budget, request_guard=guard,
                                               collect_images=collect_images)
            attempted += 1
            if response is not None and page["outcome"] in ("success", "empty"):
                base = response.url or url
                fetched.add(canonical_url(base))
                if depth + 1 < max_levels:
                    soup = BeautifulSoup(response.content, "html.parser")
                    for anchor in soup.find_all("a", href=True):
                        try:
                            target = canonical_url(urljoin(base, anchor["href"]))
                        except ValueError:
                            continue
                        if target in discovered or target in fetched:
                            duplicates += 1
                            continue
                        discovered.add(target)
                        queue.append((target, url, depth + 1, seed))
        page.update(parent_url=parent, depth=depth, level=depth + 1, seed_url=seed,
                    content_included=page["outcome"] in ("success", "empty") and (depth > 0 or main_content))
        if depth == 0 and not main_content:
            page["text"] = None
        pages.append(page)
    return {"pages": pages, "summary": summarize_page_reports(pages),
            "crawl": {"max_levels": max_levels, "max_pages": max_pages, "same_site": same_site,
                      "duplicate_links_avoided": duplicates}}
