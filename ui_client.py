"""Small HTTP client for the local Streamlit interface."""
import requests
from urllib.parse import urlsplit
from settings import resolve_database_name


def make_payload(raw_urls, *, database, mode, pdfs, webpage, subpages,
                 max_pages, max_download_mb, max_run_download_mb, keep_files, include_text, max_levels=2, same_site=True,
                 image_mode="off", max_image_mb=5, max_images_mb=25, max_images=100):
    urls = list(dict.fromkeys(line.strip() for line in raw_urls.splitlines() if line.strip()))
    if not urls:
        raise ValueError("Add at least one URL, one per line.")
    for url in urls:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
                raise ValueError()
        except ValueError:
            raise ValueError("Use complete http:// or https:// URLs without embedded credentials.") from None
    database = resolve_database_name(database)
    if not webpage and not pdfs:
        raise ValueError("Choose webpage text, linked PDFs, or both.")
    payload = {"URLs": urls, "download_content": ["y" if webpage else "n"] * len(urls),
            "download_pdfs": ["y" if pdfs else "n"] * len(urls), "db": database,
            "extraction_mode": mode, "include_subpages": bool(subpages and webpage),
            "max_pages": None if max_pages is None else int(max_pages),
            "max_levels": int(max_levels), "same_site": same_site, "max_download_mb": int(max_download_mb),
            "max_run_download_mb": int(max_run_download_mb), "keep_work_files": keep_files,
            "include_text": include_text}
    if image_mode != "off":
        payload.update(image_mode=image_mode, max_image_mb=int(max_image_mb),
                       max_images_mb=int(max_images_mb), max_images=int(max_images))
    return payload


def submit_scrape(payload):
    # Requests stay on the local Flask service.
    try:
        response = requests.post("http://127.0.0.1:8080/scrape", json=payload,
                                 timeout=(5, 600))
    except requests.exceptions.Timeout:
        raise RuntimeError("The API response timed out. The server may still be working; check its terminal before retrying.") from None
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Cannot reach Flask. Start it on 127.0.0.1:8080, then try again.") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        raise RuntimeError("The API returned an unexpected response.")
    if response.status_code >= 400 and "report" not in body:
        message = body.get("error", "Request failed")
        kind = body.get("error_type")
        raise RuntimeError(f"{response.status_code}: {message}" + (f" ({kind})" if kind else ""))
    if "report" not in body:
        raise RuntimeError("The API response did not include a scrape report.")
    return response.status_code, body
