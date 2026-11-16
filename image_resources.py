"""Image references and bounded, content-addressed local image storage."""
import hashlib
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from PIL import Image
from crawler import canonical_url
from download_limits import DownloadBudget, fetch_bounded


def discover_images(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    base = soup.find("base", href=True)
    try:
        image_base = urljoin(page_url, base["href"]) if base else page_url
    except ValueError:
        image_base = page_url
    images = []
    for index, tag in enumerate(soup.find_all("img", limit=100)):
        src = tag.get("data-src") or tag.get("data-lazy-src") or tag.get("src", "")
        if not src:
            candidates = []
            for candidate in (tag.get("srcset") or "").split(","):
                parts = candidate.strip().split()
                if parts:
                    try:
                        size = float(parts[1][:-1]) if len(parts) > 1 else 1
                    except ValueError:
                        continue
                    candidates.append((size, parts[0]))
            if candidates:
                src = min(candidates)[1]
        try:
            url = urljoin(image_base, src)
            if not src or urlsplit(url).scheme not in ("http", "https"):
                continue
            url = canonical_url(url)
        except ValueError:
            continue
        figure = tag.find_parent("figure")
        caption = figure.find("figcaption") if figure else None
        images.append({"url": url, "position": index, "alt": str(tag.get("alt", ""))[:2000],
                       "caption": caption.get_text(" ", strip=True)[:2000] if caption else ""})
    return images


class ImageCollector:
    def __init__(self, mode="off", *, max_file_mb=5, max_total_mb=25, max_images=100,
                 root="resources/images"):
        self.mode, self.root = mode, Path(root)
        self.budget = DownloadBudget(max_file_mb * 1024 ** 2, max_total_mb * 1024 ** 2)
        self.max_images, self.cache = max_images, {}

    def collect(self, references, previous=()):
        old = {item["url"]: item for item in previous if isinstance(item, dict) and "url" in item}
        results = []
        for ref in references:
            url = ref["url"]
            if self.mode == "references":
                result = {"outcome": "reference"}
            elif url in self.cache:
                result = self.cache[url]
            elif len(self.cache) >= self.max_images:
                result = {"outcome": "skipped", "reason": "image_count_limit"}
            else:
                try:
                    result = self._download(url)
                except Exception as exc:
                    result = {"outcome": "failed", "error_type": type(exc).__name__}
                self.cache[url] = result
            item = {**ref, **result}
            if result["outcome"] in ("failed", "skipped") and old.get(url, {}).get("path"):
                retained = old[url]
                # Keep a previously verified local resource on a failed refresh.
                item.update({key: retained[key] for key in ("path", "content_hash", "bytes", "format", "width", "height") if key in retained})
                item["retained_previous"] = True
            results.append(item)
        return results

    def _download(self, url):
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.root, suffix=".part", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                response = fetch_bounded(url, (5, 20), budget=self.budget, sink=handle)
            except BaseException:
                handle.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            with Image.open(temporary) as picture:
                kind, width, height = picture.format, picture.width, picture.height
                if kind not in ("JPEG", "PNG", "WEBP", "GIF") or width * height > 40_000_000:
                    raise ValueError("Unsupported image format or excessive dimensions")
                picture.verify()
            with temporary.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}[kind]
            target = self.root / f"{digest}.{extension}"
            size = temporary.stat().st_size
            # Same content, even at different URLs/runs, has one permanent file.
            if not target.exists():
                temporary.replace(target)
            return {"outcome": "downloaded", "final_url": response.url, "content_hash": digest,
                    "path": target.as_posix(), "bytes": size, "format": kind,
                    "width": width, "height": height}
        finally:
            temporary.unlink(missing_ok=True)

    def report(self):
        return {"mode": self.mode, "unique_download_attempts": len(self.cache),
                **self.budget.report()}
