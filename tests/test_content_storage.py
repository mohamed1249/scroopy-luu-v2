import unittest
from unittest.mock import MagicMock
from storage import save_content_group, expand_document, brief_diagnostics
from pull_schema import source_document


class ContentStorageTests(unittest.TestCase):
    def test_empty_text_does_not_write_or_delete(self):
        collection = MagicMock()
        doc = source_document('https://example.com', '  \n', pull_id='p', run_id='r')
        self.assertFalse(save_content_group(collection, 'source', doc))
        self.assertEqual(collection.mock_calls, [])

    def test_hash_mismatch_never_writes(self):
        collection = MagicMock()
        doc = source_document('https://example.com', 'Text', pull_id='p', run_id='r')
        doc['content'] = 'Changed'
        with self.assertRaises(ValueError):
            save_content_group(collection, "source", doc)
        collection.database.__getitem__.assert_not_called()

    def test_missing_blob_is_reported(self):
        collection = MagicMock()
        collection.database['contents'].find_one.return_value = None
        with self.assertRaisesRegex(ValueError, 'missing'):
            expand_document(collection, {'content_ref': 'missing'})

    def test_brief_log_counts_all_but_bounds_details(self):
        pages = [{'url': 'https://example.com/'+str(i), 'outcome': 'skipped',
                  'skip_reason': 'run_download_budget_exhausted', 'text': 'excluded'} for i in range(300)]
        pages.append({'url': 'https://example.com/fail', 'outcome': 'failed',
                      'http_status': 403, 'error': {'type': 'HTTPError', 'message': 'secret'}})
        result = brief_diagnostics(pages, [])
        self.assertEqual(result['total'], 301)
        self.assertEqual(result['omitted_details'], 101)
        self.assertEqual(result['details'][0]['http_status'], 403)
        self.assertEqual(result['counts']['webpage:skipped:run_download_budget_exhausted'], 300)
        self.assertNotIn('secret', str(result))
        self.assertNotIn('excluded', str(result))
