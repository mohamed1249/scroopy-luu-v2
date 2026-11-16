import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock, call

import requests
from pymongo.errors import DuplicateKeyError
from download_limits import (DownloadBudget, DownloadTooLarge, RunDownloadLimitExceeded,
                             fetch_bounded, BudgetWriter)
from scr_prp import download_pdf, scrape_page_report, save_to_mongodb


def streamed(body, headers=None):
    response = requests.Response()
    response.status_code = 200
    response.url = "https://example.invalid"
    class RawBody(io.BytesIO):
        def release_conn(self):
            self.close()
    response.raw = RawBody(body)
    response.headers.update(headers or {})
    return response


class DownloadLimitTests(unittest.TestCase):
    def test_exact_limit_and_closed_stream(self):
        response = streamed(b"1234")
        budget = DownloadBudget(4, 8)
        with patch("download_limits.request_get", return_value=response) as get:
            actual = fetch_bounded(response.url, (1, 2), budget=budget)
        self.assertEqual(actual.content, b"1234")
        self.assertEqual(budget.received_bytes, 4)
        self.assertTrue(response.raw.closed)
        get.assert_called_once_with(response.url, timeout=(1, 2), stream=True, allow_redirects=False)

    def test_missing_and_false_small_length_still_enforce_bytes(self):
        for headers in ({}, {"Content-Length": "1"}, {"Content-Length": "nonsense"}):
            with self.subTest(headers=headers):
                response = streamed(b"12345", headers)
                with patch("download_limits.request_get", return_value=response):
                    with self.assertRaises(DownloadTooLarge):
                        fetch_bounded(response.url, 1, budget=DownloadBudget(4, 10))
                self.assertTrue(response.raw.closed)

    def test_advertised_size_rejected_before_reading(self):
        response = streamed(b"12345", {"Content-Length": "5"})
        budget = DownloadBudget(4, 10)
        with patch.object(response, "iter_content") as chunks, patch("download_limits.request_get", return_value=response):
            with self.assertRaises(DownloadTooLarge):
                fetch_bounded(response.url, 1, budget=budget)
        chunks.assert_not_called()
        self.assertEqual(budget.received_bytes, 0)
        self.assertTrue(response.raw.closed)

    def test_shared_budget_counts_failed_body_and_stops_next_request(self):
        budget = DownloadBudget(10, 5)
        with patch("download_limits.request_get", side_effect=[streamed(b"123"), streamed(b"456")]) as get:
            fetch_bounded("https://example.invalid/a", 1, budget=budget)
            with self.assertRaises(RunDownloadLimitExceeded):
                fetch_bounded("https://example.invalid/b", 1, budget=budget)
            with self.assertRaises(RunDownloadLimitExceeded):
                fetch_bounded("https://example.invalid/c", 1, budget=budget)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(budget.received_bytes, 6)

    def test_partial_pdf_removed_and_failure_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            response = streamed(b"123456")
            event = {}
            with patch("download_limits.request_get", return_value=response), patch("download_limits.CHUNK_SIZE", 2):
                with self.assertRaises(DownloadTooLarge):
                    download_pdf(response.url, "pdf", 0, output_dir=directory,
                                 budget=DownloadBudget(3, 100), event=event)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(event["http_status"], 200)
            self.assertTrue(response.raw.closed)

    def test_network_failure_removes_partial_pdf(self):
        response = streamed(b"")
        def chunks(**kwargs):
            yield b"first"
            raise requests.exceptions.ConnectionError("disconnected")
        response.iter_content = chunks
        with tempfile.TemporaryDirectory() as directory, patch("download_limits.request_get", return_value=response):
            with self.assertRaises(requests.exceptions.ConnectionError):
                download_pdf(response.url, "pdf", 0, output_dir=directory)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertTrue(response.raw.closed)

    def test_report_exposes_limit_error(self):
        with patch("download_limits.request_get", return_value=streamed(b"12345")):
            report = scrape_page_report("https://example.invalid", budget=DownloadBudget(4, 100))
        self.assertEqual(report["error"]["type"], "DownloadTooLarge")
        self.assertEqual(report["http_status"], 200)
        self.assertEqual(report["outcome"], "failed")

    def test_budget_writer_never_writes_over_limit(self):
        handle = io.BytesIO()
        writer = BudgetWriter(handle, DownloadBudget(3, 100))
        writer.write(b"12")
        with self.assertRaises(DownloadTooLarge):
            writer.write(b"34")
        self.assertEqual(handle.getvalue(), b"12")

    def test_config_rejects_invalid_values(self):
        for value in (True, 0, -1, 2.5, "25", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DownloadBudget.from_payload({"max_download_mb": value})


class MongoUpdateTests(unittest.TestCase):
    def test_upsert_preserves_identity_and_other_metadata(self):
        documents = {}
        collection = MagicMock()
        def update(query, change, upsert):
            key = query["file_name"]
            document = documents.setdefault(key, dict(query, existing_metadata="keep"))
            document.update(change["$set"])
            return MagicMock(acknowledged=True)
        collection.update_one.side_effect = update
        for content in ("first", "second", "third"):
            save_to_mongodb(collection, "page.json", content)
        self.assertEqual(documents, {"page.json": {"file_name": "page.json",
                         "existing_metadata": "keep", "content": "third"}})
        collection.create_index.assert_called_with("file_name", unique=True)
        collection.replace_one.assert_not_called()
        collection.insert_one.assert_not_called()

    def test_existing_duplicates_fail_without_writing(self):
        collection = MagicMock()
        collection.create_index.side_effect = DuplicateKeyError("duplicates")
        with self.assertRaises(DuplicateKeyError):
            save_to_mongodb(collection, "page.json", "new")
        collection.update_one.assert_not_called()
        collection.delete_many.assert_not_called()

    def test_concurrent_upsert_winner_is_updated(self):
        collection = MagicMock()
        collection.update_one.side_effect = [DuplicateKeyError("race"), MagicMock(matched_count=1)]
        save_to_mongodb(collection, "page.json", "new")
        self.assertEqual(collection.update_one.call_args_list, [
            call({"file_name": "page.json"}, {"$set": {"content": "new"}}, upsert=True),
            call({"file_name": "page.json"}, {"$set": {"content": "new"}}, upsert=False)])

    def test_unrelated_duplicate_error_is_not_swallowed(self):
        collection = MagicMock()
        collection.update_one.side_effect = [DuplicateKeyError("other index"), MagicMock(matched_count=0)]
        with self.assertRaises(DuplicateKeyError):
            save_to_mongodb(collection, "page.json", "new")


if __name__ == "__main__":
    unittest.main()
