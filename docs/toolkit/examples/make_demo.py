#!/usr/bin/env python3
"""Generate invented article captures and build a reproducible local graph."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_1c_docs as docs
from collect import collect
from export_navigation import export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='New empty demo directory')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    raw = args.output / 'raw'
    raw.mkdir()
    (args.output / '.gitignore').write_text('*\n')
    for n, sid in enumerate(docs.EXPECTED_IDS):
        title = 'Введение' if sid == 51 else (f'Глава {n}. Учебный пример' if n <= 39 else f'Приложение {sid}')
        html = f'<html><body><h1>{title}</h1><p>' + 'Вымышленная учебная страница. 🧪 ' * 400 + '</p></body></html>'
        item = dict(source_id=str(sid), title=title,
                    source_url=f'https://its.1c.ru/db/v8325doc/content/{sid}/hdoc',
                    doc_html=html, html_length=len(html))
        (raw / f'{sid}.json').write_text(json.dumps(item, ensure_ascii=False))
    corpus = args.output / 'corpus'
    collect('developer', raw, corpus, '2026-01-01')
    graph = export(corpus)
    (args.output / 'navigation.json').write_text(json.dumps(graph, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'synthetic': True, 'records': len(graph['records']),
                      'nodes': len(graph['nodes']), 'edges': len(graph['edges'])}))


if __name__ == '__main__':
    main()
