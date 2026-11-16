"""Exercise the actual /scrape function with Flask and isolated dependencies.

Load only this route's AST: no Google credentials, application startup, or
embedded production database configuration is executed by this harness.
"""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, ANY, patch

from flask import Flask, request, abort
import scr_prp

API_PATH = Path(__file__).resolve().parents[1] / "API.py"


class ApiReportTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(API_PATH.read_text(encoding="utf-8-sig"))
        route = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "scrape")
        route.decorator_list = []
        # Never use the embedded connection string, even as a mock argument.
        for node in ast.walk(route):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "MongoClient":
                node.args = [ast.Constant("mongodb://unused-test-host")]
        module = ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[]))
        self.ns = dict(vars(scr_prp))
        self.ns.update(request=request, abort=abort, data={"scrape": {"API_KEY": "test-key"}})
        self.ns["MongoClient"] = MagicMock()
        self.ns["save_to_mongodb"] = MagicMock()
        self.ns["download_pdfs_from_url"] = MagicMock(return_value={})
        self.ns["extract_pdf_pages"] = MagicMock(return_value=[{"information": "PDF text"}])
        self.page = {"url": "https://example.invalid", "final_url": "https://example.invalid",
                     "outcome": "success", "http_status": 200, "text": "Hello",
                     "character_count": 5, "requested_mode": "full", "selected_region": "document",
                     "duration_ms": 1, "error": None}
        self.ns["scrape_page_report"] = MagicMock(side_effect=lambda *a, **k: dict(self.page))
        self.ns["scrape_page_and_subpages_report"] = MagicMock()
        exec(compile(module, str(API_PATH), "exec"), self.ns)
        app = Flask(__name__)
        app.testing = True
        app.add_url_rule("/scrape", view_func=self.ns["scrape"], methods=["POST"])
        self.client = app.test_client()
        self.payload = {"URLs": ["https://example.invalid"], "download_pdfs": ["n"],
                        "download_content": ["y"], "db": "test-db"}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        previous = os.getcwd()
        os.chdir(temp.name)
        self.addCleanup(os.chdir, previous)

    def post(self, payload=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.client.post("/scrape", json=self.payload if payload is None else payload,
                                    headers={"API-KEY": "test-key"})

    def test_existing_persistence_and_cleanup_plus_report(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertIn("errors", result)
        self.assertEqual(result["report"]["summary"]["success"], 1)
        self.ns["save_to_mongodb"].assert_called_once()
        document = self.ns["save_to_mongodb"].call_args.args[2]
        self.assertEqual(document["webpage_url"], self.payload["URLs"][0])
        self.assertEqual(document["content"], "Hello")
        self.assertEqual(document["attached_PDFs"], {})
        self.assertEqual(document["pull_id"], result["report"]["pull_id"])
        self.assertEqual(document["schema_version"], 1)
        self.assertFalse(Path("JSONs").exists())
        self.assertFalse(Path("PDFs").exists())
        self.assertIn("mongodb_save", [e["stage"] for e in result["report"]["events"]])

    def test_validation_before_side_effects(self):
        for updates in ({"extraction_mode": "bad"}, {"download_content": []},
                        {"include_subpages": "yes"}, {"max_pages": 0}):
            with self.subTest(updates=updates):
                self.assertEqual(self.post(dict(self.payload, **updates)).status_code, 400)
        self.ns["MongoClient"].assert_not_called()
        self.assertFalse(Path("JSONs").exists())

    def test_subpage_reports_preserve_success_alongside_failure(self):
        root = dict(self.page, parent_url=None, depth=0, content_included=True)
        child = dict(root, url="https://example.invalid/child", parent_url=root["url"], depth=1)
        failed = dict(child, url="https://example.invalid/bad", outcome="failed", text=None,
                      error={"type": "Timeout"})
        self.ns["scrape_page_and_subpages_report"].return_value = {"pages": [root, child, failed]}
        self.payload.update(include_subpages=True, extraction_mode="main", max_pages=3)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        report = response.get_json()["report"]
        self.assertEqual(report["summary"]["success"], 2)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(self.ns["save_to_mongodb"].call_count, 2)
        self.ns["scrape_page_and_subpages_report"].assert_called_once_with(
            self.payload["URLs"][0], mode="main", max_pages=3, budget=ANY, max_levels=2, same_site=True)

    def test_pdf_functionality_is_retained(self):
        def download(url, *, output_dir, events, budget):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "example.pdf").write_bytes(b"fake PDF for mocked extractor")
            return {"example.pdf": "https://example.invalid/example.pdf"}
        self.ns["download_pdfs_from_url"].side_effect = download
        self.payload["download_pdfs"] = ["y"]
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.ns["extract_pdf_pages"].assert_called_once()
        self.assertEqual(self.ns["save_to_mongodb"].call_count, 2)
        events = response.get_json()["report"]["events"]
        self.assertTrue(any(e["stage"] == "pdf_extract" and e["outcome"] == "success" for e in events))

    def test_database_failure_returns_report_and_retains_work(self):
        self.ns["save_to_mongodb"].side_effect = RuntimeError("database unavailable")
        response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["report"]["summary"]["success"], 1)
        self.assertTrue(Path(response.get_json()["report"]["workspace"]).exists())
        self.assertTrue(any(e["outcome"] == "failed" for e in response.get_json()["report"]["events"]))

    def test_run_does_not_touch_unowned_files(self):
        Path("JSONs").mkdir()
        Path("JSONs/foreign.json").write_text('{"content": "other run"}')
        Path("PDFs").mkdir()
        Path("PDFs/foreign.pdf").write_bytes(b"foreign")
        Path("personal.pdf").write_bytes(b"personal")
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Path("JSONs/foreign.json").exists())
        self.assertTrue(Path("PDFs/foreign.pdf").exists())
        self.assertTrue(Path("personal.pdf").exists())
        self.ns["save_to_mongodb"].assert_called_once()
        self.assertFalse(response.get_json()["report"]["files_retained"])

    def test_retained_run_has_nested_subpage_files_and_no_report_file(self):
        root = dict(self.page, depth=0, parent_url=None)
        child = dict(self.page, url="https://example.invalid/child", depth=1, parent_url=root["url"])
        self.ns["scrape_page_and_subpages_report"].return_value = {"pages": [root, child]}
        self.payload.update(include_subpages=True, keep_work_files=True)
        response = self.post()
        report = response.get_json()["report"]
        workspace = Path(report["workspace"])
        self.assertTrue(report["files_retained"])
        self.assertIn("/subpages/", report["pages"][1]["output_file"])
        self.assertEqual(len(list(workspace.rglob("*.json"))), 2)
        for page in report["pages"]:
            self.assertTrue((workspace / page["output_file"]).exists())

    def test_concurrent_runs_cannot_clean_each_others_files(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)
        self.ns["save_to_mongodb"].side_effect = lambda *args: barrier.wait(timeout=10)
        app = self.client.application
        def run(keep):
            with app.test_client() as client:
                return client.post("/scrape", json=dict(self.payload, keep_work_files=keep),
                                   headers={"API-KEY": "test-key"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, False)
            second = pool.submit(run, True)
            a, b = first.result(), second.result()
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        ra, rb = a.get_json()["report"], b.get_json()["report"]
        self.assertNotEqual(ra["run_id"], rb["run_id"])
        self.assertFalse(Path(ra["workspace"]).exists())
        self.assertTrue((Path(rb["workspace"]) / rb["pages"][0]["output_file"]).exists())

    def test_pdf_link_failures_are_reported_without_retaining_run(self):
        def download(url, *, output_dir, events, budget):
            events.append({"stage": "pdf_link", "url": url + "/missing.pdf",
                           "outcome": "failed", "http_status": 404, "error_type": "HTTPError"})
            return {}
        self.ns["download_pdfs_from_url"].side_effect = download
        self.payload["download_pdfs"] = ["y"]
        response = self.post()
        report = response.get_json()["report"]
        self.assertFalse(report["files_retained"])
        self.assertTrue(any(e.get("http_status") == 404 for e in report["events"]))
        self.assertIsInstance(response.get_json()["errors"], list)

    def test_drive_uses_own_workspace_and_safe_filenames(self):
        tree = ast.parse(API_PATH.read_text())
        route = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "drive")
        route.decorator_list = []
        for node in ast.walk(route):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "MongoClient":
                node.args = [ast.Constant("mongodb://unused-test-host")]
        module = ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[]))
        ns = dict(self.ns)
        ns["data"] = {"drive": {"API_KEY": "test-key"}}
        ns["service_account"] = MagicMock()
        ns["build"] = MagicMock()
        def fake_downloader(writer, media_request, chunksize):
            downloader = MagicMock()
            def chunk():
                writer.write(b"fake PDF")
                return None, True
            downloader.next_chunk.side_effect = chunk
            return downloader
        ns["MediaIoBaseDownload"] = fake_downloader
        files = ns["build"].return_value.files.return_value
        files.list.return_value.execute.return_value = {"files": [
            {"name": "../../foreign.pdf", "id": "first-id", "mimeType": "application/pdf"},
            {"name": "../../foreign.pdf", "id": "second-id", "mimeType": "application/pdf"}]}
        files.get_media.return_value.execute.return_value = b"fake PDF"
        exec(compile(module, str(API_PATH), "exec"), ns)
        app = Flask("drive-test")
        app.add_url_rule("/drive", view_func=ns["drive"], methods=["POST"])
        Path("foreign.pdf").write_bytes(b"untouched")
        with contextlib.redirect_stdout(io.StringIO()), patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "mock-credentials.json"}):
            response = app.test_client().post("/drive", json={"folder_id": "folder", "db": "test", "keep_work_files": True},
                                              headers={"API-KEY": "test-key"})
        self.assertEqual(response.status_code, 200)
        workspace = Path(response.get_json()["workspace"])
        self.assertEqual(len(list((workspace / "PDFs").glob("*.pdf"))), 2)
        self.assertEqual(Path("foreign.pdf").read_bytes(), b"untouched")
        self.assertEqual(ns["save_to_mongodb"].call_count, 2)

    def test_real_page_fetches_share_configured_run_budget(self):
        from unittest.mock import patch
        from test_download_limits import streamed
        self.ns["scrape_page_report"] = scr_prp.scrape_page_report
        self.payload.update(URLs=["https://example.invalid/a", "https://example.invalid/b", "https://example.invalid/c"],
                            download_pdfs=["n"] * 3, download_content=["y"] * 3,
                            max_download_mb=1, max_run_download_mb=1)
        body = b"<div>" + b"x" * (600 * 1024) + b"</div>"
        with patch("download_limits.request_get", side_effect=[streamed(body), streamed(body)]) as get:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        report = response.get_json()["report"]
        self.assertEqual(report["summary"], {"attempted": 2, "success": 1, "empty": 0, "failed": 1, "skipped": 1})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(report["pages"][1]["error"]["type"], "RunDownloadLimitExceeded")
        self.assertEqual(report["downloads"]["run_limit_bytes"], 1024 * 1024)
        self.assertFalse(report["files_retained"])
        self.ns["save_to_mongodb"].assert_called_once()

    def test_invalid_download_limits_have_no_side_effects(self):
        for key in ("max_download_mb", "max_run_download_mb"):
            for value in (0, -1, True, "25", 1.5):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.post(dict(self.payload, **{key: value})).status_code, 400)
        self.assertFalse(Path("runs").exists())
        self.ns["MongoClient"].assert_not_called()

    def test_no_json_round_trip_by_default(self):
        def save(collection, key, document):
            self.assertEqual(list(Path("runs").rglob("*.json")), [])
            self.assertEqual(document["content"], "Hello")
        self.ns["save_to_mongodb"].side_effect = save
        response = self.post()
        report = response.get_json()["report"]
        self.assertEqual(report["storage"]["retained_bytes"], 0)
        self.assertFalse(any(e["stage"] == "json_write" for e in report["events"]))

    def test_database_failure_keeps_only_compressed_recoverable_document(self):
        import gzip
        self.page["text"] = "Repeated paragraph. " * 10000
        self.ns["save_to_mongodb"].side_effect = RuntimeError("offline")
        response = self.post()
        report = response.get_json()["report"]
        files = [p for p in Path(report["workspace"]).rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].suffix, ".gz")
        with gzip.open(files[0], "rt", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["content"]["content"], self.page["text"])
        self.assertEqual(document["file_name"], scr_prp.document_key(document["content"]))
        self.assertLess(report["storage"]["retained_bytes"], len(self.page["text"]) // 10)
        self.assertTrue(report["storage"]["recovery_needed"])

    def test_optional_metadata_report_preserves_stored_text(self):
        self.payload["include_text"] = False
        response = self.post()
        self.assertNotIn("text", response.get_json()["report"]["pages"][0])
        self.assertEqual(self.ns["save_to_mongodb"].call_args.args[2]["content"], "Hello")

    def test_preflight_failure_prevents_downloads_and_workspace_creation(self):
        from pymongo.errors import ServerSelectionTimeoutError
        client = self.ns["MongoClient"].return_value
        client.admin.command.side_effect = ServerSelectionTimeoutError("offline")
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["stage"], "database_preflight")
        self.ns["scrape_page_report"].assert_not_called()
        self.ns["download_pdfs_from_url"].assert_not_called()
        self.assertFalse(Path("runs").exists())
        client.close.assert_called_once()

    def test_index_conflict_prevents_downloads(self):
        from pymongo.errors import OperationFailure
        client = self.ns["MongoClient"].return_value
        client.__getitem__.return_value.__getitem__.return_value.create_index.side_effect = OperationFailure("conflict")
        self.assertEqual(self.post().status_code, 503)
        self.ns["scrape_page_report"].assert_not_called()
        self.assertFalse(Path("runs").exists())

    def test_recursive_unlimited_crawl_deduplicates_across_starting_urls(self):
        from unittest.mock import patch
        from test_crawler import response
        base = "https://portfolio.example"
        graph = {base + "/": '<a href="/tab">Tab</a><a href="/other">Other</a>',
                 base + "/tab": '<a href="/post">Post</a>',
                 base + "/other": '<a href="/post#section">Post</a>',
                 base + "/post": 'Post text'}
        self.ns["scrape_page_and_subpages_report"] = scr_prp.scrape_page_and_subpages_report
        self.payload.update(URLs=[base + "/", base + "/tab", base + "/#top"],
                            download_pdfs=["n"] * 3, download_content=["y"] * 3,
                            include_subpages=True, max_levels=3, max_pages=None, keep_work_files=True)
        with patch("download_limits.request_get", side_effect=lambda url, **kwargs: response(url, graph[url])) as get:
            result = self.post()
        self.assertEqual(result.status_code, 200)
        report = result.get_json()["report"]
        self.assertEqual(get.call_count, 4)
        self.assertEqual(report["summary"]["success"], 4)
        self.assertEqual(self.ns["save_to_mongodb"].call_count, 4)
        self.assertEqual(report["crawl"]["max_levels"], 3)
        for page in report["pages"]:
            self.assertTrue((Path(report["workspace"]) / page["output_file"]).exists())

    def test_auth_is_retained(self):
        response = self.client.post("/scrape", json=self.payload)
        self.assertEqual(response.status_code, 401)
        self.ns["MongoClient"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
