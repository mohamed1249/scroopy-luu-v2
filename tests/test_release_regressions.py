"""Failure cases found during the publication-readiness review."""
import unittest
from pathlib import Path
import test_api_reports
from unittest.mock import patch
import requests
from scr_prp import scrape_page_report


class ReleaseRegressionTests(unittest.TestCase):
    setUp = test_api_reports.ApiReportTests.setUp
    post = test_api_reports.ApiReportTests.post

    def test_pdf_link_is_not_saved_as_html_garbage(self):
        response = requests.Response()
        response.status_code = 200
        response.url = "https://example.invalid/doc.pdf"
        response._content = b"%PDF-1.4 binary data"
        response._content_consumed = True
        with patch("download_limits.request_get", return_value=response):
            report = scrape_page_report(response.url)
        self.assertEqual(report["outcome"], "failed")
        self.assertIsNone(report["text"])
        self.assertEqual(report["error"]["type"], "UnsupportedContentType")

    def test_empty_pdf_refresh_preserves_previous_document(self):
        self.payload["download_pdfs"] = ["y"]
        self.payload["download_content"] = ["n"]
        def download(url, *, output_dir, **kwargs):
            output_dir.mkdir(parents=True)
            (output_dir / "doc.pdf").write_bytes(b"mock pdf")
            return {"doc.pdf": "https://example.invalid/doc.pdf"}
        self.ns["download_pdfs_from_url"].side_effect = download
        self.ns["extract_pdf_pages"].return_value = [{"information": "Original", "page_number": 1}]
        stored = {}
        self.ns["save_to_mongodb"].side_effect = lambda col, key, doc: stored.update({key: doc})
        self.assertEqual(self.post().status_code, 200)
        previous = dict(stored)
        self.ns["extract_pdf_pages"].return_value = [{"information": None, "page_number": 1}]
        response = self.post().get_json()
        self.assertEqual(stored, previous)
        self.assertTrue(any(e.get("error_type") == "EmptyPDFText" for e in response["report"]["events"]))

    def test_empty_seed_list_rejected_before_database(self):
        self.payload.update(URLs=[], download_content=[], download_pdfs=[])
        self.assertEqual(self.post().status_code, 400)
        self.ns["MongoClient"].assert_not_called()

    def test_invalid_flags_rejected_before_database(self):
        self.payload["download_pdfs"] = ["maybe"]
        self.assertEqual(self.post().status_code, 400)
        self.ns["MongoClient"].assert_not_called()

    def test_no_work_selected_rejected_before_database(self):
        self.payload["download_content"] = ["n"]
        self.assertEqual(self.post().status_code, 400)
        self.ns["MongoClient"].assert_not_called()

    def test_metadata_failure_releases_lock_and_reports_error(self):
        db = self.ns["MongoClient"].return_value.__getitem__.return_value
        db.__getitem__.return_value.update_one.side_effect = RuntimeError("metadata offline")
        response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertTrue(any(e["stage"] == "pull_metadata_save" for e in response.get_json()["report"]["events"]))
        db.__getitem__.return_value.delete_one.assert_called_once()

    def test_pipeline_failure_releases_lock(self):
        self.ns["scrape_page_report"].side_effect = RuntimeError("extractor failed")
        response = self.post()
        self.assertEqual(response.status_code, 500)
        db = self.ns["MongoClient"].return_value.__getitem__.return_value
        db.__getitem__.return_value.delete_one.assert_called_once()
        self.assertFalse(Path(response.get_json()["report"]["workspace"]).exists())
