"""Versioned source records and deterministic identities for saved pulls."""
from datetime import datetime, timezone
import hashlib
import json
from crawler import canonical_url


class EmptyPDFText(ValueError):
    """No extractable text: do not replace an earlier successful document."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pull_identity(payload):
    settings = dict(extraction_mode="full", include_subpages=False, max_pages=20,
                    max_levels=2, same_site=True, keep_work_files=False,
                    include_text=True, max_download_mb=25, max_run_download_mb=100)
    settings.update(payload)
    # Off with default limits preserves pre-image pull identities.
    image_defaults = dict(image_mode="off", max_image_mb=5, max_images_mb=25, max_images=100)
    if any(payload.get(key, default) != default for key, default in image_defaults.items()):
        for key, default in image_defaults.items():
            settings.setdefault(key, default)
    else:
        for key in image_defaults:
            settings.pop(key, None)
    # Keep seed order: it affects which pages a bounded breadth-first crawl visits.
    settings["URLs"] = [canonical_url(url) for url in settings["URLs"]]
    for key in ("download_pdfs", "download_content"):
        settings[key] = ["n" if flag.lower() == "n" else "y" for flag in settings[key]]
    encoded = json.dumps(settings, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return digest(encoded), settings


def source_document(url, text, *, pull_id, run_id, kind="webpage", sections=None,
                    extraction=None, final_url=None, parent_url=None, collected_at=None):
    source_id = digest(kind + ":" + canonical_url(url))
    return {"schema_version": 1, "source_id": source_id, "pull_id": pull_id,
            "run_id": run_id, "source_type": kind, "webpage_url": url,
            "final_url": final_url, "parent_url": parent_url,
            "collected_at": collected_at or utc_now(), "content": text,
            "content_hash": digest(text), "sections": sections or [],
            "extraction": {"version": "1", **(extraction or {})}}


def pdf_document(url, pages, **kwargs):
    if not any((page.get("information") or "").strip() for page in pages):
        raise EmptyPDFText("PDF contains no extractable text; OCR may be needed")
    parts, sections, offset = [], [], 0
    for index, page in enumerate(pages):
        text = page.get("information") or ""
        if index:
            offset += 1
        sections.append({"type": "pdf_page", "page_number": page.get("page_number", index + 1),
                         "start": offset, "end": offset + len(text)})
        parts.append(text)
        offset += len(text)
    return source_document(url, "\n".join(parts), kind="pdf", sections=sections,
                           extraction={"method": "PyPDF2", "ocr": False}, **kwargs)


def document_key(document):
    return document["pull_id"] + ":" + document["source_id"]
