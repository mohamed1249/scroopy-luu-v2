import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from streamlit.testing.v1 import AppTest
from ui_client import make_payload, submit_scrape


RESULT = {"errors": "All Completed Successfully!", "report": {
    "run_id": "test-run", "summary": {"attempted": 1, "success": 1},
    "pages": [{"url": "https://example.com", "outcome": "success", "text": "Hello",
               "http_status": 200, "character_count": 5, "selected_region": "document"}],
    "events": [{"stage": "mongodb_save", "outcome": "success"}], "storage": {}, "downloads": {}}}


class UITests(unittest.TestCase):
    def app(self):
        return AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=15).run()

    def test_empty_url_does_not_submit(self):
        app = self.app()
        app.text_area(key="urls").set_value("")
        with patch("ui_client.submit_scrape") as submit:
            app.button[0].click().run()
        submit.assert_not_called()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("at least one URL", app.error[0].value)

    def test_success_renders_and_rerun_does_not_resubmit(self):
        app = self.app()
        with patch("ui_client.submit_scrape", return_value=(200, RESULT)) as submit:
            app.button[0].click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.metric[1].value, "1")
            self.assertEqual(app.text_area(key="text_test-run_0").value, "Hello")
            payload = submit.call_args.args[0]
            self.assertEqual(payload["db"], "scroopyluu")
            self.assertEqual(payload["download_pdfs"], ["n"])
            app.run()
            submit.assert_called_once()
        self.assertNotIn("api_key", [widget.key for widget in app.text_input])

    def test_server_failure_displays_recovery_report(self):
        app = self.app()
        result = dict(RESULT, errors=["Save failed"])
        with patch("ui_client.submit_scrape", return_value=(500, result)):
            app.button[0].click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("storage failure", app.error[0].value)

    def test_unlimited_and_three_levels_reach_api(self):
        app = self.app()
        app.checkbox(key="subpages").check()
        app.checkbox(key="unlimited").check()
        app.number_input(key="max_levels").set_value(3)
        with patch("ui_client.submit_scrape", return_value=(200, RESULT)) as submit:
            app.button[0].click().run()
        payload = submit.call_args.args[0]
        self.assertIsNone(payload["max_pages"])
        self.assertEqual(payload["max_levels"], 3)
        self.assertTrue(payload["same_site"])
        self.assertEqual(len(app.exception), 0)

    def test_image_settings_reach_api(self):
        app = self.app()
        app.selectbox(key="image_mode").select("download")
        app.number_input(key="max_image_mb").set_value(2)
        app.number_input(key="max_images_mb").set_value(8)
        app.number_input(key="max_images").set_value(4)
        with patch("ui_client.submit_scrape", return_value=(200, RESULT)) as submit:
            app.button[0].click().run()
        payload = submit.call_args.args[0]
        self.assertEqual(payload["image_mode"], "download")
        self.assertEqual((payload["max_image_mb"], payload["max_images_mb"], payload["max_images"]), (2, 8, 4))
        self.assertEqual(len(app.exception), 0)

    def test_database_blank_falls_back_and_custom_is_forwarded(self):
        app = self.app()
        for value, expected in (("", "scroopyluu"), (" scroopy_recording ", "scroopy_recording")):
            app.text_input(key="database").set_value(value)
            with patch("ui_client.submit_scrape", return_value=(200, RESULT)) as submit:
                app.button[0].click().run()
            self.assertEqual(submit.call_args.args[0]["db"], expected)
            self.assertEqual(len(app.exception), 0)

    def test_connection_error_is_readable(self):
        import requests
        with patch("ui_client.requests.post", side_effect=requests.exceptions.ConnectionError()):
            with self.assertRaisesRegex(RuntimeError, "Cannot reach Flask"):
                submit_scrape({})

    def test_invalid_response_is_readable(self):
        response = MagicMock(status_code=400)
        response.json.side_effect = ValueError()
        with patch("ui_client.requests.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "400: Request failed"):
                submit_scrape({})

    def test_payload_deduplicates_and_preserves_flags(self):
        payload = make_payload("https://example.com\nhttps://example.com\nhttps://other.example", database="test",
             mode="main", pdfs=True, webpage=True, subpages=True, max_pages=3,
             max_download_mb=25, max_run_download_mb=100, keep_files=False, include_text=False)
        self.assertEqual(len(payload["URLs"]), 2)
        self.assertEqual(payload["download_pdfs"], ["y", "y"])
        self.assertFalse(payload["include_text"])


if __name__ == "__main__":
    unittest.main()
