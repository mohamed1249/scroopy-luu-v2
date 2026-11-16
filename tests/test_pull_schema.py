import unittest
from unittest.mock import MagicMock
from pymongo.errors import DuplicateKeyError
from pull_schema import pull_identity, source_document, pdf_document, document_key
from scr_prp import _extract_page_text
import test_api_reports


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.payload = dict(URLs=["https://example.org/"], download_pdfs=["n"],
                            download_content=["y"], db="test")

    def test_defaults_order_and_every_setting(self):
        identity, settings = pull_identity(self.payload)
        self.assertEqual(identity, pull_identity(dict(reversed(list(settings.items()))))[0])
        for key, value in dict(extraction_mode="main", include_subpages=True, max_pages=None,
                max_levels=3, same_site=False, keep_work_files=True, include_text=False,
                max_download_mb=26, max_run_download_mb=101, db="other",
                download_pdfs=["y"], download_content=["n"]).items():
            with self.subTest(key=key):
                self.assertNotEqual(identity, pull_identity(dict(self.payload, **{key: value}))[0])

    def test_identity_survives_refresh_and_avoids_filename_collisions(self):
        a = source_document("https://example.org/a?b", "old", pull_id="p", run_id="1")
        b = source_document("https://example.org/a?b", "new", pull_id="p", run_id="2")
        c = source_document("https://example.org/a/b", "old", pull_id="p", run_id="1")
        self.assertEqual(document_key(a), document_key(b))
        self.assertNotEqual(a["content_hash"], b["content_hash"])
        self.assertNotEqual(document_key(a), document_key(c))

    def test_ordered_html_offsets_preserve_repetition_unicode_and_types(self):
        html = '<h1>Title</h1><p>Hello <b>world</b></p><p>Hello</p><table><tr><th>A</th><td>مرحبا</td></tr></table>'
        text, _, sections = _extract_page_text(html, "full", with_sections=True)
        self.assertEqual(text, _extract_page_text(html, "full")[0])
        self.assertEqual([text[s["start"]:s["end"]] for s in sections],
                         ["Title", "Hello", "world", "Hello", "A", "مرحبا"])
        self.assertEqual([s["type"] for s in sections], ["h1", "p", "p", "p", "th", "td"])

    def test_pdf_empty_page_keeps_page_order(self):
        doc = pdf_document("https://example.org/a.pdf", [{"information": "one"},
            {"information": None}, {"information": "three"}], pull_id="p", run_id="r")
        self.assertEqual([doc["content"][s["start"]:s["end"]] for s in doc["sections"]],
                         ["one", "", "three"])
        self.assertEqual([s["page_number"] for s in doc["sections"]], [1, 2, 3])


class PullRefreshTests(unittest.TestCase):
    setUp = test_api_reports.ApiReportTests.setUp
    post = test_api_reports.ApiReportTests.post
    def test_partial_refresh_preserves_previous_pages_and_updates_same_key(self):
        stored = {}
        self.ns["save_to_mongodb"].side_effect = lambda collection, key, doc: stored.update({key: doc})
        self.payload["include_subpages"] = True
        root = dict(self.page, depth=0, parent_url=None)
        child = dict(root, url=root["url"] + "/child", depth=1, parent_url=root["url"])
        self.ns["scrape_page_and_subpages_report"].return_value = {"pages": [root, child]}
        first = self.post().get_json()["report"]
        old_child = next(d for d in stored.values() if d["webpage_url"].endswith("/child"))
        self.ns["scrape_page_and_subpages_report"].return_value = {"pages": [dict(root, text="updated"),
            dict(child, outcome="failed", text=None, error={"type": "Timeout"})]}
        second = self.post().get_json()["report"]
        self.assertEqual(first["pull_id"], second["pull_id"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[document_key(old_child)], old_child)
        self.assertTrue(any(d["content"] == "updated" for d in stored.values()))

    def test_active_pull_rejected_before_fetch(self):
        db = self.ns["MongoClient"].return_value.__getitem__.return_value
        db.__getitem__.return_value.insert_one.side_effect = DuplicateKeyError("busy")
        self.assertEqual(self.post().status_code, 409)
        self.ns["scrape_page_report"].assert_not_called()
