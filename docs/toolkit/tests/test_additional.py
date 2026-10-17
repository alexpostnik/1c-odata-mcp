"""Complete synthetic additional-manual import and transactional boundaries."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_1c_additions as additions
from collect import collect, staged_output


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class AdditionalTests(unittest.TestCase):
    def test_complete_additional_import_is_repeatable_and_retains_only_current_access_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); raw = root / 'raw'; output = root / 'corpus'
            books = []
            sid = 1000
            for book_index, (key, title) in enumerate(additions.PLATFORM_BOOKS.items()):
                count = [18, 18, 17, 17][book_index]
                pages = []
                for _ in range(count):
                    sid += 1; heading = f'Учебная страница {sid}'
                    url = f'https://its.1c.ru/db/v8325doc/content/{sid}/hdoc'
                    html = f'<html><body><h1>{heading}</h1><p>' + 'Synthetic documentation. ' * 300 + '</p></body></html>'
                    write(raw / 'platform' / key / f'{sid}.json', dict(source_id=sid, source_url=url, title=heading, collection=f'platform/{key}', doc_html=html, html_length=len(html)))
                    pages.append(dict(source_id=sid, title=heading, source_url=url))
                books.append(dict(key=key, title=title, browse='/db/v8325doc', expected=count, pages=pages))
            write(raw / 'indexes/platform-toc.json', dict(observed_at='2026-01-01', books=books))
            for sid in additions.HOOSN_IDS:
                heading = f'Учебная операция {sid}'
                html = f'<html><body><h1>{heading}</h1><p>' + 'Synthetic index. ' * 30 + '</p></body></html>'
                write(raw / 'hoosn-index' / f'{sid}.json', dict(source_id=sid, source_url=f'https://its.1c.ru/db/hoosn/content/{sid}/hdoc', title=heading, collection='hoosn-index', doc_html=html, html_length=len(html)))
            write(raw / 'indexes/metod77-accounting-edition-4-5.json', dict(metadata_only=True, source_id='demo', title='Учебный индекс', source_url='https://its.1c.ru/db/metod77', platform_version='unknown', configuration_release='unknown', items=[dict(title=f'Учебная ссылка {i}', source_url=f'https://its.1c.ru/db/metod77/content/{i}/hdoc') for i in range(1, 103)]))
            result = collect('additional', raw, output, '2026-01-01')
            self.assertEqual(result['platform_pages'], 70)
            self.assertEqual(result['operation_index_pages'], 18)
            before = snapshot(output)
            collect('additional', raw, output, '2026-01-01')
            self.assertEqual(before, snapshot(output))
            # Simulate a previous split page whose extra part no longer exists.
            graph_path = output / 'knowledge/graph.json'
            graph = json.loads(graph_path.read_text())
            stale = 'record:onec-8325-platform-user-guide-page-1001-part-99'
            graph['nodes'].append(dict(id=stale, type='record', label='Old synthetic part'))
            graph['edges'].append(dict(id='old-edge', type='part_of', **{'from': stale, 'to': 'record:onec-8325-platform-user-guide-page-1001'}))
            write(graph_path, graph)
            collect('additional', raw, output, '2026-01-01')
            self.assertEqual(before, snapshot(output))
            index = json.loads((output / 'knowledge/records/1c-7.7/accounting-edition-4-5/index.json').read_text())
            self.assertTrue(all(link['body_status'] == 'index-only-not-captured' for link in index['source_links']))
            self.assertEqual(index['provenance']['source_version'], 'unknown')
            bad = raw / 'hoosn-index/38.json'
            data = json.loads(bad.read_text()); data['doc_html'] += 'sessionid=PRIVATE'; data['html_length'] = len(data['doc_html']); write(bad, data)
            with self.assertRaisesRegex(ValueError, 'credential'):
                collect('additional', raw, output, '2026-01-01')
            self.assertEqual(before, snapshot(output))

    def test_refuses_unmanaged_output_symlink_and_concurrent_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); existing = root / 'important'; existing.mkdir()
            (existing / 'keep.txt').write_text('Keep me')
            with self.assertRaisesRegex(ValueError, 'not a managed'):
                with staged_output(existing): pass
            link = root / 'linked'; link.symlink_to(existing)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                with staged_output(link): pass
            output = root / 'new'
            with staged_output(output):
                with self.assertRaisesRegex(ValueError, 'Another import'):
                    with staged_output(output): pass
            self.assertEqual((existing / 'keep.txt').read_text(), 'Keep me')


if __name__ == '__main__': unittest.main()
