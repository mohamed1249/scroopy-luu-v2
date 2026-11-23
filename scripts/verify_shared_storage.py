"""Verify shared text in a disposable local database; never touch user datasets."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
import uuid
import gzip
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pymongo import MongoClient
from scr_prp import save_to_mongodb
from storage import database_preflight, find_source
from pull_schema import source_document
from recover import recover_file


def main():
    name = 'scroopy_dedup_verify_' + uuid.uuid4().hex
    with MongoClient(os.environ.get('MONGODB_URI', 'mongodb://127.0.0.1:27017/'), serverSelectionTimeoutMS=3000) as client:
        try:
            collection = database_preflight(client, name)
            docs = [source_document(f'https://fixture.example/{i}', 'Same text', pull_id='pull', run_id='run') for i in range(8)]
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda pair: save_to_mongodb(collection, str(pair[0]), pair[1]), enumerate(docs)))
            assert collection.count_documents({}) == 1
            assert len(collection.find_one()['sources']) == 8
            assert client[name].contents.count_documents({}) == 0
            assert collection.count_documents({'content.content': {'$exists': True}}) == 0
            updated = source_document('https://fixture.example/0', 'New text', pull_id='pull', run_id='next')
            save_to_mongodb(collection, '0', updated)
            assert find_source(collection, '1')['content']['content'] == 'Same text'
            assert find_source(collection, '0')['content']['content'] == 'New text'
            assert collection.count_documents({}) == 2
            empty = source_document('https://fixture.example/0', '   ', pull_id='pull', run_id='empty')
            save_to_mongodb(collection, '0', empty)
            assert find_source(collection, '0')['content']['content'] == 'New text'
            save_to_mongodb(collection, 'empty', empty)
            assert collection.count_documents({}) == 2
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'recovery.json.gz'
                for key, doc in [('1', docs[1]), ('recovered', docs[2])]:
                    with gzip.open(path, 'wt', encoding='utf-8') as handle:
                        json.dump({'file_name': key, 'content': doc, 'database': name}, handle)
                    assert recover_file(path, name, collection, apply=True)['outcome'] == 'recovered'
                assert collection.count_documents({}) == 2
            print('PASS: concurrent duplicate text, source preservation, independent refresh, and recovery deduplication')
        finally:
            assert name.startswith('scroopy_dedup_verify_') and len(name) == 53
            client.drop_database(name)
            print('Removed only the disposable verification database')


if __name__ == '__main__':
    main()
