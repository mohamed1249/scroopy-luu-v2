from crawler import canonical_url, DuplicateCrawlURL, OutsideCrawlSite
from image_resources import ImageCollector, discover_images
from pull_schema import pull_identity, source_document, pdf_document, document_key, utc_now
import os
from storage import store_document, database_preflight
from pathlib import Path
from run_workspace import RunWorkspace, safe_component
import re
import requests
from download_limits import DownloadBudget, BudgetWriter, RunDownloadLimitExceeded, fetch_bounded
from pymongo.errors import DuplicateKeyError, WriteConcernError
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote, urldefrag, urlsplit
import json
from time import perf_counter
from pymongo import MongoClient
import uuid
from PyPDF2 import PdfReader


def clean_title(title):
    return re.sub(r'[<>:"/\\|?*]', '_', title)


class UnsupportedContentType(requests.exceptions.RequestException):
    pass


def _check_page_type(response):
    media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if ((media and media not in ("text/html", "application/xhtml+xml", "text/plain"))
            or response.content.lstrip().startswith(b"%PDF-")):
        raise UnsupportedContentType("Response is not a supported webpage; use PDF extraction for PDFs")

def scrape_page_content(url, timeout=(5, 20), *, mode="full", budget=None):
    """Extract plain text, preserving repetition and normalizing whitespace.

    full: use the whole parsed document (not raw HTML or rendered JS).
    main: prefer the first main element, then a single article, then body,
        falling back to the document. This heuristic does not guarantee size.
    """
    _validate_mode(mode)
    response = fetch_bounded(url, timeout, budget=budget)
    response.raise_for_status()
    if response.status_code == 200:
        _check_page_type(response)
        return _extract_page_text(response.content, mode)[0]
    return None


def _validate_mode(mode):
    if mode not in ("full", "main"):
        raise ValueError("mode must be 'full' or 'main'")


def _extract_page_text(html, mode, *, with_sections=False):
    soup = BeautifulSoup(html, 'html.parser')
    content_root, selected_region = soup, "document"
    if mode == "main":
        content_root = soup.find("main")
        selected_region = "main"
        if content_root is None:
            articles = soup.find_all("article")
            if len(articles) == 1:
                content_root, selected_region = articles[0], "article"
            elif soup.body is not None:
                content_root, selected_region = soup.body, "body"
            else:
                content_root, selected_region = soup, "document"

    content = content_root.get_text(separator="\n", strip=True)
    text = "\n".join(
        re.sub(r"[ \t]+", " ", line).strip()
        for line in content.splitlines() if line.strip()
    )
    if not with_sections:
        return text, selected_region
    sections = []
    element_ids = {id(tag): index for index, tag in enumerate(content_root.find_all(True))}
    cursor = 0
    for node in content_root.find_all(string=True):
        if node.__class__.__name__ not in ("NavigableString", "CData"):
            continue
        normalized = "\n".join(re.sub(r"[ \t]+", " ", line).strip()
                               for line in str(node).strip().splitlines() if line.strip())
        if not normalized:
            continue
        start = text.find(normalized, cursor)
        if start < 0:
            continue
        parent = node.parent
        tags = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "th", "td", "pre"}
        while parent is not None and parent.name not in tags and parent is not content_root:
            parent = parent.parent
        tag = parent.name if parent is not None and parent.name in tags else "text"
        section = {"type": tag, "start": start, "end": start + len(normalized)}
        if parent is not None:
            section["element_id"] = element_ids.get(id(parent))
            if tag in ("th", "td"):
                section.update(table_id=element_ids.get(id(parent.find_parent("table"))),
                               row_id=element_ids.get(id(parent.find_parent("tr"))),
                               colspan=parent.get("colspan", "1"), rowspan=parent.get("rowspan", "1"))
        sections.append(section)
        cursor = start + len(normalized)
    return text, selected_region, sections


def scrape_page_report(url, timeout=(5, 20), *, mode="full", budget=None, collect_images=False):
    """Return a JSON-serializable single-page result; do not write files.

    Request failures become report data. Invalid modes and programming errors
    still raise. Duration covers fetching and extraction, not an overall deadline.
    Empty extracted text is reported separately from a request failure.
    """
    return _scrape_page_result(url, timeout, mode=mode, budget=budget, collect_images=collect_images)[0]


def _scrape_page_result(url, timeout, *, mode, budget=None, request_guard=None, collect_images=False):
    _validate_mode(mode)
    started = perf_counter()
    response = None
    report = {
        "url": url,
        "final_url": None,
        "outcome": "failed",
        "http_status": None,
        "requested_mode": mode,
        "selected_region": None,
        "duration_ms": None,
        "character_count": 0,
        "text": None,
        "error": None,
    }
    try:
        response = fetch_bounded(url, timeout, budget=budget, request_guard=request_guard)
        report["http_status"] = response.status_code
        report["final_url"] = response.url
        response.raise_for_status()
        if response.status_code != 200:
            report["error"] = {
                "type": "UnexpectedStatus",
                "message": f"Expected HTTP 200, received {response.status_code}.",
            }
        else:
            _check_page_type(response)
            text, region, sections = _extract_page_text(response.content, mode, with_sections=True)
            if collect_images:
                report["images"] = discover_images(response.content, response.url)
            report.update(
                outcome="success" if text else "empty",
                selected_region=region,
                character_count=len(text),
                text=text,
                sections=sections,
                collected_at=utc_now(),
            )
    except requests.exceptions.RequestException as exc:
        if isinstance(exc, RunDownloadLimitExceeded) and exc.response is None:
            report["outcome"] = "skipped"
            report["skip_reason"] = "run_download_budget_exhausted"
        if exc.response is not None:
            report["http_status"] = exc.response.status_code
            report["final_url"] = exc.response.url
        report["error"] = {
            "type": type(exc).__name__,
            "message": "Page request failed; see error type and HTTP status.",
        }
    if report["error"] and report["error"]["type"] in ("DuplicateCrawlURL", "OutsideCrawlSite"):
        reason = "duplicate_redirect" if report["error"]["type"] == "DuplicateCrawlURL" else "outside_site"
        report.update(outcome="skipped", skip_reason=reason, error=None)
    report["duration_ms"] = round((perf_counter() - started) * 1000, 3)
    return report, response


def summarize_page_reports(pages):
    """Attempted counts network attempts, including empty and failed results."""
    counts = {outcome: sum(p["outcome"] == outcome for p in pages)
              for outcome in ("success", "empty", "failed", "skipped")}
    return {"attempted": len(pages) - counts["skipped"], **counts}


def scrape_page_and_subpages_report(url, timeout=(5, 20), *, mode="full",
                                    main_content=True, max_pages=20, budget=None,
                                    max_levels=2, same_site=True, collect_images=False):
    """Breadth-first crawl; level 1 is the seed. None means no page-count cap.

    Accepts one URL or a list of starting URLs sharing one visited set and cap.
    Download budgets and URL safety checks remain active in unlimited mode.
    """
    from crawler import crawl_pages
    return crawl_pages(url, timeout, mode=mode, main_content=main_content,
                       max_pages=max_pages, max_levels=max_levels, same_site=same_site, budget=budget,
                       collect_images=collect_images)


def save_seprated(dct,path):
    for key, value in dct.items():
        sub_dict = {'URL':key, 'Content': value['content']}
        cleaned_title = clean_title(key)
        json_filename = f"{cleaned_title}.json"
        json_path = os.path.join(path, json_filename)
        with open(json_path, "w", encoding="utf-8") as json_file:
            json.dump(sub_dict, json_file, ensure_ascii=False, indent=4)

def scrape_page_and_subpages_content(url, main_content=True, timeout=30, *,
                                     mode="full", max_pages=20, return_report=False, budget=None, max_levels=2, same_site=True):
    """Use return_report=True for JSON-ready results, including failures.

    The default retains the legacy (content, link_count) return shape.
    Crawling uses max_levels and max_pages total page fetches; None removes the page cap.
    """
    report = scrape_page_and_subpages_report(
        url, timeout, mode=mode, main_content=main_content, max_pages=max_pages, budget=budget,
        max_levels=max_levels, same_site=same_site
    )
    if return_report:
        return report
    if report["pages"][0]["outcome"] not in ("success", "empty"):
        return None
    content = {
        page["url"]: {"content": page["text"], "main_content": page["depth"] == 0,
                      "link_no": index}
        for index, page in enumerate(report["pages"])
        if page["outcome"] == "success" and page["content_included"]
    }
    return content, len(report["pages"]) - 1


def download_pdfs_from_url(url, *, output_dir="PDFs", timeout=(5, 20), events=None, budget=None):
    """Keep the legacy filename-to-URL result; optionally collect per-link events.

    As before, all HTTP(S) links are PDF candidates, even without a .pdf suffix.
    Invalid files are removed only from this call's explicit output directory.
    """
    budget = budget if budget is not None else DownloadBudget()
    events = events if events is not None else []
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdfs = {}
    try:
        response = fetch_bounded(url, timeout, budget=budget)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Unexpected HTTP status")
    except (requests.exceptions.RequestException, ValueError) as exc:
        events.append({"stage": "pdf_discovery", "url": url, "outcome": "failed",
                       "error_type": type(exc).__name__})
        return None
    events.append({"stage": "pdf_discovery", "url": url, "outcome": "success",
                   "http_status": response.status_code})
    soup = BeautifulSoup(response.content, "html.parser")
    seen = set()
    for index, link in enumerate(soup.find_all("a", href=True)):
        try:
            target = urldefrag(urljoin(response.url or url, link["href"]))[0]
            scheme = urlsplit(target).scheme
        except ValueError:
            events.append({"stage": "pdf_link", "parent_url": url, "url": link["href"],
                           "outcome": "failed", "http_status": None, "file": None,
                           "error_type": "InvalidURL"})
            continue
        event = {"stage": "pdf_link", "parent_url": url, "url": target,
                 "outcome": "failed", "http_status": None, "file": None, "error_type": None}
        events.append(event)
        if target in seen:
            event.update(outcome="skipped", reason="duplicate_link")
            continue
        seen.add(target)
        if scheme not in ("http", "https"):
            event.update(outcome="skipped", reason="unsupported_scheme")
            continue
        filename = None
        try:
            filename = download_pdf(target, link.get_text(), index,
                                    output_dir=output_dir, timeout=timeout, event=event, budget=budget)
            if not is_valid_pdf(output_dir / filename):
                event.update(error_type="InvalidPDF", reason="unreadable_or_zero_pages")
                (output_dir / filename).unlink(missing_ok=True)
                continue
            pdfs[filename] = target
            event.update(outcome="success", file=filename)
        except (requests.exceptions.RequestException, OSError, ValueError) as exc:
            event["error_type"] = type(exc).__name__
            if filename is not None:
                (output_dir / filename).unlink(missing_ok=True)
    return pdfs


def make_valid_filename(url):
    return safe_component(url)


def download_pdf(url, title, i, *, output_dir="PDFs", timeout=(5, 20), event=None, budget=None):
    filename = f"{i:04d}-{safe_component(title or url)}-{uuid.uuid4().hex[:12]}.pdf"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / filename
    created = False
    try:
        with target.open("xb") as handle:
            created = True
            response = fetch_bounded(url, timeout, budget=budget, sink=handle)
            if event is not None:
                event["http_status"] = response.status_code
            if response.status_code != 200:
                raise ValueError("Expected HTTP 200 for PDF download")
    except (OSError, ValueError, requests.exceptions.RequestException) as exc:
        if event is not None and getattr(exc, "response", None) is not None:
            event["http_status"] = exc.response.status_code
        if created:
            target.unlink(missing_ok=True)
        raise
    return filename


def is_valid_pdf(pdf_path):
    try:
        with open(pdf_path, 'rb') as pdf_file:
            pdf_reader = PdfReader(pdf_file)
            return len(pdf_reader.pages) > 0
    except Exception:
        return False





from PyPDF2 import PdfReader

def extract_pdf_pages(pdf_file_path):
    pdf_pages = []
    with open(pdf_file_path, 'rb') as pdf_file:
        reader = PdfReader(pdf_file)
        for page_number in range(len(reader.pages)):
            pdf_page_info = {
                'information': reader.pages[page_number].extract_text(),
                'source_file_name': os.path.basename(pdf_file_path),
                'page_number': page_number + 1
            }
            pdf_pages.append(pdf_page_info)
    return pdf_pages




def save_to_mongodb(collection, file_name, content):
    """Preserve identity and enforce uniqueness before upserting.

    Existing duplicates make index creation fail rather than deleting user data.
    A concurrent upsert winner may require retrying as an update.
    """
    collection.create_index("file_name", unique=True)
    try:
        result = collection.update_one({"file_name": file_name}, {"$set": {"content": content}}, upsert=True)
    except DuplicateKeyError:
        result = collection.update_one({"file_name": file_name}, {"$set": {"content": content}}, upsert=False)
        if not result.matched_count:
            raise
    if result is None or result.acknowledged is False:
        raise WriteConcernError("Database did not acknowledge the save")






def delete_files_in_current_directory(file_extensions=None):
    current_directory = os.getcwd()  # Get the current working directory

    if file_extensions is None:
        file_extensions = []  # Delete all files

    for filename in os.listdir(current_directory):
        if any(filename.lower().endswith(ext.lower()) for ext in file_extensions):
            try:
                os.remove(filename)
                print(f"Deleted: {filename}")
            except Exception as e:
                print(f"Error deleting {filename}: {e}")
