import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from pymongo.errors import ConnectionFailure, DuplicateKeyError, WriteConcernError
from recover import recover_file, recovery_files, main
from storage import database_preflight
from scr_prp import save_to_mongodb


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "content.json.gz"
        self.envelope = {"file_name": "page.json", "content": {"text": "Keep me"}, "database": "test"}
        with gzip.open(self.path, "wt", encoding="utf-8") as handle:
            json.dump(self.envelope, handle)
        self.collection = MagicMock()
        self.collection.find_one.return_value = None
        self.collection.insert_one.return_value.acknowledged = True

    def test_preview_keeps_file_and_never_connects(self):
        with patch("recover.MongoClient") as client, patch("builtins.print"):
            code = main([str(self.path), "--db", "test"])
        self.assertEqual(code, 0)
        client.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_apply_deletes_only_after_acknowledged_save(self):
        def insert(document):
            self.assertTrue(self.path.exists())
            self.assertEqual(document, {"file_name": "page.json", "content": {"text": "Keep me"}})
            return MagicMock(acknowledged=True)
        self.collection.insert_one.side_effect = insert
        result = recover_file(self.path, "test", self.collection, apply=True)
        self.assertEqual(result["outcome"], "recovered")
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / ".replay.lock").exists())

    def test_failed_save_keeps_recovery_file(self):
        self.collection.insert_one.side_effect = ConnectionFailure("offline")
        result = recover_file(self.path, "test", self.collection, apply=True)
        self.assertEqual(result["outcome"], "failed")
        self.assertTrue(self.path.exists())

    def test_unacknowledged_save_keeps_file(self):
        self.collection.insert_one.return_value.acknowledged = False
        self.assertEqual(recover_file(self.path, "test", self.collection, apply=True)["error_type"], "WriteConcernError")
        self.assertTrue(self.path.exists())

    def test_identical_saved_document_is_safe_to_remove(self):
        self.collection.find_one.return_value = dict(self.envelope)
        self.assertEqual(recover_file(self.path, "test", self.collection, apply=True)["outcome"], "recovered")
        self.collection.insert_one.assert_not_called()

    def test_different_document_conflicts_without_overwrite(self):
        self.collection.find_one.return_value = {"content": "newer"}
        self.assertEqual(recover_file(self.path, "test", self.collection, apply=True)["outcome"], "conflict")
        self.assertTrue(self.path.exists())
        self.collection.insert_one.assert_not_called()

    def test_explicit_overwrite_uses_upsert(self):
        self.assertEqual(recover_file(self.path, "test", self.collection, apply=True, overwrite=True)["outcome"], "recovered")
        self.collection.update_one.assert_called_once_with({"file_name": "page.json"},
            {"$set": {"content": {"text": "Keep me"}}}, upsert=True)

    def test_duplicate_insert_race_does_not_overwrite_newer_content(self):
        self.collection.find_one.side_effect = [None, {"content": "other"}]
        self.collection.insert_one.side_effect = DuplicateKeyError("race")
        self.assertEqual(recover_file(self.path, "test", self.collection, apply=True)["outcome"], "conflict")
        self.assertTrue(self.path.exists())

    def test_wrong_database_and_corrupt_files_are_retained(self):
        result = recover_file(self.path, "wrong", self.collection, apply=True)
        self.assertEqual(result["outcome"], "failed")
        self.collection.insert_one.assert_not_called()
        self.path.write_bytes(b"corrupt gzip")
        self.assertEqual(recover_file(self.path, "test", self.collection, apply=True)["outcome"], "failed")
        self.assertTrue(self.path.exists())

    def test_existing_lock_prevents_second_recovery(self):
        lock = self.root / ".replay.lock"
        lock.touch()
        self.assertEqual(recover_file(self.path, "test", self.collection, apply=True)["error_type"], "FileExistsError")
        self.assertTrue(lock.exists())
        self.collection.insert_one.assert_not_called()

    def test_selection_leaves_other_files_alone(self):
        (self.root / "normal.json").write_text("{}")
        self.assertEqual(recovery_files(self.root), [self.path.resolve()])

    def test_preflight_failure_keeps_files(self):
        with patch("recover.MongoClient") as client, patch("builtins.print"):
            client.return_value.admin.command.side_effect = ConnectionFailure("offline")
            self.assertEqual(main([str(self.root), "--db", "test", "--apply"]), 1)
            client.return_value.close.assert_called_once()
        self.assertTrue(self.path.exists())

    def test_preflight_requires_acknowledged_writes(self):
        client = MagicMock()
        client.__getitem__.return_value.__getitem__.return_value.write_concern.acknowledged = False
        with self.assertRaises(WriteConcernError):
            database_preflight(client, "test")

    def test_save_helper_rejects_unacknowledged_update(self):
        self.collection.update_one.return_value.acknowledged = False
        with self.assertRaises(WriteConcernError):
            save_to_mongodb(self.collection, "page", "content")


if __name__ == "__main__":
    unittest.main()
