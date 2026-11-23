"""Preview/replay explicitly selected recovery files; never scrape again."""
from storage import expand_document, find_source
import argparse
import gzip
import json
import os
from pathlib import Path

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, WriteConcernError
from storage import database_preflight
from scr_prp import save_to_mongodb

MAX_RECOVERY_BYTES = 64 * 1024 * 1024


def recovery_files(target):
    target = Path(target)
    if target.is_symlink():
        raise ValueError("Recovery target cannot be a symlink")
    if target.is_file():
        if not target.name.endswith(".json.gz"):
            raise ValueError("Expected a .json.gz recovery file")
        return [target.resolve()]
    if not target.is_dir():
        raise ValueError("Recovery target does not exist")
    root = target.resolve()
    files = []
    for path in root.rglob("*.json.gz"):
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            continue
        path.resolve().relative_to(root)
        files.append(path)
    return sorted(files)


def recover_file(path, database, collection=None, *, apply=False, overwrite=False):
    path = Path(path)
    result = {"file": str(path), "outcome": "failed"}
    lock = path.with_name(".replay.lock")
    locked = False
    try:
        if path.is_symlink():
            raise ValueError("Symlink recovery files are not allowed")
        if apply:
            with lock.open("x"):
                pass
            locked = True
        with gzip.open(path, "rb") as handle:
            raw = handle.read(MAX_RECOVERY_BYTES + 1)
        if len(raw) > MAX_RECOVERY_BYTES:
            raise ValueError("Recovery file exceeds decompressed size limit")
        envelope = json.loads(raw)
        if (not isinstance(envelope, dict) or not isinstance(envelope.get("file_name"), str)
                or not envelope["file_name"] or "content" not in envelope):
            raise ValueError("Invalid recovery envelope")
        if envelope.get("database", database) != database:
            raise ValueError("Recovery database does not match --db")
        result["document_key"] = envelope["file_name"]
        if not apply:
            result["outcome"] = "ready"
            return result
        if collection is None:
            raise ValueError("Apply requires a database collection")
        key, content = envelope["file_name"], envelope["content"]
        if isinstance(content, dict) and content.get("source_id"):
            existing = find_source(collection, key)
            if existing is not None and existing.get("content") != content and not overwrite:
                result.update(outcome="conflict", message="Different stored content; file retained.")
                return result
            save_to_mongodb(collection, key, content)
        elif overwrite:
            save_to_mongodb(collection, key, content)
        else:
            existing = collection.find_one({"file_name": key})
            if existing is None:
                try:
                    saved = collection.insert_one({"file_name": key, "content": content})
                    if not saved.acknowledged:
                        raise WriteConcernError("Database did not acknowledge recovery")
                except DuplicateKeyError:
                    existing = collection.find_one({"file_name": key})
                    if existing is None:
                        raise
            if existing is not None and expand_document(collection, existing.get("content")) != content:
                result.update(outcome="conflict", message="Different stored content; file retained. Use --overwrite deliberately.")
                return result
        # A successful acknowledged insert/upsert, or matching stored content,
        # makes this particular recovery copy redundant. No directory deletion.
        try:
            path.unlink()
        except OSError as exc:
            result.update(outcome="saved_file_retained", error_type=type(exc).__name__)
            return result
        result["outcome"] = "recovered"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        # Do not echo database connection strings or exception credentials.
        result["message"] = "Recovery not completed; source file retained"
    finally:
        if locked:
            lock.unlink(missing_ok=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="One .json.gz file or an explicitly selected run directory")
    parser.add_argument("--db", required=True, help="Target MongoDB database (required for older envelopes too)")
    parser.add_argument("--apply", action="store_true", help="Save documents and remove confirmed recovery copies")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing different stored content")
    args = parser.parse_args(argv)
    if args.overwrite and not args.apply:
        parser.error("--overwrite requires --apply")
    client = None
    try:
        files = recovery_files(args.path)
        collection = None
        if args.apply and files:
            client = MongoClient(os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017/"),
                                 serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000)
            collection = database_preflight(client, args.db)
        results = [recover_file(path, args.db, collection, apply=args.apply, overwrite=args.overwrite)
                   for path in files]
        print(json.dumps({"mode": "apply" if args.apply else "preview", "files": results}, indent=2))
        return int(any(item["outcome"] not in ("ready", "recovered") for item in results))
    except Exception as exc:
        print(json.dumps({"outcome": "failed", "error_type": type(exc).__name__,
                          "message": "Preflight or target validation failed; recovery files untouched"}))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
