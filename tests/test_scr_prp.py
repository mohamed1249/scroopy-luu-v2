import unittest
import json
from unittest.mock import patch

import requests

from scr_prp import (scrape_page_content, scrape_page_report,
                     scrape_page_and_subpages_content, scrape_page_and_subpages_report)

class ScrapePageContentTests(unittest.TestCase):
    def setUp(self):
        self.url = "https://example.invalid/page"
        self.response = requests.Response()
        self.response._content_consumed = True
        self.response.status_code = 200
        self.response.url = self.url
        self.response._content = b"<div>Test Content</div>"

        # Replace only network access; keep real HTTP status handling.
        patcher = patch("download_limits.request_get", return_value=self.response)
        self.mock_get = patcher.start()
        self.addCleanup(patcher.stop)

    def test_subpages_report_partial_failure_and_limits(self):
        self.response._content = (
            b'<main>Seed</main><a href="/good">Good</a>'
            b'<a href="/good#part">Duplicate</a><a href="/bad">Bad</a>'
            b'<a href="mailto:test@example.invalid">Mail</a><a href="/extra">Extra</a>'
        )
        child = requests.Response()
        child._content_consumed = True
        child.status_code = 200
        child.url = "https://example.invalid/good"
        child._content = b'<main>Child</main><a href="/grandchild">No recursion</a>'
        self.mock_get.side_effect = [self.response, child, requests.exceptions.Timeout()]
        report = scrape_page_and_subpages_report(self.url, mode="main", max_pages=3)
        self.assertEqual(report["summary"], {
            "attempted": 3, "success": 2, "empty": 0, "failed": 1, "skipped": 2})
        self.assertEqual(self.mock_get.call_count, 3)
        self.assertEqual(report["pages"][1]["text"], "Child")
        self.assertEqual(report["pages"][1]["parent_url"], self.url)
        self.assertEqual(report["pages"][1]["depth"], 1)
        self.assertEqual(report["pages"][2]["error"]["type"], "Timeout")
        self.assertEqual(report["pages"][3]["skip_reason"], "unsupported_scheme")
        self.assertEqual(report["pages"][4]["skip_reason"], "page_limit")
        json.dumps(report)

    def test_subpages_seed_failure_stops_discovery(self):
        self.mock_get.side_effect = requests.exceptions.ConnectionError()
        report = scrape_page_and_subpages_report(self.url)
        self.assertEqual(report["summary"]["failed"], 1)
        self.mock_get.assert_called_once()

    def test_subpages_legacy_shape_and_report_option(self):
        content, count = scrape_page_and_subpages_content(self.url)
        self.assertEqual(content[self.url]["content"], "Test Content")
        self.assertEqual(count, 0)
        report = scrape_page_and_subpages_content(self.url, return_report=True,
                                                 main_content=False)
        self.assertIsNone(report["pages"][0]["text"])
        self.assertEqual(report["summary"]["attempted"], 1)

    def test_subpages_rejects_invalid_limit_before_request(self):
        for limit in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                scrape_page_and_subpages_report(self.url, max_pages=limit)
        self.mock_get.assert_not_called()

    def test_success_extracts_text(self):
        content = scrape_page_content(self.url)
        self.assertEqual(content, "Test Content")

    def test_report_is_json_serializable_with_measured_duration(self):
        self.response.url = "https://example.invalid/final"
        with patch("scr_prp.perf_counter", side_effect=[10.0, 10.25]):
            report = scrape_page_report(self.url, timeout=(1, 2))
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertEqual(report["outcome"], "success")
        self.assertEqual(report["text"], "Test Content")
        self.assertEqual(report["character_count"], len("Test Content"))
        self.assertEqual(report["selected_region"], "document")
        self.assertEqual(report["requested_mode"], "full")
        self.assertEqual(report["http_status"], 200)
        self.assertEqual(report["final_url"], self.response.url)
        self.assertEqual(report["duration_ms"], 250.0)
        self.assertIsNone(report["error"])
        self.mock_get.assert_called_once_with(self.url, timeout=(1, 2), stream=True, allow_redirects=False)

    def test_report_records_actual_region(self):
        for html, region in [
            (b"<main>Text</main>", "main"),
            (b"<article>Text</article>", "article"),
            (b"<body>Text</body>", "body"),
            (b"<p>Text</p>", "document"),
        ]:
            with self.subTest(region=region):
                self.response._content = html
                report = scrape_page_report(self.url, mode="main")
                self.assertEqual(report["selected_region"], region)
                self.assertEqual(report["requested_mode"], "main")

    def test_report_distinguishes_empty_text(self):
        self.response._content = b"<main></main>"
        report = scrape_page_report(self.url, mode="main")
        self.assertEqual(report["outcome"], "empty")
        self.assertEqual(report["text"], "")
        self.assertEqual(report["character_count"], 0)
        self.assertIsNone(report["error"])

    def test_report_records_http_failure_and_unexpected_status(self):
        for status, error_type in [(404, "HTTPError"), (503, "HTTPError"),
                                   (204, "UnexpectedStatus")]:
            with self.subTest(status=status):
                self.response.status_code = status
                report = scrape_page_report(self.url)
                self.assertEqual(report["outcome"], "failed")
                self.assertEqual(report["http_status"], status)
                self.assertEqual(report["error"]["type"], error_type)
                self.assertIsNone(report["text"])
                self.assertIsNone(report["selected_region"])
                json.dumps(report)

    def test_report_records_network_failures(self):
        for error_type in (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            with self.subTest(error_type=error_type):
                self.mock_get.side_effect = error_type("sensitive exception detail")
                report = scrape_page_report(self.url)
                self.assertEqual(report["outcome"], "failed")
                self.assertEqual(report["error"]["type"], error_type.__name__)
                self.assertIsNone(report["http_status"])
                self.assertNotIn("sensitive exception detail", json.dumps(report))

    def test_report_rejects_invalid_mode_before_request(self):
        with self.assertRaises(ValueError):
            scrape_page_report(self.url, mode="invalid")
        self.mock_get.assert_not_called()

    def test_default_timeout_is_forwarded(self):
        scrape_page_content(self.url)
        self.mock_get.assert_called_once_with(self.url, timeout=(5, 20), stream=True, allow_redirects=False)

    def test_extracts_text_outside_divs(self):
        self.response._content = (
            b"<h1>Useful title</h1><p>Important introduction.</p>"
            b"<div>Inside the div.</div>"
        )
        self.assertEqual(
            scrape_page_content(self.url),
            "Useful title\nImportant introduction.\nInside the div.",
        )

    def test_separates_adjacent_paragraphs(self):
        self.response._content = (
            b"<div><p>First paragraph.</p><p>Second paragraph.</p></div>"
        )
        self.assertEqual(
            scrape_page_content(self.url),
            "First paragraph.\nSecond paragraph.",
        )

    def test_custom_timeout_is_forwarded(self):
        scrape_page_content(self.url, timeout=(1, 2))
        self.mock_get.assert_called_once_with(self.url, timeout=(1, 2), stream=True, allow_redirects=False)

    def test_full_preserves_surrounding_content_and_repetition(self):
        self.response._content = (
            b"<body><nav>Home</nav><main><p>Repeat</p><p>Repeat</p>"
            b"</main><footer>Contact</footer></body>"
        )
        expected = "Home\nRepeat\nRepeat\nContact"
        self.assertEqual(scrape_page_content(self.url), expected)
        self.assertEqual(scrape_page_content(self.url, mode="full"), expected)

    def test_main_selection_and_fallbacks(self):
        cases = [
            ("main wins", "<body><nav>Home</nav><article>Other</article>"
             "<main><p>Repeat</p><p>Repeat</p></main></body>", "Repeat\nRepeat"),
            ("single article", "<body><nav>Home</nav><article>Story</article>"
             "<footer>Contact</footer></body>", "Story"),
            ("multiple articles", "<body><nav>Home</nav><article>One</article>"
             "<article>Two</article></body>", "Home\nOne\nTwo"),
            ("body", "<html><head><title>Metadata</title></head>"
             "<body><p>Body text</p></body></html>", "Body text"),
            ("document", "<p>Fragment</p><p>More text</p>", "Fragment\nMore text"),
            ("empty main", "<body>Outside<main></main></body>", ""),
        ]
        for name, html, expected in cases:
            with self.subTest(name=name):
                self.response._content = html.encode("utf-8")
                self.assertEqual(scrape_page_content(self.url, mode="main"), expected)

    def test_invalid_mode_is_rejected_before_network_access(self):
        for mode in ("unknown", "FULL", None, [], 1):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "mode must be"):
                    scrape_page_content(self.url, mode=mode)
        self.mock_get.assert_not_called()

    def test_404_raises_http_error(self):
        self.response.status_code = 404
        with self.assertRaises(requests.exceptions.HTTPError):
            scrape_page_content(self.url)

    def test_503_raises_http_error(self):
        self.response.status_code = 503
        with self.assertRaises(requests.exceptions.HTTPError):
            scrape_page_content(self.url)

    def test_network_timeout_propagates(self):
        self.mock_get.side_effect = requests.exceptions.Timeout("simulated timeout")
        with self.assertRaises(requests.exceptions.Timeout):
            scrape_page_content(self.url)


if __name__ == "__main__":
    unittest.main()
