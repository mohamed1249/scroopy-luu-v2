import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
"""Opt-in live LOCAL MongoDB verification; creates and drops one temporary DB.

Run: python -B verify_local_mongodb.py
HTTP downloads are mocked; Flask, extraction, files, and MongoDB are real.
"""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import tempfile
import uuid
from unittest.mock import patch

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure
import requests
from PyPDF2 import PdfWriter
from PyPDF2.generic import NameObject, DecodedStreamObject, DictionaryObject

import API
from scr_prp import save_to_mongodb


def main():
    uri = "mongodb://127.0.0.1:27017/"
    database_name = "scroopy_verify_" + uuid.uuid4().hex
    with MongoClient(uri, serverSelectionTimeoutMS=5000) as client:
        client.admin.command("ping")
        database = client[database_name]
        try:
            collection = database["identity_checks"]
            for value in ("one", "two", "three"):
                save_to_mongodb(collection, "same.json", value)
            assert collection.count_documents({}) == 1
            assert collection.find_one({"file_name": "same.json"})["content"] == "three"
            collection.update_one({"file_name": "same.json"}, {"$set": {"metadata": "keep"}})
            save_to_mongodb(collection, "same.json", "four")
            assert collection.find_one({"file_name": "same.json"})["metadata"] == "keep"
            print("PASS: repeated saves preserve identity and metadata")

            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda n: save_to_mongodb(collection, "concurrent.json", n), range(12)))
            assert collection.count_documents({"file_name": "concurrent.json"}) == 1
            print("PASS: concurrent saves produce one document")

            conflicting = database["duplicate_fixture"]
            conflicting.insert_many([{"file_name": "duplicate"}, {"file_name": "duplicate"}])
            try:
                save_to_mongodb(conflicting, "duplicate", "new")
            except (DuplicateKeyError, OperationFailure):
                pass
            else:
                raise AssertionError("Existing duplicates were not rejected")
            assert conflicting.count_documents({}) == 2
            print("PASS: duplicate fixture fails safely without deleting records")

            from recover import main as recover_main
            from storage import store_document
            from run_workspace import RunWorkspace
            with tempfile.TemporaryDirectory(prefix="scroopy_recovery_verify_") as recovery_temp:
                workspace = RunWorkspace(recovery_temp)
                path = workspace.root / "content.json"
                def offline(*args):
                    raise RuntimeError("simulated save interruption")
                saved, relative = store_document(database["JSONs"], "recovered.json", {"text": "Recovered without scraping"},
                    path, keep_work_files=False, workspace=workspace, events=[], save=offline)
                assert not saved
                recovery_path = workspace.root / relative
                with patch.dict(os.environ, {"MONGODB_URI": uri}), contextlib.redirect_stdout(io.StringIO()):
                    assert recover_main([str(recovery_path), "--db", database_name]) == 0
                    assert recovery_path.exists()
                    assert recover_main([str(recovery_path), "--db", database_name, "--apply"]) == 0
                assert not recovery_path.exists()
                assert database["JSONs"].find_one({"file_name": "recovered.json"})["content"]["text"] == "Recovered without scraping"
                print("PASS: compressed recovery preview and acknowledged replay to local MongoDB")

            writer = PdfWriter()
            page = writer.add_blank_page(width=72, height=72)
            page = writer.pages[0]
            content = DecodedStreamObject()
            content.set_data(b"BT /F1 12 Tf (PDF fixture text) Tj ET")
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})})})
            page[NameObject("/Contents")] = writer._add_object(content)
            pdf = io.BytesIO()
            writer.write(pdf)
            bodies = {
                "https://fixture.example/": b'<main>Seed content</main><a href="/child">Child</a><a href="/doc.pdf">Document</a>',
                "https://fixture.example/child": b'<main>Child content</main>',
                "https://fixture.example/doc.pdf": pdf.getvalue(),
            }
            def fake_get(url, **kwargs):
                response = requests.Response()
                response.status_code = 200
                response.url = url
                response._content = bodies[url]
                response._content_consumed = True
                return response

            with tempfile.TemporaryDirectory(prefix="sv_") as temporary:
                with contextlib.chdir(temporary), patch.dict(os.environ, {"MONGODB_URI": uri}), patch("download_limits.request_get", side_effect=fake_get):
                    response = API.app.test_client().post("/scrape", json={
                        "URLs": ["https://fixture.example/"], "download_content": ["y"],
                        "download_pdfs": ["y"], "db": database_name, "include_subpages": True,
                        "extraction_mode": "main", "keep_work_files": True,
                    })
                assert response.status_code == 200, response.status_code
                report = response.get_json()["report"]
                assert Path(report["workspace"]).exists()
                assert any(page.get("depth", 0) > 0 and page.get("output_file")
                           and (Path(report["workspace"]) / page["output_file"]).is_file()
                           for page in report["pages"])
                assert any(event.get("error_type") == "InvalidPDF" for event in report["events"])
                assert any(event["stage"] == "pdf_extract" and event["outcome"] == "success" for event in report["events"]), report["events"]
                assert database["JSONs"].count_documents({}) >= 3
                assert report["downloads"]["received_body_bytes"] > 0
                print("PASS: actual Flask route, parsing, nested outputs, PDF report, and local persistence")
                # Extended Windows paths must also be used for retained-file cleanup.
                import shutil
                owned = Path(report["workspace"])
                assert owned.parents[2].samefile(temporary)
                assert owned.name == report["run_id"]
                shutil.rmtree(owned)
        finally:
            # Only this generated database belongs to this verification run.
            assert database_name.startswith("scroopy_verify_") and len(database_name) == 47
            client.drop_database(database_name)
            print("Temporary verification database removed")


if __name__ == "__main__":
    main()
