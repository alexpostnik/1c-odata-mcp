"""Synthetic capture and publication-boundary regressions."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import build_1c_docs as docs


class PortableDocsTests(unittest.TestCase):
    def test_links_do_not_carry_authentication_or_executable_schemes(self):
        for link in ['vbscript:bad()', 'file:///secret', 'https://user:password@its.1c.ru/db/test', 'https://its.1c.ru/db/test?access_token=secret']:
            with self.subTest(link=link):
                self.assertEqual(docs.doc_link(link, 'https://its.1c.ru/db/test'), '')

    def test_standalone_import_and_repeat_are_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); raw=root/'raw'; raw.mkdir(); output=root/'corpus'
            for n,sid in enumerate(docs.EXPECTED_IDS):
                title='Введение' if sid==51 else (f'Глава {n}. Тест' if n<=39 else f'Приложение {sid}')
                html=f'<html><body><h1>{title}</h1><p>'+'Synthetic content. '*400+'</p></body></html>'
                (raw/f'{sid}.json').write_text(json.dumps(dict(source_id=str(sid),title=title,source_url=f'https://its.1c.ru/db/v8325doc/content/{sid}/hdoc',doc_html=html,html_length=len(html))))
            command=[sys.executable,str(SCRIPTS/'collect.py'),'developer',str(raw),'--output',str(output),'--observed','2026-01-01']
            run=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            graph=json.loads((output/'knowledge/graph.json').read_text())
            ids={n['id'] for n in graph['nodes']}
            self.assertEqual(len(ids),len(graph['nodes']))
            self.assertTrue(all(e['from'] in ids and e['to'] in ids for e in graph['edges']))
            before={str(p.relative_to(output)):p.read_bytes() for p in output.rglob('*') if p.is_file()}
            self.assertEqual(subprocess.run(command,capture_output=True).returncode,0)
            after={str(p.relative_to(output)):p.read_bytes() for p in output.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
            broken=json.loads((raw/'52.json').read_text()); broken['doc_html']=broken['doc_html'].replace('<h1>Глава 1. Тест</h1>','<h1>Different heading</h1>');broken['html_length']=len(broken['doc_html']);(raw/'52.json').write_text(json.dumps(broken))
            self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)
            self.assertEqual(before,{str(p.relative_to(output)):p.read_bytes() for p in output.rglob('*') if p.is_file()})

    def test_public_graph_contains_navigation_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); records=root/'knowledge/records/1c-8.3.25/developer-guide';records.mkdir(parents=True)
            data={'record_id':'onec-doc-example','title':'Example title','source_refs':['https://its.1c.ru/db/v8325doc/content/51/hdoc','https://github.com/private/repo/issues/1'],'summary':'CONFIDENTIAL ARTICLE BODY','sections':[{'heading':'Heading','summary':'SECRET EXCERPT','anchor':'one'}],'relations':[]}
            (records/'page.json').write_text(json.dumps(data))
            (root/'knowledge/graph.json').write_text(json.dumps({'nodes':[{'id':'record:onec-doc-example','type':'record','label':'Example title'},{'id':'private:user','type':'person','label':'PRIVATE NAME'}],'edges':[{'id':'x','from':'private:user','to':'record:onec-doc-example','type':'uses'}],'hyperedges':[]}))
            out=root/'public.json'
            run=subprocess.run([sys.executable,str(SCRIPTS/'export_navigation.py'),str(root),'--output',str(out)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            public=out.read_text()
            for marker in ['CONFIDENTIAL','SECRET','PRIVATE','private/repo','private:user']:
                self.assertNotIn(marker,public)
            result=json.loads(public);self.assertEqual(len(result['records']),1);self.assertEqual(result['edges'],[])

if __name__=='__main__':unittest.main()
