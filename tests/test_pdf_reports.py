import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
from PyPDF2 import PdfWriter
from scr_prp import download_pdfs_from_url, download_pdf
from run_workspace import RunWorkspace, safe_component


def response(body, status=200, url="https://example.invalid/page"):
    result = requests.Response()
    result._content_consumed = True
    result.status_code = status
    result.url = url
    result._content = body
    return result


class PdfReportTests(unittest.TestCase):
    def test_per_link_outcomes_and_safe_names(self):
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        stream = io.BytesIO()
        writer.write(stream)
        valid = stream.getvalue()
        html = (b'<a href="/ok">../../CON</a><a href="/other">../../CON</a>'
                b'<a href="/404">Missing</a><a href="/slow">Slow</a>'
                b'<a href="/html">Not a PDF</a><a href="/ok#part">Duplicate</a>'
                b'<a href="mailto:test@example.invalid">Mail</a>')
        events = []
        with tempfile.TemporaryDirectory() as directory:
            with patch("download_limits.request_get", side_effect=[
                response(html), response(valid), response(valid), response(b"", 404),
                requests.exceptions.Timeout(), response(b"<html>not pdf</html>")
            ]) as get:
                result = download_pdfs_from_url("https://example.invalid/page",
                                               output_dir=directory, events=events)
            self.assertEqual(len(result), 2)
            self.assertEqual(len(list(Path(directory).iterdir())), 2)
            for filename in result:
                self.assertEqual((Path(directory) / filename).resolve().parent, Path(directory).resolve())
            links = [event for event in events if event["stage"] == "pdf_link"]
            self.assertEqual([event["outcome"] for event in links],
                             ["success", "success", "failed", "failed", "failed", "skipped", "skipped"])
            self.assertEqual(links[2]["http_status"], 404)
            self.assertEqual(links[2]["error_type"], "HTTPError")
            self.assertEqual(links[3]["error_type"], "Timeout")
            self.assertEqual(links[4]["error_type"], "InvalidPDF")
            self.assertEqual(get.call_count, 6)
            self.assertTrue(all(call.kwargs["timeout"] == (5, 20) for call in get.call_args_list))

    def test_discovery_failure_is_reported(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            with patch("download_limits.request_get", side_effect=requests.exceptions.ConnectionError()):
                result = download_pdfs_from_url("https://example.invalid", output_dir=directory, events=events)
        self.assertIsNone(result)
        self.assertEqual(events[0]["stage"], "pdf_discovery")
        self.assertEqual(events[0]["error_type"], "ConnectionError")

    def test_output_failure_is_reported(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            with patch("download_limits.request_get", return_value=response(b'<a href="/pdf">PDF</a>')):
                with patch("scr_prp.download_pdf", side_effect=PermissionError()):
                    download_pdfs_from_url("https://example.invalid", output_dir=directory, events=events)
        self.assertEqual(events[-1]["error_type"], "PermissionError")

    def test_workspace_cleanup_is_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = RunWorkspace(directory), RunWorkspace(directory)
            sentinel = second.root / "keep.txt"
            sentinel.write_text("keep")
            first.cleanup()
            self.assertTrue(sentinel.exists())
            second.root = Path(directory)
            with self.assertRaises(ValueError):
                second.cleanup()

    def test_filename_decoding_cannot_introduce_path_components(self):
        for name in ("%2e%2e%2fsecret", "C:\\temp\\file", "CON", "../../x"):
            component = safe_component(name)
            self.assertNotIn("/", component)
            self.assertNotIn("\\", component)
            self.assertNotIn(":", component)


if __name__ == "__main__":
    unittest.main()
