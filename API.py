from flask import Flask, request, abort
from flask_cors import CORS,cross_origin
import os
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:
    service_account = build = MediaIoBaseDownload = None
from scr_prp import *


data = {
    'scrape': {
         'API_KEY' : os.environ.get("SCROOPY_API_KEY"),
    },
    'drive': {
         'API_KEY' : os.environ.get("SCROOPY_DRIVE_API_KEY"),
    }
}

print('Server Started!')
app = Flask(__name__)
app.config['CORS_HEADERS'] = ['Content-Type', 'API-KEY']
allowed_origins = [value.strip() for value in os.environ.get("SCROOPY_ALLOWED_ORIGINS", "http://127.0.0.1:8501,http://localhost:8501").split(",") if value.strip()]
cors = CORS(app, origins=allowed_origins)

@app.route("/scrape", methods=['POST'])
@cross_origin(origins=allowed_origins, headers=['Content-Type','API-KEY'])
def scrape():

    api_key = request.headers.get("API-KEY")

    if not data["scrape"]['API_KEY']:
        abort(503, "Set SCROOPY_API_KEY before starting Flask")
    if api_key != data["scrape"]['API_KEY']:
        abort(401, "Invalid API key")

    errors = []
    pages = []
    events = []
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"error": "Expected a JSON object"}, 400
    mode = payload.get("extraction_mode", "full")
    include_subpages = payload.get("include_subpages", False)
    max_pages = payload.get("max_pages", 20)
    max_levels = payload.get("max_levels", 2)
    same_site = payload.get("same_site", True)
    if mode not in ("full", "main"):
        return {"error": "extraction_mode must be full or main"}, 400
    if (type(include_subpages) is not bool or type(same_site) is not bool
            or type(max_levels) is not int or max_levels < 1
            or (max_pages is not None and (type(max_pages) is not int or max_pages < 1))):
        return {"error": "Use boolean crawl options, positive max_levels, and a positive max_pages or null for unlimited"}, 400
    fields = [payload.get(name) for name in ("URLs", "download_pdfs", "download_content")]
    if (not all(isinstance(value, list) for value in fields)
            or len({len(value) for value in fields}) != 1
            or not all(isinstance(item, str) for value in fields for item in value)):
        return {"error": "URLs, download_pdfs and download_content must be equal-length lists of strings"}, 400
    if not fields[0]:
        return {"error": "Provide at least one starting URL"}, 400
    if any(flag.lower() not in ("y", "n") for flags in fields[1:] for flag in flags):
        return {"error": "download_pdfs and download_content flags must be y or n"}, 400
    if not any(flag.lower() == "y" for flags in fields[1:] for flag in flags):
        return {"error": "Select webpage content or PDF downloads"}, 400
    # Merge repeated starting URLs, keeping any requested work enabled.
    merged = {}
    try:
        for url, pdf, text in zip(*fields):
            key = canonical_url(url)
            if key in merged:
                old = merged[key]
                old[1] = "y" if pdf.lower() != "n" or old[1].lower() != "n" else "n"
                old[2] = "y" if text.lower() != "n" or old[2].lower() != "n" else "n"
            else:
                merged[key] = [url, pdf, text]
    except ValueError:
        return {"error": "Malformed starting URL"}, 400
    fields = list(zip(*merged.values())) if merged else [[], [], []]
    if not isinstance(payload.get("db"), str) or not payload["db"]:
        return {"error": "db must be a nonempty string"}, 400

    keep_work_files = payload.get("keep_work_files", False)
    if type(keep_work_files) is not bool:
        return {"error": "keep_work_files must be boolean"}, 400
    try:
        budget = DownloadBudget.from_payload(payload)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    recovery_needed = False
    include_text = payload.get("include_text", True)
    if type(include_text) is not bool:
        return {"error": "include_text must be boolean"}, 400
    pull_id, pull_settings = pull_identity(payload)
    client = None
    try:
        client = MongoClient(os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017/"), serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000)
        collection = database_preflight(client, payload["db"])
    except Exception as exc:
        if client is not None:
            client.close()
        return {"error": "Database is not ready; no scraping started",
                "stage": "database_preflight", "error_type": type(exc).__name__}, 503
    try:
        # Unique _id gives one refresh ownership; no time-based lock stealing.
        client[payload["db"]]["pull_locks"].insert_one({"_id": pull_id, "started_at": utc_now()})
    except DuplicateKeyError:
        client.close()
        return {"error": "This pull is already running", "pull_id": pull_id}, 409
    except Exception as exc:
        client.close()
        return {"error": "Could not acquire pull ownership", "error_type": type(exc).__name__}, 503
    try:
        workspace = RunWorkspace(base=Path("runs") / pull_id)
    except Exception:
        client[payload["db"]]["pull_locks"].delete_one({"_id": pull_id})
        client.close()
        raise
    events.append({"stage": "database_preflight", "outcome": "success"})
    crawl_info = None
    status_code = 200

    def result_report():
        if not include_text:
            for page in pages:
                page.pop("text", None)
                page["content_included"] = False
        return {"pull_id": pull_id, "pull_settings": pull_settings,
                "refresh_policy": "merge_successes_preserve_unseen",
                "run_id": workspace.run_id, "workspace": str(workspace.root),
                "files_retained": workspace.root.exists(), "pages": pages,
                "summary": summarize_page_reports(pages), "events": events,
                "downloads": budget.report(), "crawl": crawl_info,
                "storage": {"retained_bytes": sum(p.stat().st_size for p in workspace.root.rglob("*") if p.is_file()),
                            "recovery_needed": recovery_needed}}

    def write_document(path, document, storage_key):
        nonlocal recovery_needed, status_code
        saved, output = store_document(collection, storage_key, document, path,
                                       keep_work_files=keep_work_files, workspace=workspace,
                                       events=events, save=save_to_mongodb)
        if not saved:
            recovery_needed = True
            status_code = 500
            errors.append("Database save failed; compressed recovery document retained")
        return output

    try:
        by_seed = {}
        folders = {}
        if include_subpages:
            crawl_seeds = [url for url, _, dc in zip(*fields) if dc.lower() != "n"]
            if crawl_seeds:
                batch = scrape_page_and_subpages_report(
                    crawl_seeds[0] if len(crawl_seeds) == 1 else crawl_seeds,
                    mode=mode, max_pages=max_pages, budget=budget,
                    max_levels=max_levels, same_site=same_site)
                crawl_info = batch.get("crawl")
                for page in batch["pages"]:
                    by_seed.setdefault(page.get("seed_url", crawl_seeds[0]), []).append(page)
        for index, (url, dpdf, dc) in enumerate(zip(*fields)):
            folder = workspace.page(index, url)
            folders[canonical_url(url)] = folder
            pdfs = {}
            if dpdf.lower() != "n":
                pdfs = download_pdfs_from_url(url, output_dir=folder / "PDFs", events=events, budget=budget) or {}
                for filename in pdfs:
                    try:
                        extracted = extract_pdf_pages(folder / "PDFs" / filename)
                        document = pdf_document(pdfs[filename], extracted, pull_id=pull_id,
                                                run_id=workspace.run_id, parent_url=url)
                        write_document(folder / f"{filename}.json", document, document_key(document))
                        events.append({"stage": "pdf_extract", "file": workspace.relative(folder / "PDFs" / filename),
                                       "outcome": "success", "page_count": len(extracted)})
                    except Exception as exc:
                        events.append({"stage": "pdf_extract", "file": filename,
                                       "outcome": "failed", "error_type": type(exc).__name__})
                    finally:
                        if not keep_work_files:
                            (folder / "PDFs" / filename).unlink(missing_ok=True)
            else:
                events.append({"stage": "pdf_download", "url": url, "outcome": "skipped"})

            if dc.lower() == "n":
                events.append({"stage": "webpage", "url": url, "outcome": "skipped"})
                continue
            if include_subpages:
                page_reports = by_seed.get(url, [])
            else:
                page = scrape_page_report(url, mode=mode, budget=budget)
                page.update(parent_url=None, depth=0, content_included=True)
                page_reports = [page]
            pages.extend(page_reports)
            for page_index, page in enumerate(page_reports):
                if page["error"] is not None:
                    errors.append(f"Failed to scrape {page['url']}: {page['error']['type']}")
                if page["outcome"] != "success":
                    continue
                if page.get("depth", 0) == 0:
                    page_folder = folder
                else:
                    parent_folder = folders.get(canonical_url(page["parent_url"]), folder)
                    page_folder = workspace.page(len(folders), page["url"], parent=parent_folder)
                folders[canonical_url(page["url"])] = page_folder
                path = page_folder / "content.json"
                document = source_document(page["url"], page["text"], pull_id=pull_id,
                    run_id=workspace.run_id, final_url=page.get("final_url"),
                    parent_url=page.get("parent_url"), sections=page.get("sections"),
                    collected_at=page.get("collected_at"),
                    extraction={"mode": mode, "selected_region": page.get("selected_region"),
                                "method": "BeautifulSoup", "http_status": page.get("http_status")})
                document["attached_PDFs"] = pdfs if page_index == 0 else {}
                output_file = write_document(path, document, document_key(document))
                if output_file is not None:
                    page["output_file"] = output_file
                if not include_text:
                    page.pop("text", None)
                    page["content_included"] = False

    except Exception as exc:
        events.append({"stage": "pipeline", "outcome": "failed", "error_type": type(exc).__name__})
        errors.append("Scraping pipeline failed; inspect report for retained recovery files")
        status_code = 500
    finally:
        if client is not None:
            try:
                partial = bool(errors or any(e["outcome"] == "failed" for e in events)
                               or any(p["outcome"] != "success" for p in pages))
                client[payload["db"]]["pulls"].update_one({"_id": pull_id}, {"$set": {
                    "settings": pull_settings, "last_run_id": workspace.run_id,
                    "updated_at": utc_now(), "status": "partial" if partial else "success",
                    "refresh_policy": "merge_successes_preserve_unseen"}}, upsert=True)
                client[payload["db"]]["runs"].insert_one({"_id": workspace.run_id,
                    "pull_id": pull_id, "finished_at": utc_now(),
                    "status": "partial" if partial else "success",
                    "summary": summarize_page_reports(pages), "events": events})
            except Exception as exc:
                events.append({"stage": "pull_metadata_save", "outcome": "failed",
                               "error_type": type(exc).__name__})
                errors.append("Pull metadata save failed; document saves are reported separately")
                status_code = 500
            finally:
                try:
                    client[payload["db"]]["pull_locks"].delete_one({"_id": pull_id})
                except Exception as exc:
                    events.append({"stage": "pull_unlock", "outcome": "failed",
                                   "error_type": type(exc).__name__})
                    errors.append("Pull lock release failed")
                    status_code = 500
                client.close()

    failures = [event for event in events if event["outcome"] == "failed"]
    if failures and not errors:
        errors.append("Some processing steps failed; inspect report.events")
    if keep_work_files or recovery_needed:
        events.append({"stage": "cleanup", "outcome": "skipped",
                       "reason": "requested_retention" if keep_work_files else "recovery_retention"})
    else:
        try:
            workspace.cleanup()
            events.append({"stage": "cleanup", "outcome": "success"})
        except Exception as exc:
            events.append({"stage": "cleanup", "outcome": "failed", "error_type": type(exc).__name__})
            errors.append("Workspace cleanup failed")
            status_code = 500
    return {"errors": errors or "All Completed Successfully!", "report": result_report()}, status_code

@app.route("/drive", methods=['POST'])
@cross_origin(origins=allowed_origins, headers=['Content-Type','API-KEY'])
def drive():

    api_key = request.headers.get("API-KEY")

    if not data["drive"]['API_KEY']:
        abort(503, "The legacy Drive endpoint is not configured")
    if api_key != data["drive"]['API_KEY']:
        abort(401, "Invalid API key")

    if service_account is None:
        abort(503, "Install the optional Google Drive dependencies")

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("db"), str) or not payload["db"]:
        return {"error": "Expected a JSON object with a nonempty db"}, 400
    folder_id = request.json.get('folder_id')
    db = request.json.get("db")

    keep_work_files = request.json.get("keep_work_files", False)
    if type(keep_work_files) is not bool:
        return {"error": "keep_work_files must be boolean"}, 400
    try:
        budget = DownloadBudget.from_payload(request.json)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    errors = []
    client = None
    try:
        client = MongoClient(os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017/"), serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000)
        collection = database_preflight(client, db)
    except Exception as exc:
        if client is not None:
            client.close()
        return {"error": "Database is not ready; no downloads started",
                "stage": "database_preflight", "error_type": type(exc).__name__}, 503
    try:
        workspace = RunWorkspace()
        pdf_dir = workspace.root / "PDFs"
        json_dir = workspace.root / "JSONs"
        pdf_dir.mkdir()
        json_dir.mkdir()

        SCOPES = ['https://www.googleapis.com/auth/drive']

        credentials = service_account.Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=SCOPES)

        # Create a service object
        service = build('drive', 'v3', credentials=credentials)



        def download_pdf_files_in_folder(folder_id):
            # List all files in the specified folder
            results = service.files().list(q=f"'{folder_id}' in parents").execute()
            files = results.get('files', [])

            if not files:
                print(f'No files found in the folder with ID: {folder_id}')
            else:
                for file in files:
                    file_name = file['name']
                    file_id = file['id']
                    mime_type = file['mimeType']

                    if 'pdf' in mime_type:
                        # Download the PDF file
                        target = pdf_dir / (safe_component(file_id) + '.pdf')
                        try:
                            if budget.received_bytes >= budget.total_bytes:
                                raise RunDownloadLimitExceeded("Run download budget exhausted")
                            media_request = service.files().get_media(fileId=file_id)
                            with target.open("wb") as handle:
                                downloader = MediaIoBaseDownload(BudgetWriter(handle, budget), media_request,
                                                                 chunksize=64 * 1024)
                                done = False
                                while not done:
                                    _, done = downloader.next_chunk()
                        except Exception as exc:
                            target.unlink(missing_ok=True)
                            errors.append({"stage": "pdf_download", "file_id": file_id,
                                           "error_type": type(exc).__name__})

                    if mime_type == 'application/vnd.google-apps.folder':
                        # If it's a subfolder, recursively search and download PDFs
                        download_pdf_files_in_folder(file_id)

        download_pdf_files_in_folder(folder_id)

    except Exception:
        client.close()
        raise

    recovery_needed = False
    events = []
    try:
        for pdf_path in pdf_dir.glob("*.pdf"):
            try:
                document = extract_pdf_pages(pdf_path)
                path = json_dir / f"{pdf_path.stem}_pages_data.json"
                saved, _ = store_document(collection, path.name, document, path,
                                          keep_work_files=keep_work_files, workspace=workspace,
                                          events=events, save=save_to_mongodb)
                if not saved:
                    recovery_needed = True
                    errors.append({"stage": "mongodb_save", "file": pdf_path.name})
            except Exception as exc:
                errors.append({"stage": "pdf_extract", "file": pdf_path.name,
                               "error_type": type(exc).__name__})
            finally:
                if not keep_work_files:
                    pdf_path.unlink(missing_ok=True)
    finally:
        client.close()
    if not keep_work_files and not recovery_needed:
        workspace.cleanup()

    print('Done!')

    return {'done': 'Completed with failures' if errors else 'All Completed Successfully!',
            'errors': errors, 'run_id': workspace.run_id, 'workspace': str(workspace.root),
            'files_retained': workspace.root.exists(), 'downloads': budget.report(),
            'events': events, 'storage': {'recovery_needed': recovery_needed,
            'retained_bytes': sum(p.stat().st_size for p in workspace.root.rglob('*') if p.is_file())}}



if __name__ == "__main__":
    app.run(threaded=True, debug=False, host="127.0.0.1", port=int(os.environ.get("PORT", 8080)))
