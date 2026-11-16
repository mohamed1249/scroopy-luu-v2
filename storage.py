"""Persist directly; disk copies are opt-in or compressed recovery data."""
import gzip
import json
from pymongo.errors import WriteConcernError


def database_preflight(client, database_name):
    """Check reachability and index readiness before any scraping work."""
    client.admin.command("ping")
    collection = client[database_name]["JSONs"]
    if collection.write_concern.acknowledged is False:
        raise WriteConcernError("Acknowledged database writes are required")
    collection.create_index("file_name", unique=True)
    return collection


def store_document(collection, storage_key, document, path, *, keep_work_files,
                   workspace, events, save):
    relative = workspace.relative(path)
    try:
        save(collection, storage_key, document)
    except Exception as exc:
        recovery = path.with_suffix(path.suffix + ".gz")
        # A self-contained envelope keeps the database key for manual recovery.
        envelope = {"file_name": storage_key, "content": document}
        database_name = getattr(collection.database, "name", None)
        if isinstance(database_name, str):
            envelope["database"] = database_name
        temporary = recovery.with_name(".recovery.tmp")
        try:
            with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
                json.dump(envelope, handle, ensure_ascii=False, separators=(",", ":"))
            temporary.replace(recovery)
        finally:
            temporary.unlink(missing_ok=True)
        events.append({"stage": "mongodb_save", "outcome": "failed",
                       "error_type": type(exc).__name__, "recovery_file": workspace.relative(recovery)})
        return False, workspace.relative(recovery)
    events.append({"stage": "mongodb_save", "outcome": "success", "document_key": storage_key})
    if keep_work_files:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
        events.append({"stage": "json_write", "file": relative, "outcome": "success"})
        return True, relative
    return True, None
