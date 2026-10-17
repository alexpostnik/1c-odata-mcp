#!/usr/bin/env python3
"""Export the documentation subgraph as navigation, never article text or account data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

PREFIXES = ('knowledge/records/1c-8.3.25/', 'knowledge/records/1c-7.7/', 'knowledge/records/upgrade1c/', 'knowledge/records/poby/')
RELATIONS = {'part_of', 'has_part', 'has_page', 'references', 'see_also', 'covers_topic', 'has_section', 'contains_section', 'section_of', 'next_section'}


def public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        its = url.netloc == 'its.1c.ru' and re.fullmatch(r'/db/[A-Za-z0-9_-]+(?:/content/[0-9]+/hdoc(?:/[A-Za-z0-9_.-]+)?)?/?', url.path)
        poby = url.netloc == 'po.by' and re.fullmatch(r'/knowledge-base(?:-by)?/(?:[a-z0-9-]+/)?', url.path)
        if (url.scheme != 'https' or not (its or poby) or url.query or url.username or url.password):
            return None
        fragment = url.fragment if poby and re.fullmatch(r'faqOne[0-9]+', url.fragment) else ''
        return urlunsplit((url.scheme, url.netloc, url.path, '', fragment))
    except ValueError:
        return None


def export(root: Path) -> dict:
    graph = json.loads((root / 'knowledge/graph.json').read_text())
    records = []
    source_links = {}
    for prefix in PREFIXES:
        for path in sorted((root / prefix).rglob('*.json')):
            if prefix.endswith('/poby/') and path.name != 'knowledge-base-index.json':
                continue
            item = json.loads(path.read_text())
            urls = sorted({url for ref in item.get('source_refs', []) if (url := public_url(ref))})
            ident = item.get('record_id')
            title = item.get('title')
            if not (urls and isinstance(ident, str) and re.fullmatch(r'[a-z0-9-]+', ident) and isinstance(title, str)):
                continue
            records.append({'id': ident, 'title': title, 'source_urls': urls})
            for link in item.get('source_links', []) + item.get('articles', []) + item.get('faqs', []):
                url = public_url(link.get('source_url', link.get('href', link.get('index_href'))))
                label = link.get('title', link.get('label', link.get('visible_title')))
                if url and isinstance(label, str) and label.strip():
                    source_links[(ident, url)] = {'url': url, 'title': label.strip()[:200]}
    records.sort(key=lambda r: r['id'])
    allowed = {'record:'+r['id'] for r in records}
    # Section IDs are retained only where an exported document owns them.
    sections = {n['id'] for n in graph.get('nodes', []) if n.get('type') == 'section'
                and any(n['id'].startswith('section:'+r['id']+':') for r in records)}
    topic_ids = {'topic:'+t for t in ('bank', 'cash', 'payroll', 'purchases', 'services', 'counterparty-settlements')}
    topics = {n['id'] for n in graph.get('nodes', []) if n.get('type') == 'work_topic' and n.get('id') in topic_ids}
    allowed |= sections | topics
    nodes = [{'id':'record:'+r['id'], 'type':'record', 'label':r['title']} for r in records]
    nodes += [{k:n[k] for k in ('id','type','label')} for n in graph.get('nodes', []) if n['id'] in sections | topics]
    edges = [{'from':e['from'],'to':e['to'],'type':e['type']} for e in graph.get('edges', [])
             if e.get('from') in allowed and e.get('to') in allowed and e.get('type') in RELATIONS]
    linked_nodes = {}
    for (owner, url), link in sorted(source_links.items()):
        ident = 'source:' + hashlib.sha256(url.encode()).hexdigest()[:20]
        linked_nodes.setdefault(ident, {'id': ident, 'type': 'source_link', 'label': link['title'], 'url': url, 'body_status': 'not_distributed'})
        edges.append({'from': 'record:'+owner, 'to': ident, 'type': 'references'})
    nodes += list(linked_nodes.values())
    unique = {(e['from'],e['to'],e['type']):e for e in edges}
    return {'schema_version':1,'purpose':'Navigation metadata; article bodies and access grants are not distributed.',
            'records':records,'nodes':sorted(nodes,key=lambda n:n['id']),
            'edges':[unique[k] for k in sorted(unique)],'hyperedges':[]}


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=export(args.corpus)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'records':len(result['records']),'nodes':len(result['nodes']),'edges':len(result['edges'])}))


if __name__=='__main__':main()
