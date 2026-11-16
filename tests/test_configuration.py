"""Real Flask authentication boundary without database/network calls."""
import unittest
from unittest.mock import patch
import API


class ConfigurationTests(unittest.TestCase):
    def test_missing_key_fails_closed(self):
        with patch.dict(API.data["scrape"], {"API_KEY": None}):
            self.assertEqual(API.app.test_client().post("/scrape", json={}).status_code, 503)

    def test_wrong_key_rejected(self):
        with patch.dict(API.data["scrape"], {"API_KEY": "fixture-only-key"}):
            self.assertEqual(API.app.test_client().post("/scrape", json={},
                headers={"API-KEY": "wrong"}).status_code, 401)

    def test_correct_key_reaches_validation(self):
        with patch.dict(API.data["scrape"], {"API_KEY": "fixture-only-key"}):
            self.assertEqual(API.app.test_client().post("/scrape", json={},
                headers={"API-KEY": "fixture-only-key"}).status_code, 400)
