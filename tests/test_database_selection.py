import unittest
from settings import resolve_database_name
import test_api_reports


class DatabaseNameTests(unittest.TestCase):
    def test_default_and_custom_names(self):
        for value in (None, "", "   "):
            self.assertEqual(resolve_database_name(value), "scroopyluu")
        self.assertEqual(resolve_database_name("  scroopy_recording  "), "scroopy_recording")

    def test_reject_non_string_names(self):
        for value in (1, True, [], {}):
            with self.assertRaises(ValueError):
                resolve_database_name(value)


class DatabaseApiTests(unittest.TestCase):
    setUp = test_api_reports.ApiReportTests.setUp
    post = test_api_reports.ApiReportTests.post

    def test_blank_missing_and_explicit_default_share_pull(self):
        identities = []
        for value in (None, "", "  ", "scroopyluu"):
            payload = dict(self.payload)
            if value is None:
                payload.pop("db")
            else:
                payload["db"] = value
            response = self.post(payload)
            self.assertEqual(response.status_code, 200)
            report = response.get_json()["report"]
            self.assertEqual(report["pull_settings"]["db"], "scroopyluu")
            identities.append(report["pull_id"])
        self.assertEqual(len(set(identities)), 1)

    def test_custom_database_is_used_without_reset(self):
        response = self.post(dict(self.payload, db="  scroopy_recording "))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["report"]["pull_settings"]["db"], "scroopy_recording")
        client = self.ns["MongoClient"].return_value
        client.drop_database.assert_not_called()
        client.__getitem__.assert_any_call("scroopy_recording")
