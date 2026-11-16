import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import requests
import socket
from PIL import Image
from image_resources import discover_images, ImageCollector
from pull_schema import pull_identity
import test_api_reports


def png(color="red"):
    data = io.BytesIO()
    Image.new("RGB", (3, 2), color).save(data, format="PNG")
    return data.getvalue()


def response(url, body):
    value = requests.Response()
    value.status_code, value.url = 200, url
    value._content, value._content_consumed = body, True
    return value


class ImageTests(unittest.TestCase):
    def test_html_base_and_lazy_placeholder(self):
        refs = discover_images('<base href="/assets/"><img src="placeholder.png" data-src="real.png">', "https://example.org/page")
        self.assertEqual(refs[0]["url"], "https://example.org/assets/real.png")

    def test_private_destination_blocked_before_http(self):
        with tempfile.TemporaryDirectory() as tmp, patch("url_safety.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]), patch("url_safety.requests.Session") as session:
            result = ImageCollector("download", root=tmp).collect([{"url": "http://127.0.0.1/a.png"}])
            self.assertEqual(result[0]["error_type"], "UnsafeURL")
            session.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_discovery_relative_lazy_caption_srcset_and_bad_urls(self):
        refs = discover_images(b'<figure><img src="a.png" alt="diagram"><figcaption>Caption</figcaption></figure>'
            b'<img data-src="/lazy.png"><img srcset="/large.png 800w, /small.png 200w">'
            b'<img src="data:image/png;base64,AAA"><img src="http://[bad">', "https://example.org/posts/")
        self.assertEqual([r["url"] for r in refs], ["https://example.org/posts/a.png", "https://example.org/lazy.png", "https://example.org/small.png"])
        self.assertEqual(refs[0]["caption"], "Caption")
        self.assertEqual(refs[0]["alt"], "diagram")

    def test_reference_mode_never_fetches_or_creates_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch("image_resources.fetch_bounded") as fetch:
            root = Path(tmp) / "images"
            result = ImageCollector("references", root=root).collect([{"url": "https://example.org/a"}])
            self.assertEqual(result[0]["outcome"], "reference")
            fetch.assert_not_called()
            self.assertFalse(root.exists())

    def test_duplicate_urls_and_identical_bytes_use_one_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch("download_limits.request_get", side_effect=lambda url, **kw: response(url, png())) as get:
            collector = ImageCollector("download", root=tmp)
            refs = [{"url": "https://example.org/a", "alt": "one"}, {"url": "https://example.org/a", "alt": "two"}, {"url": "https://example.org/b"}]
            results = collector.collect(refs)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)
            self.assertEqual(results[0]["path"], results[2]["path"])
            self.assertEqual(results[1]["alt"], "two")
            ImageCollector("download", root=tmp).collect(refs[:1])
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_invalid_image_is_rejected_without_leftovers(self):
        with tempfile.TemporaryDirectory() as tmp, patch("download_limits.request_get", side_effect=lambda url, **kw: response(url, b'<html>not an image</html>')):
            result = ImageCollector("download", root=tmp).collect([{"url": "https://example.org/a.png"}])
            self.assertEqual(result[0]["outcome"], "failed")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_limits_clean_partial_and_preserve_old_reference(self):
        url = "https://example.org/a"
        with tempfile.TemporaryDirectory() as tmp, patch("download_limits.request_get", side_effect=lambda url, **kw: response(url, b'x' * (1024 ** 2 + 1))):
            collector = ImageCollector("download", root=tmp, max_file_mb=1, max_images=1)
            result = collector.collect([{"url": url}, {"url": url + "/b"}], [{"url": url, "path": "previous.png", "content_hash": "old"}])
            self.assertEqual(result[0]["error_type"], "DownloadTooLarge")
            self.assertTrue(result[0]["retained_previous"])
            self.assertEqual(result[1]["reason"], "image_count_limit")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_image_defaults_preserve_old_identity(self):
        payload = dict(URLs=["https://example.org/"], download_pdfs=["n"], download_content=["y"], db="test")
        self.assertEqual(pull_identity(payload)[0], pull_identity(dict(payload, image_mode="off", max_image_mb=5, max_images_mb=25, max_images=100))[0])
        self.assertNotEqual(pull_identity(payload)[0], pull_identity(dict(payload, image_mode="references"))[0])


class ImageApiTests(unittest.TestCase):
    setUp = test_api_reports.ApiReportTests.setUp
    post = test_api_reports.ApiReportTests.post

    def test_empty_text_refresh_with_images_preserves_old_text(self):
        self.page.update(outcome="empty", text="", images=[{"url": "https://example.invalid/p.png"}])
        self.payload["image_mode"] = "references"
        collection = self.ns["MongoClient"].return_value.__getitem__.return_value.__getitem__.return_value
        collection.find_one.return_value = {"content": {"content": "Old text", "content_hash": "old-hash",
            "sections": [], "extraction": {"mode": "full"}, "run_id": "old-run"}}
        self.assertEqual(self.post().status_code, 200)
        document = self.ns["save_to_mongodb"].call_args.args[2]
        self.assertEqual(document["content"], "Old text")
        self.assertEqual(document["text_retained_from_run"], "old-run")

    def test_page_references_reach_storage_and_report(self):
        self.page["images"] = [{"url": "https://example.invalid/p.png", "alt": "Example", "position": 0}]
        self.payload["image_mode"] = "references"
        result = self.post()
        self.assertEqual(result.status_code, 200)
        document = self.ns["save_to_mongodb"].call_args.args[2]
        self.assertEqual(document["images"][0]["outcome"], "reference")
        self.assertEqual(result.get_json()["report"]["images"]["received_body_bytes"], 0)

    def test_bad_options_fail_before_database(self):
        for extra in ({"image_mode": "video"}, {"max_images": 0}, {"max_image_mb": True}):
            self.assertEqual(self.post(dict(self.payload, **extra)).status_code, 400)
        self.ns["MongoClient"].assert_not_called()
