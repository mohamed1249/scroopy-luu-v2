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
    if isinstance(document, dict) and document.get("source_id") and not document.get("content", "").strip():
        events.append({"stage": "mongodb_save", "outcome": "skipped", "reason": "empty_content", "document_key": storage_key})
        return True, None
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


def expand_document(collection, document):
    """Read both legacy inline text and schema-2 shared text."""
    if not isinstance(document, dict) or "content_ref" not in document:
        return document
    blob = collection.database["contents"].find_one({"_id": document["content_ref"]})
    if not isinstance(blob, dict) or "text" not in blob:
        raise ValueError("Stored source content is missing")
    expanded = dict(document)
    expanded["content"] = blob["text"]
    expanded.pop("content_ref")
    expanded["schema_version"] = 1
    return expanded



def find_source(collection, storage_key):
    """Resolve a source from content-first or legacy storage."""
    group = collection.find_one({"schema_version": 3, "sources.file_name": storage_key})
    if isinstance(group, dict):
        for source in group.get("sources", []):
            if source["file_name"] == storage_key:
                doc = {k: v for k, v in source.items() if k != "file_name"}
                doc.update(content=group["content"], content_hash=group["content_hash"], schema_version=1)
                return {"content": doc}
    old = collection.find_one({"file_name": storage_key})
    if isinstance(old, dict):
        return {**old, "content": expand_document(collection, old.get("content"))}
    return None


def save_content_group(collection, storage_key, document):
    """One nonempty exact text with embedded source metadata per database."""
    import hashlib
    from pymongo.errors import DuplicateKeyError
    text = document["content"]
    if not text.strip():
        return False  # An empty refresh must not erase previously collected text.
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != document["content_hash"]:
        raise ValueError("Content hash does not match text")
    source = {k: v for k, v in document.items() if k not in ("content", "content_hash", "schema_version", "content_ref")}
    source["file_name"] = storage_key
    key = "text:" + digest
    # A single atomic pipeline retains other sources and refreshes this source.
    fields = {"schema_version": 3, "file_name": key, "content_hash": digest,
              "content": {"$literal": text},
              "sources": {"$concatArrays": [
                  {"$filter": {"input": {"$ifNull": ["$sources", []]}, "as": "s",
                               "cond": {"$ne": ["$$s.file_name", {"$literal": storage_key}]}}},
                  {"$literal": [source]}]}}
    pipeline = [{"$set": fields}]
    try:
        result = collection.update_one({"file_name": key}, pipeline, upsert=True)
    except DuplicateKeyError:
        result = collection.update_one({"file_name": key}, pipeline)
        if not result.matched_count:
            raise
    if not result.acknowledged:
        raise WriteConcernError("Content write was not acknowledged")
    # Only detach after the replacement is durably acknowledged. Partial runs
    # never touch unseen sources. Shared content belonging to other URLs survives.
    result = collection.update_many({"schema_version": 3, "file_name": {"$ne": key},
                                     "sources.file_name": storage_key},
                                    {"$pull": {"sources": {"file_name": storage_key}}})
    if not result.acknowledged:
        raise WriteConcernError("Source move was not acknowledged")
    collection.delete_many({"schema_version": 3, "sources": {"$size": 0}})
    collection.delete_one({"file_name": storage_key, "schema_version": {"$ne": 3}})
    return True


def brief_diagnostics(pages, events, limit=200):
    """Count every issue, retain bounded examples without page text or traces."""
    from collections import Counter
    counts = Counter()
    details = []
    total = 0
    items = [("webpage", item) for item in pages] + [(None, item) for item in events]
    for stage, item in sorted(items, key=lambda pair: {"failed": 0, "empty": 1}.get(pair[1].get("outcome"), 2)):
        outcome = item.get("outcome")
        if outcome not in ("failed", "empty", "skipped"):
            continue
        error = item.get("error") or {}
        kind = error.get("type") if isinstance(error, dict) else None
        reason = item.get("skip_reason") or item.get("reason") or item.get("error_type") or kind or outcome
        entry = {"stage": stage or item.get("stage", "unknown"), "outcome": outcome, "reason": str(reason)[:160]}
        counts[f"{entry['stage']}:{outcome}:{entry['reason']}"] += 1
        total += 1
        if len(details) < limit:
            for key in ("url", "http_status", "depth", "file", "recovery_file"):
                if item.get(key) is not None:
                    value = item[key]
                    entry[key] = value[:2048] if isinstance(value, str) else value
            details.append(entry)
    return {"counts": dict(counts), "details": details, "total": total,
            "omitted_details": total - len(details), "detail_limit": limit}
