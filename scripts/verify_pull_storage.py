import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
"""Local MongoDB integration check; HTTP mocked, disposable database only."""
import os
import tempfile
import uuid
from contextlib import chdir
from unittest.mock import patch
import requests
import io
from PIL import Image
from pymongo import MongoClient
import API


def main():
    uri = "mongodb://127.0.0.1:27017/"
    name = "scroopy_pull_verify_" + uuid.uuid4().hex
    with MongoClient(uri, serverSelectionTimeoutMS=3000) as mongo:
        mongo.admin.command("ping")
        db = mongo[name]
        try:
            failed = False
            image_failed = False
            image_buffer = io.BytesIO()
            Image.new("RGB", (3, 2), "red").save(image_buffer, format="PNG")
            calls = []
            def get(url, **kwargs):
                calls.append(url)
                response = requests.Response()
                response.url = url
                response.status_code = 503 if failed and url.endswith("/child") else 200
                response._content = (b'<h1>Changed</h1><a href="/child">A</a><a href="/child#again">B</a><img src="/image.png"><img src="/image.png">'
                    if url.endswith("/") else b'<p>Original child</p>')
                if url.endswith("/image.png"):
                    response.status_code = 503 if image_failed else 200
                    response._content = image_buffer.getvalue()
                response._content_consumed = True
                return response
            payload = dict(URLs=["https://fixture.example/"], download_content=["y"],
                           download_pdfs=["n"], db=name, include_subpages=True)
            with tempfile.TemporaryDirectory() as temp, chdir(temp), patch.dict(os.environ, {"MONGODB_URI": uri}), patch("download_limits.request_get", side_effect=get):
                client = API.app.test_client()
                first = client.post("/scrape", json=payload)
                assert first.status_code == 200
                first = first.get_json()["report"]
                assert calls.count("https://fixture.example/child") == 1
                child = db.JSONs.find_one({"sources.webpage_url": "https://fixture.example/child"})
                failed = True
                second = client.post("/scrape", json=payload).get_json()["report"]
                assert first["pull_id"] == second["pull_id"]
                assert db.JSONs.count_documents({}) == 2
                assert db.JSONs.find_one({"_id": child["_id"]}) == child
                assert db.pulls.find_one({"_id": first["pull_id"]})["status"] == "partial"
                assert db.runs.count_documents({}) == 2
                assert db.pull_locks.count_documents({}) == 0
                db.pull_locks.insert_one({"_id": first["pull_id"]})
                assert client.post("/scrape", json=payload).status_code == 409
                db.pull_locks.delete_one({"_id": first["pull_id"]})
                third = client.post("/scrape", json=dict(payload, include_text=False)).get_json()["report"]
                assert third["pull_id"] != first["pull_id"]
                assert db.pulls.count_documents({}) == 2
                assert db.JSONs.count_documents({}) == 2
                print("PASS: duplicate link fetch, stable pull refresh, retained failed page, separate settings, MongoDB ownership lock")
                image_payload = dict(payload, image_mode="download", include_subpages=False)
                calls.clear()
                image_report = client.post("/scrape", json=image_payload).get_json()["report"]
                assert calls.count("https://fixture.example/image.png") == 1
                saved_image = image_report["pages"][0]["images"][0]
                assert Path(saved_image["path"]).is_file()
                assert not Path(image_report["workspace"]).exists()
                group = db.JSONs.find_one({"sources.pull_id": image_report["pull_id"]})
                stored = next(source for source in group["sources"] if source["pull_id"] == image_report["pull_id"])
                assert stored["images"][0]["content_hash"] == saved_image["content_hash"]
                image_failed = True
                retry = client.post("/scrape", json=image_payload).get_json()["report"]
                assert retry["pages"][0]["images"][0]["retained_previous"]
                assert Path(saved_image["path"]).is_file()
                assert len(list(Path("resources/images").iterdir())) == 1
                print("PASS: image deduplication, durable files after run cleanup, MongoDB metadata, failed refresh preservation")
        finally:
            assert name.startswith("scroopy_pull_verify_") and len(name) == 52
            mongo.drop_database(name)
            print("Disposable verification database removed")


if __name__ == "__main__":
    main()
