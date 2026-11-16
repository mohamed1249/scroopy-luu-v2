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
from pymongo import MongoClient
os.environ.setdefault("SCROOPY_API_KEY", uuid.uuid4().hex)
import API


def main():
    uri = "mongodb://127.0.0.1:27017/"
    name = "scroopy_pull_verify_" + uuid.uuid4().hex
    with MongoClient(uri, serverSelectionTimeoutMS=3000) as mongo:
        mongo.admin.command("ping")
        db = mongo[name]
        try:
            failed = False
            calls = []
            def get(url, **kwargs):
                calls.append(url)
                response = requests.Response()
                response.url = url
                response.status_code = 503 if failed and url.endswith("/child") else 200
                response._content = (b'<h1>Changed</h1><a href="/child">A</a><a href="/child#again">B</a>'
                    if url.endswith("/") else b'<p>Original child</p>')
                response._content_consumed = True
                return response
            payload = dict(URLs=["https://fixture.example/"], download_content=["y"],
                           download_pdfs=["n"], db=name, include_subpages=True)
            headers = {"API-KEY": API.data["scrape"]["API_KEY"]}
            with tempfile.TemporaryDirectory() as temp, chdir(temp), patch.dict(os.environ, {"MONGODB_URI": uri}), patch("download_limits.request_get", side_effect=get):
                client = API.app.test_client()
                first = client.post("/scrape", json=payload, headers=headers)
                assert first.status_code == 200
                first = first.get_json()["report"]
                assert calls.count("https://fixture.example/child") == 1
                child = db.JSONs.find_one({"content.webpage_url": "https://fixture.example/child"})
                failed = True
                second = client.post("/scrape", json=payload, headers=headers).get_json()["report"]
                assert first["pull_id"] == second["pull_id"]
                assert db.JSONs.count_documents({}) == 2
                assert db.JSONs.find_one({"_id": child["_id"]}) == child
                assert db.pulls.find_one({"_id": first["pull_id"]})["status"] == "partial"
                assert db.runs.count_documents({}) == 2
                assert db.pull_locks.count_documents({}) == 0
                db.pull_locks.insert_one({"_id": first["pull_id"]})
                assert client.post("/scrape", json=payload, headers=headers).status_code == 409
                db.pull_locks.delete_one({"_id": first["pull_id"]})
                third = client.post("/scrape", json=dict(payload, include_text=False), headers=headers).get_json()["report"]
                assert third["pull_id"] != first["pull_id"]
                assert db.pulls.count_documents({}) == 2
                assert db.JSONs.count_documents({}) == 3
                print("PASS: duplicate link fetch, stable pull refresh, retained failed page, separate settings, MongoDB ownership lock")
        finally:
            assert name.startswith("scroopy_pull_verify_") and len(name) == 52
            mongo.drop_database(name)
            print("Disposable verification database removed")


if __name__ == "__main__":
    main()
