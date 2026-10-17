#!/usr/bin/env python3
"""Offline, deterministic statement graphs; no network or 1C operations."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from payment_text import prepare_payment_text, writable_payment_text

PACKAGE = Path(__file__).resolve().parents[1]
DECIMAL = re.compile(r'^-?(0|[1-9][0-9]*)(\.[0-9]+)?$')
RELATIONS = {
    'CONTAINS': ({'Statement'}, {'Operation'}),
    'FOR_ACCOUNT': ({'Statement'}, {'Account'}),
    'IN_CURRENCY': ({'Statement'}, {'CurrencyContext'}),
    'OWNED_BY': ({'Account'}, {'Party'}), 'SERVICED_BY': ({'Account'}, {'Bank'}),
    'PAYER': ({'Operation'}, {'Party'}), 'PAYEE': ({'Operation'}, {'Party'}),
    'FROM_ACCOUNT': ({'Operation'}, {'Account'}), 'TO_ACCOUNT': ({'Operation'}, {'Account'}),
    'MENTIONS': ({'Operation'}, {'DocumentReference'}),
    'HAS_COMPONENT': ({'Operation'}, {'AmountComponent'}), 'HAS_TAX': ({'Operation'}, {'TaxDetail'}),
    'DUPLICATE_MEMBER': ({'DuplicateDecision'}, {'Operation', 'Statement'}),
    'HAS_CASH_SYMBOL': ({'Operation'}, {'CashSymbol'}), 'ON_BEHALF_OF': ({'Operation'}, {'Party'}),
}


def decimal(value):
    if not isinstance(value, str) or not DECIMAL.fullmatch(value):
        raise ValueError('money must be a canonical decimal string')
    return Decimal(value)


def decimal_text(value):
    """Serialize computed values in fixed notation; source input stays strict."""
    return format(value, 'f')


def normalize_decimal(raw):
    """Only formatting normalization. No scale conversion or guessed separators."""
    if not isinstance(raw, str):
        raise ValueError('raw amount must be text')
    cleaned = raw.strip().replace('\u00a0', '').replace(' ', '')
    if ',' in cleaned and '.' in cleaned:
        raise ValueError('ambiguous decimal separators')
    cleaned = cleaned.replace(',', '.')
    if not DECIMAL.fullmatch(cleaned):
        raise ValueError('unrecognized monetary text')
    return cleaned


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', errors='backslashreplace') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def write_new_pair(first_path, first_data, second_path, second_data):
    """Reserve both exclusive destinations before writing; clean handled failures.

    This is not crash atomicity or an atomic view for concurrent readers.
    """
    import os
    requested = [Path(first_path), Path(second_path)]
    if any(os.path.lexists(path) for path in requested):
        raise FileExistsError('paired output destination already exists')
    paths = [path.resolve() for path in requested]
    if paths[0] == paths[1]:
        raise ValueError('paired output paths alias each other')
    if any(os.path.lexists(path) for path in paths):
        raise FileExistsError('paired output destination already exists')
    reserved = []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open('x', encoding='utf-8', errors='backslashreplace')
            stat = os.fstat(stream.fileno())
            reserved.append((path, stream, (stat.st_dev, stat.st_ino)))
        for (_, stream, _), data in zip(reserved, (first_data, second_data)):
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
        for _, stream, _ in reserved:
            stream.close()
    except Exception:
        for path, stream, identity in reserved:
            try:
                stream.close()
            except Exception:
                pass
            try:
                stat = path.lstat()
                if (stat.st_dev, stat.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        raise


def recognize_source_identifier(raw, kind, glyphs_unknown=False):
    """Never recognize a rendered token when source glyphs are unresolved."""
    import bank_identifiers as bi
    if not glyphs_unknown:
        return bi.recognize(raw, kind)
    value = bi.recognize(None, kind)
    value.update(raw=raw, format_status='unknown', diagnostics=['source_identifier_glyphs_unresolved'])
    return value


def node_identifier(node, kind):
    value = node.get('properties', {}).get('identifier', {})
    evidence = [o for o in node.get('observations', []) if o['field'] == 'identifier_glyphs_raw']
    unresolved = bool(evidence and all(o['raw'] == value.get('raw') and
                      o['normalized'] is None and o['status'] == 'unknown' for o in evidence))
    return recognize_source_identifier(value.get('raw'), kind, unresolved)


def extract(pdf, engine='auto'):
    """Separate extraction QA. Alternative local engines help resolve layout loss."""
    if engine=='auto':engine='poppler' if shutil.which('pdftotext') and shutil.which('pdfinfo') else 'pypdf'
    if engine=='poppler':
        for program in ('pdftotext','pdfinfo'):
            if not shutil.which(program):raise ValueError('missing local PDF extraction tool: '+program)
        info=subprocess.run(['pdfinfo',str(pdf)],capture_output=True,check=True).stdout.decode('utf-8',errors='replace')
        match=re.search(r'^Pages:\s+(\d+)',info,re.MULTILINE)
        if not match:raise ValueError('PDF page count unavailable')
        count=int(match.group(1))
        raw=subprocess.run(['pdftotext','-layout','-enc','UTF-8',str(pdf),'-'],capture_output=True,check=True).stdout.decode('utf-8')
        pages=raw.split('\f')
        if pages and not pages[-1].strip():pages.pop()
        if len(pages)!=count:raise ValueError('text page boundaries do not match PDF count')
    elif engine=='pypdf':
        from pypdf import PdfReader
        reader=PdfReader(str(pdf));count=len(reader.pages)
        pages=[page.extract_text(extraction_mode='layout') or '' for page in reader.pages]
    elif engine=='pdfplumber':
        import pdfplumber
        with pdfplumber.open(pdf) as reader:
            count=len(reader.pages);pages=[page.extract_text(layout=True) or '' for page in reader.pages]
    else:raise ValueError('unknown local extraction engine')
    return {'source':{'sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'name':pdf.name,'page_count':count},
            'extractor':engine+'-layout-v1','pages':[
                {'page':i,'raw_text':text,'nonspace_characters':len(re.sub(r'\s','',text)),
                 'coverage':'text_available' if text.strip() else 'requires_ocr_or_visual','row_assembly':'not_reviewed'} for i,text in enumerate(pages,1)]}


def build(document, *, diagnostic=False):
    """Build standalone banking facts. No PDF metadata or per-field PDF links."""
    prefix = 'dataset:' + document['dataset_id']
    if not isinstance(document['dataset_id'],str) or not document['dataset_id'].strip():
        raise ValueError('opaque stable dataset_id is required')
    nodes, edges, aliases = [], [], {}
    def node(entry, typ, props):
        alias=entry['alias']
        if alias in aliases:raise ValueError('duplicate local alias')
        ident=entry.get('id',prefix+':'+alias)
        aliases[alias]=ident
        nodes.append({'id':ident,'type':typ,'properties':props,'observations':entry.get('observations',[]),
                      'unrecognized_fields':entry.get('unrecognized_fields',[])})
    for entry in document['entities']:
        node(entry,entry['type'],dict(entry['properties']))
    for statement in document['statements']:
        props=dict(statement['properties'])
        blank=[o for o in statement.get('observations',[]) if o['field']=='opening' and isinstance(o['raw'],str) and not o['raw'].strip()]
        if props['opening'] is None and blank and props['first_account_evidence']:
            props['opening']='0';props['opening_origin']='first_statement_blank_zero'
            statement=dict(statement);statement['observations']=[dict(o,normalized='0',status='observed') if o in blank else o for o in statement.get('observations',[])]
        node(statement,'Statement',props)
        for ordinal,row in enumerate(statement['operations'],1):
            entry=dict(row);entry['alias']=row.get('alias',statement['alias']+':row:'+str(ordinal))
            op=dict(row['properties']);op['ordinal']=ordinal;op['statement_id']=statement['alias']
            op['payment1c_text'] = prepare_payment_text(op['purpose_raw'], op.get('purpose_status', 'unknown'))
            node(entry,'Operation',op)
    single={'statement_id','account_id','currency_context_id','canonical_id','party_id'}
    def resolve(value,key=None):
        if isinstance(value,dict):return {k:resolve(v,k) for k,v in value.items()}
        if isinstance(value,list):return [aliases.get(v,v) if key=='member_ids' and isinstance(v,str) else resolve(v) for v in value]
        return aliases.get(value,value) if key in single and isinstance(value,str) else value
    nodes=resolve(nodes)
    def edge(typ,left,right):
        edges.append({'id':prefix+':edge:'+str(len(edges)+1),'type':typ,'from':left,'to':right})
    for n in nodes:
        prop=n['properties']
        if n['type']=='Statement':
            edge('FOR_ACCOUNT',n['id'],prop['account_id']);edge('IN_CURRENCY',n['id'],prop['currency_context_id'])
        if n['type']=='Operation':edge('CONTAINS',prop['statement_id'],n['id'])
    for e in document.get('relations',[]):edge(e['type'],aliases.get(e['from'],e['from']),aliases.get(e['to'],e['to']))
    graph = {'schema_version':1,'nodes':nodes,'edges':edges}
    if not diagnostic:
        materialize_processing(graph)
        require_processing_admission(graph)
    return graph


def combine(graphs):
    """Merge explicit common business IDs only; names/amounts never identify nodes."""
    nodes,edges={},{}
    for graph in graphs:
        require_processing_admission(graph)
        for n in graph['nodes']:
            if n['id'] in nodes and nodes[n['id']]['properties']!=n['properties']:
                raise ValueError('conflicting properties for explicitly shared ID')
            if n['id'] not in nodes:nodes[n['id']]=copy.deepcopy(n)
            old=nodes[n['id']]
            for key in ('observations','unrecognized_fields'):
                old[key]=[json.loads(v) for v in sorted({json.dumps(x,sort_keys=True,ensure_ascii=False) for x in old[key]+n[key]})]
        for e in graph['edges']:
            if e['id'] in edges and edges[e['id']]!=e:raise ValueError('conflicting edge ID')
            edges[e['id']]=e
    result = {'schema_version':1,'nodes':[nodes[k] for k in sorted(nodes)],'edges':[edges[k] for k in sorted(edges)]}
    materialize_processing(result)
    require_processing_admission(result)
    return result


def annotate_rko(graph, annotations, private_evidence=None):
    """Add safe TEST RKO observations; never infer a completed correction.

    annotations maps existing Operation IDs to the closed one_c_rko shape.
    Private evidence is supplied by the assigned writer from actual outcomes;
    neither this pure helper nor schema validation authenticates those receipts.
    GUIDs, personnel, receipts and arbitrary evidence text are never copied.
    """
    require_processing_admission(graph)
    if not validate(graph)['valid']:
        raise ValueError('input graph must validate')
    if not isinstance(annotations, dict) or (private_evidence is not None and not isinstance(private_evidence, dict)):
        raise ValueError('annotations/evidence must be mappings')
    result = copy.deepcopy(graph)
    operations = {n['id']: n for n in result['nodes'] if n['type'] == 'Operation'}
    schema = json.loads((PACKAGE/'references/graph.schema.json').read_text())
    from jsonschema import Draft202012Validator, FormatChecker
    validator = Draft202012Validator({'$defs':schema['$defs'], '$ref':'#/$defs/OneCRko'}, format_checker=FormatChecker())
    for ident, annotation in annotations.items():
        if ident not in operations or operations[ident]['properties']['processing']['status'] != 'ready' or not validator.is_valid(annotation):
            raise ValueError('unknown operation or invalid safe RKO annotation')
        if annotation['status'] == 'loaded_verified':
            evidence = (private_evidence or {}).get(ident, {})
            if not isinstance(evidence, dict) or any(evidence.get(k) is not True for k in
                ('exact_source_target', 'post_success', 'readback_success', 'same_test_claim')):
                raise ValueError('loaded_verified requires actual exact-link/Post/readback/claim evidence')
        operations[ident]['properties']['one_c_rko'] = copy.deepcopy(annotation)
    if not validate(result)['valid']:
        raise ValueError('annotated graph must validate')
    return result


def _schema_errors(graph):
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads((PACKAGE/'references/graph.schema.json').read_text())
    return ['schema violation at ' + '/'.join(map(str,e.absolute_path)) for e in
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(graph)]


def bank_cell_known(op, field):
    """A null value is zero only for a proven blank, never unreadable text."""
    import archive_adapter as adapter
    value = op['properties'][field]
    observations = [o for o in op.get('observations', []) if o['field'] == field]
    if not observations and 'purpose_status' not in op['properties']:
        return True  # Legacy structural diagnostics; no processing admission.
    if len(observations) != 1:
        return False
    o = observations[0]
    raw = o.get('raw')
    if not isinstance(raw, str) or o.get('normalized') != value:
        return False
    if not raw.strip():
        return o['status'] == 'absent' and value is None
    parsed = adapter.component_money(raw)
    return o['status'] == 'observed' and parsed is not None and parsed == value


def valid_clarification(op, role, source_observation, metadata, identity_gaps):
    """Separate owner evidence bound to the unchanged original row/raw detail.

    No inference of authority from a claim: the caller must retain the actual
    owner answer/confirmation referenced here in its private evidence ledger.
    """
    import archive_adapter as adapter
    answer = op['properties'].get('counterparty_clarification')
    if not identity_gaps or not isinstance(answer, dict) or not role or source_observation is None:
        return False
    if answer.get('role') != role or answer.get('original_detail_raw') != source_observation['raw']:
        return False
    if not adapter.readable_name(answer.get('name_raw')) or not adapter.readable_unp(answer.get('unp_raw')):
        return False
    if any(not isinstance(answer.get(k), str) or not answer[k].strip() for k in
           ('answered_by', 'answered_at', 'answer_ref', 'confirmation_ref')):
        return False
    try:
        stamp = datetime.fromisoformat(answer['answered_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None: return False
    except ValueError:
        return False
    # An answer cannot silently change an already unambiguous readable fact.
    for key, label, readable in [('name_raw', 'Наименование корреспондента:', adapter.readable_name),
                                 ('unp_raw', 'УНП '+('плательщика:' if role == 'payer' else 'получателя:'), adapter.readable_unp)]:
        values = metadata.get(label, [])
        if 'modern_fields' in op['properties'] and key=='name_raw':
            import modern_adapter as modern
            if modern.source_fields(op).get('name_status')!='observed':values=[]
        if len(values) == 1:
            value = values[0] if key == 'name_raw' else values[0].replace(' ', '')
            if readable(value) and value != answer[key]: return False
    return True


def valid_owner_type(op, role, source_observation):
    """Owner classification stays separate from printed identity facts.

    References require an actual owner answer in the caller's private ledger;
    a structurally valid annotation cannot authenticate that external evidence.
    """
    answer = op['properties'].get('owner_counterparty_type')
    if not isinstance(answer, dict) or source_observation is None or not role:
        return False
    if (answer.get('operation_id') != op['id'] or answer.get('role') != role or
            answer.get('original_detail_raw') != source_observation['raw'] or
            answer.get('person_type') not in ('physical_person', 'legal_entity') or
            answer.get('origin') != 'owner_confirmed'):
        return False
    if any(not isinstance(answer.get(k), str) or not answer[k].strip() for k in
           ('answered_by', 'answered_at', 'answer_ref', 'confirmation_ref')):
        return False
    try:
        return datetime.fromisoformat(answer['answered_at'].replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def owner_physical_blank_unp(op, role, source_observation, metadata, nodes,
                             role_links, holder_ids, modern_profile, sf):
    """Allow only an actually blank same-role UNP for an owner-confirmed person.

    Check name, Party and role independently before suppressing aggregate gaps
    caused by blank UNP. Printed zero/damage/duplicates are never blank.
    """
    import archive_adapter as adapter
    if not valid_owner_type(op, role, source_observation):
        return False
    if op['properties']['owner_counterparty_type']['person_type'] != 'physical_person':
        return False
    cp = op['properties'].get('counterparty') or {}
    values = metadata.get('УНП '+('плательщика:' if role == 'payer' else 'получателя:'), [])
    names = metadata.get('Наименование корреспондента:', [])
    if (len(values) != 1 or not isinstance(values[0], str) or values[0].strip() or
            cp.get('unp_raw') is not None or cp.get('role') != role or
            not adapter.readable_name(cp.get('name_raw')) or
            names != [cp['name_raw']] or len(role_links) != 1 or
            role_links[0]['to'] != cp.get('party_id')):
        return False
    party = nodes.get(cp.get('party_id'))
    if not party or party['type'] != 'Party' or party['id'] in holder_ids:
        return False
    pp = party['properties']
    if pp.get('role_raw') != role or pp.get('name_raw') != cp['name_raw'] or pp.get('unp_raw') is not None:
        return False
    for key in ('name_raw', 'unp_raw'):
        obs = [o for o in party.get('observations', []) if o['field'] == key]
        if len(obs) != 1 or not isinstance(obs[0]['raw'], str) or obs[0]['normalized'] != cp[key]:
            return False
        if key == 'unp_raw':
            if obs[0]['raw'].strip() or obs[0]['status'] not in ('unknown', 'absent'):
                return False
        elif obs[0]['status'] != 'observed':
            return False
        elif modern_profile:
            import modern_adapter as modern
            if obs[0]['raw'] != sf.get('name_raw') or not modern.name_observation_matches(obs[0]['raw'], cp[key]):
                return False
        elif obs[0]['raw'] != cp[key]:
            return False
    return True


NON_IDENTITY_GAPS = {'bank_identifier_unusable', 'row_fields_or_boundary_unresolved',
                     'purpose_boundary_unresolved', 'duplicate_review_unresolved',
                     'modern_unknown_metadata', 'modern_document_date_unresolved',
                     'modern_transaction_date_unresolved', 'modern_metadata_source_unresolved',
                     'modern_operation_code_unresolved', 'modern_conflicting_urgency',
                     'modern_conflicting_acceptance', 'owner_type_missing_or_conflicting_provenance'}


def operation_identifier_gaps(op, nodes, account_links):
    """Retained account spelling never grants readiness on a failed check."""
    import bank_identifiers as bi
    for ident in account_links.get(op['id'], ()):
        node = nodes.get(ident, {})
        value = node.get('properties', {}).get('identifier')
        if node.get('type') == 'Account' and value is not None:
            if value != node_identifier(node, 'account') or not bi.usable(value):
                return ['bank_identifier_unusable']
    return []


def identity_admission(graph):
    """Prove source field types, then independently assess every retained row.

    This checks source-evidence consistency, never external authenticity.
    A present but blank field is a row gap; an unproven type blocks the file.
    """
    import archive_adapter as adapter
    nodes = {n['id']: n for n in graph.get('nodes', [])}
    edges = graph.get('edges', [])
    holder_ids = set(); contains_by_operation = {}; role_edges = {}; account_links = {}
    # Retain all matching edges: duplicate and contradictory links stay visible.
    for edge in edges:
        if edge['type'] == 'OWNED_BY':
            holder_ids.add(edge['to'])
        if edge['type'] == 'CONTAINS':
            contains_by_operation.setdefault(edge['to'], []).append(edge)
        if edge['type'] in ('FROM_ACCOUNT', 'TO_ACCOUNT'):
            account_links.setdefault(edge['from'], []).append(edge['to'])
        if edge['type'] in ('PAYER', 'PAYEE'):
            role_edges.setdefault((edge['from'], edge['type']), []).append(edge)
    import counterparty_identity as ci
    official_role_conflicts = ci.role_collisions([a for n in nodes.values() if n['type'] == 'Operation' for a in n['properties'].get(ci.ROLE_PROPERTY, [])])
    identity_conflicts = ci.collisions([n['properties'][ci.PROPERTY] for n in nodes.values() if n['type'] == 'Operation' and ci.PROPERTY in n['properties']])
    statements = []
    for st in (n for n in nodes.values() if n['type'] == 'Statement'):
        operations = [n for n in nodes.values() if n['type'] == 'Operation' and
                      n['properties']['statement_id'] == st['id']]
        import modern_adapter as modern
        modern_profile=any(o['field']=='modern_anatomy_profile' for o in st.get('observations',[]))
        modern_supported=modern_profile and modern.format_supported(st,operations)
        source = {}; source_observations = {}; labels = set()
        for op in operations:
            observations = [o for o in op.get('observations', []) if o['field'] == 'identity_detail_raw']
            if len(observations) != 1 or not isinstance(observations[0]['raw'], str):
                source[op['id']] = ({}, [], ['original_identity_detail_missing'])
                continue
            observation = observations[0]; source_observations[op['id']] = observation; lines = observation['raw'].split('\n')
            if modern_profile:
                sf=modern.source_fields(op)
                meta={'Наименование корреспондента:':[sf['name_identity_raw']] if sf.get('name_identity_raw') is not None else [],
                      'УНП '+('плательщика:' if op['properties']['direction']=='inflow' else 'получателя:'):[sf.get('unp_raw')] if sf.get('unp_raw') is not None else []}
                assertions=[];gaps=list(sf['gaps'])
            else:
                meta, assertions, _, gaps = adapter.metadata_fields(lines, [{} for _ in lines])
            if observation['status'] != 'observed':
                gaps.append('identity_source_boundary_or_glyphs_unresolved')
            source[op['id']] = (meta, assertions, gaps)
            labels.update((x['label_raw'], adapter.IDENTITY_FIELD_LABELS[x['label']])
                          for x in assertions if x['label'] in adapter.IDENTITY_FIELD_LABELS)
        modern_diagnostic=any(o['field']=='diagnostic_anatomy_profile' and o['normalized']=='belarusbank-landscape-diagnostic-v1' for o in st.get('observations',[]))
        available = [] if modern_diagnostic else sorted({o['normalized'] for o in st.get('observations', [])
                            if o['field'] == 'counterparty_field_type' and o['status'] == 'observed'
                            and (o['raw'], o['normalized']) in labels})
        if modern_supported:
            available=['counterparty_full_name','payer_unp','payee_unp']
        directions = {op['properties']['direction'] for op in operations}
        required = {'counterparty_full_name'}
        if 'inflow' in directions: required.add('payer_unp')
        if 'outflow' in directions: required.add('payee_unp')
        if not directions or 'unknown' in directions: required.update(('payer_unp', 'payee_unp'))
        missing_types = sorted(required - set(available))
        p = st['properties']
        coverage_gaps = []
        if any(o['field'] == 'unassigned_business_raw' and o.get('raw') for o in st.get('observations', [])):
            coverage_gaps.append('unassigned_original_business_text')
        if p.get('expected_operations') != len(operations):
            coverage_gaps.append('original_row_count_missing_or_mismatched')
        if sorted(o['properties']['ordinal'] for o in operations) != list(range(1, len(operations)+1)):
            coverage_gaps.append('original_row_sequence_incomplete')
        for op in operations:
            contains = contains_by_operation.get(op['id'], [])
            if len(contains) != 1 or contains[0]['from'] != st['id']:
                coverage_gaps.append('original_row_membership_ambiguous')
        rows = []
        for op in operations:
            p = op['properties']; cp = p.get('counterparty'); gaps = []
            sf = modern.source_fields(op) if modern_profile else {}
            role = {'inflow': 'payer', 'outflow': 'payee'}.get(p['direction'])
            metadata, assertions, source_gaps = source[op['id']]
            gaps.extend(source_gaps)
            if not isinstance(cp, dict):
                gaps.append('missing_counterparty_contract')
            else:
                if not role or cp.get('role') != role: gaps.append('wrong_or_unknown_counterparty_role')
                if cp.get('status') != 'observed': gaps.append('counterparty_not_source_observed')
                if not adapter.readable_name(cp.get('name_raw')): gaps.append('missing_or_unreadable_full_name')
                if not adapter.readable_unp(cp.get('unp_raw')): gaps.append('missing_or_unreadable_unp')
                name_values = metadata.get('Наименование корреспондента:', [])
                unp_values = metadata.get('УНП '+('плательщика:' if role == 'payer' else 'получателя:'), [])
                if len(name_values) != 1 or name_values[0] != cp.get('name_raw'):
                    gaps.append('full_name_source_assertion_missing_or_conflicting')
                if len(unp_values) != 1 or not adapter.observed_unp_matches(unp_values[0], cp.get('unp_raw')):
                    gaps.append('unp_source_assertion_missing_or_conflicting')
                party = nodes.get(cp.get('party_id'))
                role_links = role_edges.get((op['id'], (role or '').upper()), [])
                if len(role_links) != 1 or role_links[0]['to'] != cp.get('party_id'):
                    gaps.append('counterparty_role_link_missing_or_ambiguous')
                if not party or party['type'] != 'Party' or party['id'] in holder_ids:
                    gaps.append('counterparty_party_missing_or_holder_substitution')
                else:
                    pp = party['properties']
                    if (pp.get('role_raw') != role or pp.get('identity_status') != 'observed' or
                            pp.get('name_raw') != cp.get('name_raw') or pp.get('unp_raw') != cp.get('unp_raw')):
                        gaps.append('counterparty_party_disagreement')
                    for key in ('name_raw', 'unp_raw'):
                        observations = [o for o in party.get('observations', []) if o['field'] == key]
                        if (len(observations) != 1 or observations[0]['status'] != 'observed' or
                                observations[0]['normalized'] != cp.get(key) or
                                not isinstance(observations[0]['raw'], str) or
                                (not adapter.observed_unp_matches(observations[0]['raw'], cp.get(key)) if key == 'unp_raw'
                                 else ((observations[0]['raw'] != sf.get('name_raw') or
                                        not modern.name_observation_matches(observations[0]['raw'],cp.get(key)))
                                       if modern_profile else not adapter.readable_name(observations[0]['raw']) or
                                       observations[0]['raw'] != cp.get(key)))):
                            gaps.append('counterparty_'+key+'_source_observation_missing_or_ambiguous')
            if p.get('row_status') != 'complete' or not all(bank_cell_known(op, f) for f in ('printed_debit', 'printed_credit')):
                gaps.append('row_fields_or_boundary_unresolved')
            gaps.extend(operation_identifier_gaps(op, nodes, account_links))
            if p.get('purpose_status') != 'observed': gaps.append('purpose_boundary_unresolved')
            if p.get('duplicate_status') != 'unique': gaps.append('duplicate_review_unresolved')
            original_identity_gaps = sorted(set(gaps))
            owner_type = p.get('owner_counterparty_type')
            optional_unp = owner_physical_blank_unp(op, role, source_observations.get(op['id']),
                              metadata, nodes, role_edges.get((op['id'], (role or '').upper()), []),
                              holder_ids, modern_profile, sf)
            if owner_type is not None and not valid_owner_type(op, role, source_observations.get(op['id'])):
                gaps.append('owner_type_missing_or_conflicting_provenance')
            if optional_unp:
                blank_unp_gaps = {'missing_or_unreadable_unp', 'unp_source_assertion_missing_or_conflicting',
                                 'counterparty_unp_raw_source_observation_missing_or_ambiguous',
                                 'counterparty_not_source_observed', 'counterparty_party_disagreement'}
                gaps = [g for g in gaps if g not in blank_unp_gaps]
            clarification = p.get('counterparty_clarification')
            non_identity_gaps = NON_IDENTITY_GAPS
            clarified = valid_clarification(op, role, source_observations.get(op['id']), metadata,
                                           set(gaps) - non_identity_gaps)
            if clarified:
                non_identity = NON_IDENTITY_GAPS | {
                                'original_identity_detail_missing', 'wrong_or_unknown_counterparty_role',
                                'missing_counterparty_contract', 'counterparty_role_link_missing_or_ambiguous',
                                'counterparty_party_missing_or_holder_substitution'}
                gaps = [g for g in gaps if g in non_identity]
            elif clarification is not None:
                gaps.append('owner_clarification_missing_or_conflicting_provenance')
            if ci.ROLE_PROPERTY in p and (op['id'] in official_role_conflicts or not ci.role_answers_valid(graph, op, source_observations.get(op['id']), metadata, nodes, role_edges)):
                gaps.append('owner_official_role_identity_missing_or_conflicting_provenance')
            if ci.ROLE_PROPERTY in p and ci.role_answers_valid(graph, op, source_observations.get(op['id']), metadata, nodes, role_edges):
                if any(a.get('correction', {}).get('status') == 'pending' for a in p[ci.ROLE_PROPERTY]):
                    gaps.append('owner_disputed_official_unp_unresolved')
            internal = p.get(ci.PROPERTY)
            internal_valid = ci.valid(graph, op, role, source_observations.get(op['id']), metadata,
                                      nodes, role_edges.get((op['id'], (role or '').upper()), []),
                                      holder_ids, modern_profile, sf) and op['id'] not in identity_conflicts
            if internal_valid:
                gaps = [g for g in gaps if g not in ci.RESOLVABLE]
            elif internal is not None:
                gaps.append('owner_internal_identity_missing_or_conflicting_provenance')
            text = prepare_payment_text(p['purpose_raw'], p.get('purpose_status', 'unknown'))
            if p.get('payment1c_text') != text:
                gaps.append('payment_text_missing_or_stale')
            if text['status'] != 'ready':
                gaps.append('payment_text_held')
            gaps = sorted(set(gaps))
            status = 'blocked_format' if missing_types or coverage_gaps else 'needs_clarification' if gaps else 'ready'
            questions = []
            context = 'Документ '+str(p.get('document_number_raw') or 'номер не распознан')+' от '+str(p.get('transaction_date') or 'дата не распознана')+' ('+str(role or 'роль не определена')+'), строка '+str(p['ordinal'])
            if status == 'needs_clarification':
                if any('name' in g or 'наименование' in g.lower() for g in gaps):
                    questions.append(context+': уточните полное наименование контрагента по исходному полю, включая нераспознанную часть.')
                if any('unp' in g or 'УНП' in g for g in gaps):
                    questions.append(context+': уточните УНП контрагента и разрешите противоречивые исходные значения.')
                if not questions or any('boundary' in g or 'glyph' in g or 'duplicate' in g or 'label' in g or 'anatomy' in g for g in gaps):
                    questions.append(context+': подтвердите границы/реквизиты исходной строки: '+', '.join(gaps)+'.')
            effective = ({'role': role, 'name_raw': clarification['name_raw'], 'unp_raw': clarification['unp_raw'], 'origin': 'owner_confirmed'}
                         if clarified else {'role': role or 'unknown', 'name_raw': cp.get('name_raw') if cp else None,
                                             'unp_raw': cp.get('unp_raw') if cp else None,
                                             'origin': 'owner_confirmed' if optional_unp else 'source_observed' if not gaps else 'unknown'})
            # A printed zero is evidence only, never a usable tax identifier.
            if effective['unp_raw'] in ('0', '000000000'):
                effective['unp_raw'] = None
            if ci.ROLE_PROPERTY in p and ci.role_answers_valid(graph, op, source_observations.get(op['id']), metadata, nodes, role_edges) and op['id'] not in official_role_conflicts:
                confirmed_role = next((a for a in p[ci.ROLE_PROPERTY] if a['role'] == role), None)
                if confirmed_role and 'correction' in confirmed_role:
                    effective.update({'unp_raw': confirmed_role['official_unp'], 'origin': 'owner_confirmed',
                                      'correction_kind': confirmed_role['correction']['kind'],
                                      'resolution_status': confirmed_role['correction']['status']})
            if internal_valid:
                effective.update({'origin': 'owner_confirmed', 'unp_raw': None,
                                  'internal_id': internal['internal_id'],
                                  'confirmed_name': internal['confirmed_name'],
                                  'confirmed_entity_ref': internal['confirmed_entity_ref'],
                                  'business_relation': internal['business_relation']})
            rows.append({'operation_id': op['id'], 'status': status, 'gaps': gaps, 'questions': questions,
                         'effective_counterparty': effective, 'original_identity_gaps': original_identity_gaps})
        status = 'blocked' if missing_types or coverage_gaps else 'partial' if any(r['gaps'] for r in rows) else 'admitted'
        statements.append({'statement_id': st['id'], 'status': status,
                           'field_types': {'status': 'unproven' if missing_types else 'supported',
                                           'available': available, 'required': sorted(required), 'missing': missing_types},
                           'coverage_gaps': sorted(set(coverage_gaps)), 'operation_count': len(rows),
                           'ready_count': sum(r['status'] == 'ready' for r in rows),
                           'missing_identity_count': sum(bool(r['gaps']) for r in rows),
                           'clarification_count': sum(r['status'] == 'needs_clarification' for r in rows), 'rows': rows})
    statement_ids = {s['statement_id'] for s in statements}
    orphan_operations = [n['id'] for n in nodes.values() if n['type'] == 'Operation' and n['properties']['statement_id'] not in statement_ids]
    return {'status': 'blocked' if orphan_operations or not statements or any(s['status'] == 'blocked' for s in statements)
            else 'partial' if any(s['status'] == 'partial' for s in statements) else 'admitted',
            'statements': statements, 'orphan_operations': orphan_operations, 'legacy_without_identity': 'diagnostic_only'}


def materialize_processing(graph):
    """Make every row's computed admission explicit; retain full source coverage."""
    nodes = {n['id']: n for n in graph['nodes']}
    admission = identity_admission(graph)
    for statement in admission['statements']:
        for row in statement['rows']:
            nodes[row['operation_id']]['properties']['processing'] = {k: row[k] for k in ('status', 'gaps', 'questions', 'effective_counterparty')}
    return admission


def require_processing_admission(graph):
    admission = identity_admission(graph)
    if admission['status'] == 'blocked':
        raise ValueError('entire statement blocked: required counterparty field types or original coverage unproven')
    nodes = {n['id']: n for n in graph['nodes']}
    for statement in admission['statements']:
        for row in statement['rows']:
            operation = nodes[row['operation_id']]
            properties = operation['properties']
            if properties.get('payment1c_text') != prepare_payment_text(properties['purpose_raw'], properties.get('purpose_status', 'unknown')):
                raise ValueError('payment text representation missing or stale; rebuild with current package')
            if (not all(bank_cell_known(operation, f) for f in ('printed_debit', 'printed_credit')) and
                    (properties['amount'] is not None or properties['direction'] != 'unknown')):
                raise ValueError('claimed bank movement has unresolved source cells')
            cp = properties.get('counterparty')
            non_identity = NON_IDENTITY_GAPS
            if cp and cp['status'] == 'observed' and any(g not in non_identity for g in row['original_identity_gaps']):
                raise ValueError('claimed source-observed identity is inconsistent with original evidence')
            if 'owner_clarification_missing_or_conflicting_provenance' in row['gaps']:
                raise ValueError('owner clarification provenance conflicts with original row')
            if 'owner_official_role_identity_missing_or_conflicting_provenance' in row['gaps']:
                raise ValueError('owner official role identity conflicts with original row/Party')
            if 'owner_internal_identity_missing_or_conflicting_provenance' in row['gaps']:
                raise ValueError('owner internal identity conflicts with original row or registry')
            if 'owner_type_missing_or_conflicting_provenance' in row['gaps']:
                raise ValueError('owner person type provenance conflicts with original row')
            expected = {k: row[k] for k in ('status', 'gaps', 'questions', 'effective_counterparty')}
            if properties.get('processing') != expected:
                raise ValueError('explicit per-row processing admission missing or stale')
    return admission


def clarify_counterparties(graph, answers):
    """Apply separately evidenced owner answers without changing source facts.

    answers maps an existing held Operation ID to the closed clarification shape.
    Evidence references must name actual explicit owner answer/confirmation in a
    private ledger; this offline helper cannot authenticate those external facts.
    """
    require_processing_admission(graph)
    if not validate(graph)['valid'] or not isinstance(answers, dict):
        raise ValueError('consistent current full-coverage graph and owner answer mapping required')
    result = copy.deepcopy(graph)
    operations = {n['id']: n for n in result['nodes'] if n['type'] == 'Operation'}
    schema = json.loads((PACKAGE/'references/graph.schema.json').read_text())
    from jsonschema import Draft202012Validator, FormatChecker
    validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/CounterpartyClarification'}, format_checker=FormatChecker())
    for ident, answer in answers.items():
        if ident not in operations or operations[ident]['properties']['processing']['status'] != 'needs_clarification' or not validator.is_valid(answer):
            raise ValueError('existing held row and complete confirmed owner provenance required')
        operations[ident]['properties']['counterparty_clarification'] = copy.deepcopy(answer)
    materialize_processing(result)
    if not validate(result)['valid']:
        raise ValueError('owner answer conflicts with source facts or lacks exact provenance')
    require_processing_admission(result)
    return result


def classify_counterparty_types(graph, answers):
    """Apply externally confirmed owner types, retaining every source field."""
    require_processing_admission(graph)
    if not validate(graph)['valid'] or not isinstance(answers, dict):
        raise ValueError('consistent full-coverage graph and owner type mapping required')
    result = copy.deepcopy(graph)
    operations = {n['id']: n for n in result['nodes'] if n['type'] == 'Operation'}
    schema = json.loads((PACKAGE/'references/graph.schema.json').read_text())
    from jsonschema import Draft202012Validator, FormatChecker
    validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/OwnerCounterpartyType'}, format_checker=FormatChecker())
    for ident, answer in answers.items():
        if ident not in operations or not validator.is_valid(answer):
            raise ValueError('existing row and complete confirmed owner type provenance required')
        operations[ident]['properties']['owner_counterparty_type'] = copy.deepcopy(answer)
    materialize_processing(result)
    if not validate(result)['valid']:
        raise ValueError('owner type conflicts with the exact original row')
    require_processing_admission(result)
    return result


def validate(graph):
    errors = _schema_errors(graph)
    if errors:
        return {'valid':False, 'errors':errors, 'statements':[]}
    nodes = {n['id']:n for n in graph['nodes']}
    import modern_adapter as modern
    modern_support={st['id']:modern.format_supported(st,[o for o in graph['nodes'] if o['type']=='Operation' and o['properties']['statement_id']==st['id']]) for st in graph['nodes'] if st['type']=='Statement' and any(o['field']=='modern_anatomy_profile' for o in st['observations'])}
    if len(nodes) != len(graph['nodes']):
        errors.append('duplicate node IDs')
    if len({e['id'] for e in graph['edges']}) != len(graph['edges']):
        errors.append('duplicate edge IDs')
    def target(ident, typ=None):
        if ident not in nodes or typ and nodes[ident]['type'] != typ:
            errors.append('missing or mistyped reference: ' + ident)
            return None
        return nodes[ident]
    for n in graph['nodes']:
        p = n['properties']
        if n['type'] in ('Account','Bank') and 'identifier' in p:
            import bank_identifiers as bi
            ident=p['identifier'];kind='account' if n['type']=='Account' else 'bank'
            if ident != node_identifier(n, kind):
                errors.append('bank identifier recognition mismatch: '+n['id'])
            if kind=='account' and p['format'] != ('iban' if ident['format']=='iban' else 'historical' if ident['format']=='historical_numeric' else 'unknown'):
                errors.append('bank account format mismatch: '+n['id'])
            value=p['number_raw'] if kind=='account' else p['code_raw']
            retained_unknown_bank_raw = (kind == 'bank' and not bi.usable(ident)
                and p['identity_status'] == 'unknown' and value == ident['raw']
                and any(o['field'] == 'code_raw' and o['raw'] == value
                        and o['normalized'] is None and o['status'] == 'unknown'
                        for o in n['observations']))
            if value is not None and not retained_unknown_bank_raw and (ident['format_status'] != 'recognized' or value != ident['normalized']):
                errors.append('bank identifier value mismatch: '+n['id'])
            if not bi.usable(ident) and p['identity_status'] == 'observed':
                errors.append('unusable bank identifier claimed observed: '+n['id'])
        if n['type']=='Statement':
            if p.get('archive_only'):
                archive_ops=[o for o in graph['nodes'] if o['type']=='Operation' and o['properties']['statement_id']==n['id']]
                if archive_ops or p.get('expected_operations')!=0 or p['printed_debit_total'] is not None or p['printed_credit_total'] is not None:
                    errors.append('balance-only archive has operations or invented turnovers: '+n['id'])
            account=target(p['account_id'],'Account'); target(p['currency_context_id'],'CurrencyContext')
            if account and p['account_number_raw']!=account['properties']['number_raw']:
                errors.append('statement/account number disagreement: '+n['id'])
            if p['opening_origin']=='first_statement_blank_zero':
                blank=any(o['field']=='opening' and isinstance(o['raw'],str) and not o['raw'].strip() for o in n['observations'])
                if not blank or not p['first_account_evidence'] or p['opening'] is None or decimal(p['opening'])!=0:
                    errors.append('unproven first-statement blank opening rule: '+n['id'])
            if p['period_start'] and p['period_end'] and p['period_start'] > p['period_end']:
                errors.append('reversed statement period: '+n['id'])
            if p['selection']=='exclude_reissue' and not p['selection_evidence']:
                errors.append('reissue exclusion requires evidence: '+n['id'])
        if n['type']=='CurrencyContext' and p['semantics_status']=='verified' and (not p['epoch'] or not p['evidence_refs'] or not p['printed_currency'] or not p['unit_raw']):
            errors.append('verified currency context requires epoch/evidence: '+n['id'])
        if n['type']=='Bank' and p['code_system_raw'] == 'Код':
            for observation in n['observations']:
                if observation['field'] == 'code_raw' and observation['status'] == 'observed':
                    import archive_adapter as adapter
                    raw = observation['raw']
                    code = adapter.compact(raw) if isinstance(raw, str) else ''
                    import bank_identifiers as bi
                    recognized=bi.recognize(raw,'bank')
                    if not bi.usable(recognized) or observation['normalized'] != recognized['normalized'] or p['code_raw'] != recognized['normalized']:
                        errors.append('unproven observed bank code: '+n['id'])
        if n['type']=='Operation':
            st = target(p['statement_id'],'Statement')
            if 'payment1c_text' in p and p['payment1c_text'] != prepare_payment_text(p['purpose_raw'], p.get('purpose_status', 'unknown')):
                errors.append('payment text differs from deterministic full source transformation: '+n['id'])
            if 'payment1c_comment_raw' in p and p['payment1c_comment_raw'] != p['purpose_raw']:
                errors.append('payment comment differs from full source purpose: '+n['id'])
            modern_diagnostic=any(o['field']=='diagnostic_anatomy_profile' and o['normalized']=='belarusbank-landscape-diagnostic-v1' for o in n.get('observations',[]))
            if modern_diagnostic:
                diagnostic_purpose=[o for o in n['observations'] if o['field']=='diagnostic_purpose_raw']
                details=[o for o in n['observations'] if o['field']=='identity_detail_raw']
                if (p.get('purpose_status')!='unknown' or p['row_status']!='partial' or p['classification'] is not None or p['classification_status']!='unknown' or p.get('settlement') is not None or p.get('counterparty',{}).get('status')!='unknown' or
                    len(diagnostic_purpose)!=1 or diagnostic_purpose[0]['raw']!=p['purpose_raw'] or len(details)!=1 or
                    not isinstance(details[0]['raw'],str) or
                    not re.sub(r'\s','',details[0]['raw']).endswith(re.sub(r'\s','',p['purpose_raw'])) or
                    any(e['type']=='HAS_COMPONENT' and e['from']==n['id'] for e in graph['edges']) or
                    not st or not any(o['field']=='diagnostic_anatomy_profile' and o['normalized']=='belarusbank-landscape-diagnostic-v1' for o in st['observations'])):
                    errors.append('modern diagnostic scope/purpose mismatch: '+n['id'])
            if 'purpose_status' in p and not modern_diagnostic and 'modern_fields' not in p:
                import archive_adapter as adapter
                details = [o for o in n['observations'] if o['field'] == 'identity_detail_raw']
                if len(details) == 1 and isinstance(details[0]['raw'], str):
                    lines = details[0]['raw'].split('\n')
                    _, _, purpose, _ = adapter.metadata_fields(lines, [{} for _ in lines])
                    if p['purpose_raw'] != '\n'.join(purpose):
                        errors.append('purpose differs from retained source detail: '+n['id'])
                else:
                    errors.append('unique retained source detail required for purpose: '+n['id'])
            if 'modern_fields' in p:
                import modern_adapter as modern
                sf=modern.source_fields(n);mf=p['modern_fields']
                if not st or not modern_support.get(st['id'],False):errors.append('modern source profile proof missing: '+n['id'])
                side=[o for o in n['observations'] if o['field']=='modern_sidecar_raw']
                if (len(side)!=1 or mf['sidecar_raw']!=side[0]['raw'] or
                    any(mf[k]!=sf.get(k) for k in ('name_raw','bank_name_raw','unp_raw','unlabelled_codes_raw')) or
                    p['purpose_raw']!=sf.get('purpose_raw') or p['priority_raw']!=sf.get('priority_raw')):
                    errors.append('modern fields differ from retained source: '+n['id'])
                for key in ('transaction_date','document_number_raw','operation_code_raw'):
                    os=[o for o in n['observations'] if o['field']==key]
                    import archive_adapter as adapter
                    expected_date=modern.date_value(os[0]['raw']) if len(os)==1 and key=='transaction_date' else None
                    if len(os)!=1 or os[0]['normalized']!=p[key] or (p[key]!=expected_date if key=='transaction_date' else os[0]['raw']!=p[key]):
                        errors.append('modern bank row field source mismatch: '+n['id'])
                account_edges=[e for e in graph['edges'] if e['from']==n['id'] and e['type'] in ('FROM_ACCOUNT','TO_ACCOUNT')]
                if len(account_edges)==1:
                    account=nodes.get(account_edges[0]['to'],{})
                    bank_edges=[e for e in graph['edges'] if e['from']==account.get('id') and e['type']=='SERVICED_BY']
                    bank=nodes.get(bank_edges[0]['to'],{}) if len(bank_edges)==1 else {}
                    for field,node in [('modern_account_raw',account),('modern_bank_code_raw',bank)]:
                        os=[o for o in n['observations'] if o['field']==field]
                        if len(os)!=1 or node.get('properties',{}).get('identifier',{}).get('raw')!=os[0]['raw']:
                            errors.append('modern linked identifier differs from source field: '+n['id'])
                    if bank.get('properties',{}).get('name_raw')!=sf.get('bank_name_raw'):
                        errors.append('modern bank name differs from source field: '+n['id'])
                elif p['direction']!='unknown':errors.append('modern bank account link missing or ambiguous: '+n['id'])
                md=sf.get('metadata',{})
                if p['document_date']!=md.get('document_date') or mf['urgency_raw']!=md.get('urgency_raw') or mf['acceptance_raw']!=md.get('acceptance_raw'):
                    errors.append('modern date urgency acceptance source mismatch: '+n['id'])
            d = decimal(p['printed_debit'] or '0')
            cr = decimal(p['printed_credit'] or '0')
            cells_known = all(bank_cell_known(n, f) for f in ('printed_debit', 'printed_credit'))
            bank_known = cells_known and ((d > 0 and cr == 0) or (cr > 0 and d == 0))
            semantics = p.get('bank_debit_is', st['properties']['debit_is'] if st else 'unknown')
            if bank_known and semantics != 'unknown' or p['amount'] is not None or p['direction'] != 'unknown':
                expected = ('outflow' if d > 0 else 'inflow')
                if semantics == 'inflow':
                    expected = 'inflow' if d > 0 else 'outflow'
                bank_amount = d if d > 0 else cr
                if (not bank_known or semantics == 'unknown' or p['amount'] is None or
                        decimal(p['amount']) != bank_amount or p['direction'] != expected):
                    errors.append('movement differs from same-operation bank cells: '+n['id'])
            elif p['row_status'] == 'complete':
                errors.append('complete operation has unknown bank movement: '+n['id'])
            currency_node = nodes.get(st['properties']['currency_context_id']) if st else None
            currency = currency_node['properties']['printed_currency'] if currency_node else None
            settlement = p.get('settlement')
            if settlement:
                # Reproduce the supported fields from the full source purpose
                # and independently validated bank movement. Reviewed JSON
                # cannot substitute a description amount or assert a fee.
                import archive_adapter as adapter
                standalone = p.get('purpose_status') == 'observed' and adapter.cash_acceptance_fee(p['purpose_raw'], p['direction'])
                components = (adapter.fee_basis(p['purpose_raw']) if standalone else
                              adapter.amount_components(p['purpose_raw'], p['direction'], p['amount'], currency))
                if p.get('purpose_status') == 'unknown':
                    components = [(*c[:4], 'unknown') for c in components]
                expected_settlement = adapter.settlement_view(components, p['direction'], p['amount'], standalone)
                if settlement != expected_settlement:
                    errors.append('unproven source settlement fields: '+n['id'])
            if 'purpose_status' in p and not modern_diagnostic:
                import archive_adapter as adapter
                standalone = p['purpose_status'] == 'observed' and adapter.cash_acceptance_fee(p['purpose_raw'], p['direction'])
                if (p['classification'] != ('bank_fee' if standalone else None) or
                        p['classification_status'] != ('inferred' if standalone else 'unknown')):
                    errors.append('classification differs from source purpose: '+n['id'])
                expected_components = (adapter.fee_basis(p['purpose_raw']) if standalone else
                                       adapter.amount_components(p['purpose_raw'], p['direction'], p['amount'], currency))
                if p['purpose_status'] == 'unknown':
                    expected_components = [(*c[:4], 'unknown') for c in expected_components]
                expected_props = []
                for offset, kind, raw, mention, inclusion in expected_components:
                    unit, currency = adapter.component_unit(p['purpose_raw'], offset, mention)
                    expected_props.append({'kind':kind, 'value_raw':raw, 'value':adapter.component_money(raw),
                                           'unit_raw':unit, 'currency_raw':currency, 'inclusion':inclusion})
                component_links = [e for e in graph['edges'] if e['type']=='HAS_COMPONENT' and e['from']==n['id']]
                actual = [nodes.get(e['to'], {}).get('properties') for e in component_links]
                # Compare whole source assertions, not a selected settlement only.
                import collections
                key = lambda obj: json.dumps(obj, sort_keys=True, ensure_ascii=False)
                if collections.Counter(map(key, actual)) != collections.Counter(map(key, expected_props)):
                    errors.append('amount components differ from full source purpose: '+n['id'])
            if p.get('purpose_status') == 'unknown' and p['classification'] == 'bank_fee':
                errors.append('fee classification has unresolved purpose boundary: '+n['id'])
            if 'one_c_rko' in p and p['one_c_rko']['observed_at'] is not None:
                try:
                    timestamp = datetime.fromisoformat(p['one_c_rko']['observed_at'].replace('Z', '+00:00'))
                    if timestamp.tzinfo is None or timestamp.date().isoformat() != p['one_c_rko']['observed_on']:raise ValueError('matching observation date/timezone required')
                except ValueError:
                    errors.append('RKO observation requires exact timezone timestamp: '+n['id'])
            if st and p['transaction_date'] and st['properties']['period_start'] and st['properties']['period_end']:
                if not st['properties']['period_start'] <= p['transaction_date'] <= st['properties']['period_end']:
                    errors.append('operation date outside period: '+n['id'])
            for key in ('amount','printed_debit','printed_credit'):
                if p[key] is not None and decimal(p[key]) < 0:
                    errors.append('negative operation amount: '+n['id'])
        if n['type']=='DuplicateDecision':
            for member in p['member_ids']: target(member)
            if p['status']=='confirmed' and (not p['canonical_id'] or not p['evidence_refs']):
                errors.append('confirmed duplicate needs canonical/evidence: '+n['id'])
    links={(e['type'],e['from'],e['to']) for e in graph['edges']}
    for edge in graph['edges']:
        left,right=target(edge['from']),target(edge['to'])
        a,b=RELATIONS[edge['type']]
        if left and right and (a and left['type'] not in a or right['type'] not in b):
            errors.append('edge endpoint type mismatch: '+edge['id'])
    for n in graph['nodes']:
        p=n['properties']
        required=[]
        if n['type']=='Statement': required=[('FOR_ACCOUNT',n['id'],p['account_id']),('IN_CURRENCY',n['id'],p['currency_context_id'])]
        if n['type']=='Operation':required=[('CONTAINS',p['statement_id'],n['id'])]
        for link in required:
            if link not in links: errors.append('required edge missing: '+n['id'])
    # Money validation checks representation only; yearly reconciliation is future work.
    for n in graph['nodes']:
        p=n['properties']
        if n['type']=='Statement' and p['expected_operations'] is not None:
            ops=[o for o in graph['nodes'] if o['type']=='Operation' and o['properties']['statement_id']==n['id']]
            if p['rows_complete'] and p['expected_operations']!=len(ops):errors.append('reviewed operation count mismatch: '+n['id'])
            ordinals=[o['properties']['ordinal'] for o in ops]
            if sorted(ordinals)!=list(range(1,len(ops)+1)):errors.append('operation ordinal sequence: '+n['id'])
        if n['type']=='Statement':
            # Source-declared bank-column semantics are preserved, not guessed from dates.
            if p['year'] and p['period_start'] and int(p['period_start'][:4])!=p['year']:
                errors.append('statement year/period mismatch: '+n['id'])
    admission = identity_admission(graph)
    for state in admission['statements']:
        for row in state['rows']:
            # Missing legacy contracts remain readable as private diagnostics.
            # A current claim of observed identity must actually be supported.
            cp = nodes[row['operation_id']]['properties'].get('counterparty')
            if cp and cp['status'] == 'observed' and any(g not in NON_IDENTITY_GAPS for g in row['original_identity_gaps']):
                errors.append('unproven counterparty identity: '+row['operation_id'])
            if 'owner_clarification_missing_or_conflicting_provenance' in row['gaps']:
                errors.append('invalid owner clarification: '+row['operation_id'])
            if 'owner_official_role_identity_missing_or_conflicting_provenance' in row['gaps']:
                errors.append('invalid owner official role identity: '+row['operation_id'])
            if 'owner_internal_identity_missing_or_conflicting_provenance' in row['gaps']:
                errors.append('invalid owner internal identity: '+row['operation_id'])
            if 'owner_type_missing_or_conflicting_provenance' in row['gaps']:
                errors.append('invalid owner person type: '+row['operation_id'])
            processing = nodes[row['operation_id']]['properties'].get('processing')
            if processing is None and state['status'] != 'blocked':
                errors.append('missing explicit per-row processing admission: '+row['operation_id'])
            if processing is not None and processing != {k: row[k] for k in ('status', 'gaps', 'questions', 'effective_counterparty')}:
                errors.append('stale or false row processing admission: '+row['operation_id'])
    return {'valid':not errors,'processing_admission':admission,'errors':errors,'node_count':len(graph['nodes']),'edge_count':len(graph['edges']),
            'operation_count':sum(n['type']=='Operation' for n in graph['nodes']),
            'partial_statements':sum(n['type']=='Statement' and not n['properties']['rows_complete'] for n in graph['nodes']),
            'amount_reconciliation':'not_run_future_context'}


def source_unit_review_valid(unit):
    """A reviewed no-conversion unit tuple; source claims still need review."""
    if not isinstance(unit, dict):
        return False
    supported = {('BYR_2000_historical_ruble', 'pre_2016_07_01'),
                 ('BYN_2009_ruble', 'from_2016_07_01'),
                 ('synthetic-unit', 'synthetic-epoch')}
    return (unit.get('status') == 'verified' and unit.get('transfer_factor') == '1'
            and (unit.get('source_unit'), unit.get('source_epoch')) in supported
            and type(unit.get('printed_label_anomaly')) is bool
            and isinstance(unit.get('basis'), str) and bool(unit['basis'].strip()))


def check_conversion(graph, controls):
    """Compare parsed banking facts to separately reviewed statement controls."""
    structural=validate(graph)
    if not structural['valid']:return {'status':'failed','accepted':False,'math_status':'unknown','errors':structural['errors'],'statements':[]}
    from jsonschema import Draft202012Validator
    schema=json.loads((PACKAGE/'references/controls.schema.json').read_text())
    if list(Draft202012Validator(schema).iter_errors(controls)):
        return {'status':'failed','accepted':False,'math_status':'unknown','errors':['invalid statement control contract'],'statements':[]}
    control_ids=[c['statement_id'] for c in controls['statements']]
    if len(set(control_ids))!=len(control_ids):return {'status':'failed','accepted':False,'math_status':'unknown','errors':['duplicate controls'],'statements':[]}
    control_map={c['statement_id']:c for c in controls['statements']}
    nodes={n['id']:n for n in graph['nodes']}
    statements=[n for n in graph['nodes'] if n['type']=='Statement']
    if set(control_ids)-{s['id'] for s in statements}:return {'status':'failed','accepted':False,'math_status':'unknown','errors':['controls reference unknown statement'],'statements':[]}
    results=[]
    with localcontext() as context:
        context.prec=max(100,sum(len(str(n['properties'])) for n in graph['nodes'])*2)
        for st in statements:
            p=st['properties'];c=control_map.get(st['id']);issues=[];differences={};notes=[]
            if p.get('archive_only'):issues.append('balance_only_archive_no_processing_acceptance')
            operations=[n for n in graph['nodes'] if n['type']=='Operation' and n['properties']['statement_id']==st['id']]
            identity = next(x for x in structural['processing_admission']['statements'] if x['statement_id'] == st['id'])
            if identity['status'] == 'blocked':issues.append('required_counterparty_field_types_or_coverage_unproven')
            elif identity['status'] == 'partial':issues.append('row_clarification_required')
            if p['selection']=='exclude_reissue':
                results.append({'statement_id':st['id'],'status':'needs_review' if issues else 'excluded_reviewed_copy','issues':issues,'differences':{},'math_result':{'status':'not_applicable','issues':[]}});continue
            if c is None:
                results.append({'statement_id':st['id'],'status':'needs_review','issues':['missing_printed_controls'],'differences':{},'math_result':{'status':'unknown','issues':['missing_printed_controls']}});continue
            c=dict(c)
            math_issues=[]
            if not source_unit_review_valid(c.get('source_unit_review')):
                math_issues.append('independent_source_unit_unverified')
            source=c.get('source_review',{})
            if source.get('origin')!='independent_original_pdf' or source.get('review_status')!='reviewed' or not source.get('reference'):
                math_issues.append('independent_pdf_controls_unverified')
            count=c.get('printed_operation_count',{})
            count_status=count.get('status','unknown')
            if count_status=='observed':
                if not isinstance(count.get('value'),int) or isinstance(count.get('value'),bool) or not count.get('raw'):
                    math_issues.append('printed_operation_count_unknown')
                else:differences['printed_operation_count_rows']=str(len(operations)-count['value'])
            elif count_status=='absent' and count.get('value') is None and count.get('raw') is None:
                notes.append('printed_operation_count_not_applicable')
            else:math_issues.append('printed_operation_count_unknown')
            raw_blank=isinstance(c['opening_raw'],str) and not c['opening_raw'].strip()
            if c['opening'] is None and raw_blank and c['first_for_account'] is True and c['first_account_basis']:
                c['opening']='0'
            elif raw_blank and c['first_for_account'] is not True:
                issues.append('blank_opening_not_proven_first_statement')
            if not p['rows_complete'] or any(n['properties']['row_status']!='complete' for n in operations):issues.append('incomplete_row_assembly')
            if any(n['properties']['duplicate_status']!='unique' for n in operations):issues.append('duplicate_review_incomplete')
            if p['selection']!='include':issues.append('statement_copy_selection')
            if p['debit_is']=='unknown' or p['opening_sign_rule']=='unknown':issues.append('bank_column_semantics_unknown')
            if nodes[p['currency_context_id']]['properties']['semantics_status']!='verified':issues.append('money_unit_unknown')
            # Direct graph-vs-printed controls: independent from sums of parsed rows.
            for key in ('opening','closing','printed_debit_total','printed_credit_total'):
                if c[key] is None:
                    math_issues.append(key+'_control_unknown');continue
                if p[key] is None:math_issues.append(key+'_graph_unknown');continue
                differences[key+'_copy']=decimal_text(decimal(p[key])-decimal(c[key]))
            debit=Decimal(0);credit=Decimal(0);incoming=Decimal(0);outgoing=Decimal(0);money_known=True
            for n in operations:
                o=n['properties'];d=decimal(o['printed_debit']) if o['printed_debit'] is not None else Decimal(0)
                cr=decimal(o['printed_credit']) if o['printed_credit'] is not None else Decimal(0)
                if not all(bank_cell_known(n, f) for f in ('printed_debit','printed_credit')):
                    math_issues.append('bank_cell_observation_unknown');money_known=False
                if d<0 or cr<0 or d>0 and cr>0 or d==0 and cr==0:
                    issues.append('ambiguous_bank_cells');money_known=False;continue
                debit+=d;credit+=cr
                if o['amount'] is None or o['direction']=='unknown':issues.append('normalized_money_unknown');money_known=False;continue
                if decimal(o['amount'])!=d+cr:differences['row_amount:'+str(o['ordinal'])]=decimal_text(decimal(o['amount'])-d-cr)
                if p['debit_is']!='unknown':
                    expected='outflow' if d>0 else 'inflow'
                    if p['debit_is']=='inflow':expected='inflow' if d>0 else 'outflow'
                    if o['direction']!=expected:issues.append('row_direction_mismatch')
                if o['direction']=='inflow':incoming+=decimal(o['amount'])
                else:outgoing+=decimal(o['amount'])
            expense_key='printed_debit_total' if p['debit_is']=='outflow' else 'printed_credit_total'
            income_key='printed_credit_total' if p['debit_is']=='outflow' else 'printed_debit_total'
            if money_known:
                for key,total in [('printed_debit_total',debit),('printed_credit_total',credit)]:
                    if c[key] is not None:differences[key+'_rows']=decimal_text(total-decimal(c[key]))
                if p['debit_is']!='unknown':
                    if c[expense_key] is not None:differences['expense_rows']=decimal_text(outgoing-decimal(c[expense_key]))
                    if c[income_key] is not None:differences['income_rows']=decimal_text(incoming-decimal(c[income_key]))
                if c['opening'] is not None and c['closing'] is not None and p['debit_is']!='unknown' and p['opening_sign_rule']=='signed':
                    differences['balance_equation']=decimal_text(decimal(c['opening'])+incoming-outgoing-decimal(c['closing']))
            if not money_known:math_issues.append('normalized_money_unknown')
            if c['opening'] is None or c['closing'] is None or p['debit_is']=='unknown' or p['opening_sign_rule']!='signed':math_issues.append('balance_equation_unknown')
            if c['no_activity'] and operations:issues.append('no_activity_control_has_operations')
            issues+=math_issues
            checks={}
            def comparison(key, graph_value, pdf_value, difference, absent=False):
                checks[key]={'graph':graph_value,'pdf':pdf_value,'difference':difference,
                             'status':'not_applicable' if absent else 'unknown' if difference is None else
                                      'pass' if decimal(difference)==0 else 'failed'}
            for key in ('opening','closing','printed_debit_total','printed_credit_total'):
                comparison(key,p[key],c[key],differences.get(key+'_copy'))
            comparison('operation_count',len(operations),count.get('value'),differences.get('printed_operation_count_rows'),count_status=='absent')
            comparison('expense',decimal_text(outgoing) if money_known else None,c['printed_debit_total'] if p['debit_is']=='outflow' else c['printed_credit_total'],differences.get('expense_rows'))
            comparison('income',decimal_text(incoming) if money_known else None,c['printed_credit_total'] if p['debit_is']=='outflow' else c['printed_debit_total'],differences.get('income_rows'))
            comparison('bank_debit_cells',decimal_text(debit) if money_known else None,c['printed_debit_total'],differences.get('printed_debit_total_rows'))
            comparison('bank_credit_cells',decimal_text(credit) if money_known else None,c['printed_credit_total'],differences.get('printed_credit_total_rows'))
            equation_semantics_known = p['opening_sign_rule']=='signed' and p['debit_is']!='unknown'
            calculated_closing=decimal_text(decimal(p['opening'])-outgoing+incoming) if equation_semantics_known and p['opening'] is not None and money_known else None
            comparison('graph_balance_equation',calculated_closing,c['closing'],decimal_text(decimal(calculated_closing)-decimal(c['closing'])) if calculated_closing is not None and c['closing'] is not None else None)
            if checks['graph_balance_equation']['difference'] is not None:differences['graph_balance_equation']=checks['graph_balance_equation']['difference']
            comparison('pdf_balance_equation',decimal_text(decimal(c['opening'])-decimal(c[expense_key])+decimal(c[income_key])) if all(c[k] is not None for k in ('opening','printed_debit_total','printed_credit_total')) and equation_semantics_known else None,c['closing'],None)
            if checks['pdf_balance_equation']['graph'] is not None and c['closing'] is not None:
                diff=decimal_text(decimal(checks['pdf_balance_equation']['graph'])-decimal(c['closing']))
                checks['pdf_balance_equation'].update(difference=diff,status='pass' if decimal(diff)==0 else 'failed')
                differences['pdf_balance_equation']=diff
            failed=any(decimal(v)!=0 for v in differences.values()) or 'row_direction_mismatch' in issues or 'no_activity_control_has_operations' in issues
            results.append({'statement_id':st['id'],'status':'failed' if failed else 'needs_review' if issues else 'pass','issues':sorted(set(issues)),
                            'differences':differences,'checks':checks,'computed_debit':decimal_text(debit),'computed_credit':decimal_text(credit),'computed_expense':decimal_text(outgoing) if money_known else None,'computed_income':decimal_text(incoming) if money_known else None,
                            'graph_operation_count':len(operations),'printed_operation_count_status':count_status,
                            'math_result':{'status':'failed' if failed else 'unknown' if math_issues else 'pass','issues':sorted(set(math_issues)), 'all_graph_rows_included':True},'notes':notes})
    continuity=check_continuity(graph)
    overall='failed' if any(r['status']=='failed' for r in results) or continuity['status']=='failed' else 'needs_review' if not results or any(r['status']=='needs_review' for r in results) or continuity['status']=='needs_review' else 'pass'
    if structural['processing_admission']['status'] == 'blocked':overall='failed'
    elif structural['processing_admission']['status'] == 'partial' and overall == 'pass':overall='needs_review'
    math_status='failed' if any(r.get('math_result',{}).get('status')=='failed' for r in results) else 'unknown' if not results or any(r.get('math_result',{}).get('status')=='unknown' for r in results) else 'pass'
    return {'status':overall,'math_status':math_status,'accepted':overall=='pass' and math_status=='pass','processing_admission':structural['processing_admission'],'errors':[],'statements':results,'continuity':continuity,'scope':'graph_import_integrity_only'}


def check_continuity(graph):
    """Same business account, adjacent periods and proven unit; no conversion."""
    nodes={n['id']:n for n in graph['nodes']}
    groups={};results=[]
    for n in graph['nodes']:
        if n['type']=='Statement' and n['properties']['selection']!='exclude_reissue':groups.setdefault(n['properties']['account_id'],[]).append(n)
    with localcontext() as context:
        context.prec=max(100,sum(len(str(n['properties'])) for n in graph['nodes'])*2)
        for account,statements in sorted(groups.items()):
            statements.sort(key=lambda n:n['properties']['period_start'] or '')
            for previous,current in zip(statements,statements[1:]):
                a,b=previous['properties'],current['properties'];issues=[];difference=None
                if a['selection']!='include' or b['selection']!='include':issues.append('copy_selection_unknown')
                if not a['period_end'] or not b['period_start']:issues.append('period_unknown')
                else:
                    expected=date.fromisoformat(a['period_end'])+timedelta(days=1);actual=date.fromisoformat(b['period_start'])
                    if actual>expected:issues.append('period_gap')
                    if actual<expected:issues.append('period_overlap_or_reissue')
                ac=nodes[a['currency_context_id']]['properties'];bc=nodes[b['currency_context_id']]['properties']
                if ac['semantics_status']!='verified' or bc['semantics_status']!='verified':issues.append('money_unit_unknown')
                elif any(ac[k]!=bc[k] for k in ['printed_currency','unit_raw','epoch']):issues.append('currency_unit_or_denomination_boundary')
                if a['closing'] is None or b['opening'] is None:issues.append('boundary_balance_unknown')
                if not issues:difference=decimal_text(decimal(b['opening'])-decimal(a['closing']))
                results.append({'account_id':account,'previous_statement_id':previous['id'],'next_statement_id':current['id'],'status':'needs_review' if issues else 'failed' if decimal(difference)!=0 else 'pass','issues':issues,'opening_minus_previous_closing':difference})
    # Equal-looking account strings do not silently establish a shared banking identity.
    numbers={}
    for account in groups:
        number=nodes[account]['properties']['number_raw']
        if number is not None:numbers.setdefault(number,[]).append(account)
    ambiguities=[ids for ids in numbers.values() if len(ids)>1]
    status='failed' if any(r['status']=='failed' for r in results) else 'needs_review' if ambiguities or any(r['status']=='needs_review' for r in results) else 'pass'
    return {'status':status,'pairs':results,'account_identity_ambiguities':ambiguities,'yearly_1c_reconciliation':'not_run_out_of_scope'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['extract','build','combine','validate','check','accept','annotate-rko','clarify-counterparties'])
    parser.add_argument('input',type=Path);parser.add_argument('--output',type=Path);parser.add_argument('--controls',type=Path);parser.add_argument('--engine',choices=['auto','poppler','pypdf','pdfplumber'],default='auto')
    parser.add_argument('--annotations',type=Path);parser.add_argument('--private-evidence',type=Path)
    parser.add_argument('--acceptance-output',type=Path)
    args=parser.parse_args()
    try:
        output_written=False
        if args.action=='extract':result=extract(args.input,args.engine)
        else:
            document=json.loads(args.input.read_text(encoding='utf-8'))
            if args.action=='build':
                result=build(document)
                checks=validate(result)
                if not checks['valid']:raise ValueError('reviewed input produces invalid graph: '+'; '.join(checks['errors']))
                if args.controls is not None:
                    if args.acceptance_output is None:raise ValueError('--acceptance-output required with build --controls')
                    acceptance=check_conversion(result,json.loads(args.controls.read_text(encoding='utf-8')))
                    if args.output:
                        write_new_pair(args.acceptance_output,acceptance,args.output,result)
                        output_written=True
                    else:write_new(args.acceptance_output,acceptance)
                    if acceptance['status']!='pass':
                        print(json.dumps({'action':'build','status':acceptance['status'],'accepted':False,'math_status':acceptance.get('math_status'),'output_saved':bool(args.output)}))
                        return 1
            elif args.action=='combine':
                result=combine(document)
                checks=validate(result)
                if not checks['valid']:raise ValueError('combined graph is invalid')
            elif args.action=='validate':result=validate(document)
            elif args.action=='annotate-rko':
                if args.annotations is None:raise ValueError('--annotations is required')
                evidence=json.loads(args.private_evidence.read_text(encoding='utf-8')) if args.private_evidence else None
                result=annotate_rko(document,json.loads(args.annotations.read_text(encoding='utf-8')),evidence)
            elif args.action=='clarify-counterparties':
                if args.annotations is None:raise ValueError('--annotations is required for confirmed owner answers')
                result=clarify_counterparties(document,json.loads(args.annotations.read_text(encoding='utf-8')))
            elif args.action in ('check','accept'):
                if args.controls is None:raise ValueError('--controls is required')
                result=check_conversion(document,json.loads(args.controls.read_text(encoding='utf-8')))
        if args.output and not output_written:write_new(args.output,result)
        summary={'action':args.action,'output_saved':bool(args.output),'status':result.get('status'),'valid':result.get('valid'),'nodes':len(result.get('nodes',[])),'accepted':result.get('accepted',False), 'math_status':result.get('math_status','unverified'), 'pages':len(result.get('pages',[])),'errors':len(result.get('errors',[])), 'processing_admission':result.get('processing_admission',{}).get('status')}
        print(json.dumps(summary))
        return 1 if result.get('processing_admission',{}).get('status') == 'blocked' or result.get('valid') is False or result.get('status') in {'invalid','failed','needs_review'} else 0
    except (ValueError,KeyError,TypeError,InvalidOperation,OSError,subprocess.CalledProcessError,ImportError) as exc:
        print(json.dumps({'action':args.action,'status':'error','error_type':type(exc).__name__}))
        return 2

if __name__=='__main__':raise SystemExit(main())
