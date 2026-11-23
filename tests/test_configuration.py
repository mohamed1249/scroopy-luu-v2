"""Local Flask endpoints accept requests without application credentials."""
import unittest
from unittest.mock import patch
import API


class ConfigurationTests(unittest.TestCase):
    def test_scrape_without_key_reaches_validation(self):
        self.assertEqual(API.app.test_client().post("/scrape", json={}).status_code, 400)

    def test_old_header_does_not_block_requests(self):
        self.assertEqual(API.app.test_client().post("/scrape", json={},
            headers={"API-KEY": "obsolete"}).status_code, 400)

    def test_drive_without_key_checks_optional_dependencies(self):
        with patch.object(API, "service_account", None):
            response = API.app.test_client().post("/drive", json={})
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"optional Google Drive dependencies", response.data)
