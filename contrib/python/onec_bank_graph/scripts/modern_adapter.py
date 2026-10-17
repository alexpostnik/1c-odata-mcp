"""Source-specific proportional table diagnostics and a separate reviewed profile.

The default diagnostic path remains processing blocked. Reviewed admission needs
physical source fields plus separately verified owner format-role evidence; no
legacy labels or individual identity values are manufactured.
"""
import re
from pathlib import Path
import bank_identifiers as bi

CELLS = {'document_number_raw': (65.76,127.46), 'operation_code_raw': (127.94,184.10),
         'bank_code': (184.58,247.97), 'account': (248.45,396.79),
         'printed_debit': (397.27,510.19), 'printed_credit': (510.67,623.62),
         'transaction_date': (624.10,687.48)}
PROFILE = 'belarusbank-landscape-diagnostic-v1'

def slice_x(line, lo, hi):
    return ''.join(g['text'] for g in line['glyphs'] if lo <= g['x0'] < hi).strip()

def prepare(pdf):
    import pdfplumber
    pages=[]
    with pdfplumber.open(pdf) as reader:
        for page in reader.pages:
            groups=[]
            for g in sorted(page.chars,key=lambda x:(x['top'],x['x0'])):
                if not groups or abs(groups[-1][0]-g['top'])>1:
                    groups.append((g['top'],[]))
                groups[-1][1].append(g)
            lines=[]
            for top,gs in groups:
                gs=sorted(gs,key=lambda x:x['x0'])
                raw=''.join(g['text'] for g in gs).rstrip()
                lines.append({'raw_text':raw,'text':raw,'top':top,'glyphs':[
                    {k:g[k] for k in ('text','x0','x1','top')} for g in gs]})
            pages.append({'page':page.page_number,'lines':lines,'width':page.width,'height':page.height})
    return {'layout_profile':PROFILE,'pages':pages}

def header_contract(doc):
    # Source-specific headings must occupy the observed labelled cells. A name,
    # bank or UNP in a neighbouring cell never establishes movement-role anatomy.
    first=doc['pages'][0]['lines']
    def has(text,bounds):
        return any(text in slice_x(line,*bounds) for line in first)
    fields={'document':has('Дата',(65.28,127.94)),
            'operation':has('Опер',(127.94,184.58)),
            'bank':has('Код',(184.58,248.45)),
            'account':has('Счет',(248.45,397.27)),
            'debit':has('Дебет',(397.27,510.67)),
            'credit':has('Кредит',(510.67,624.10)),
            'date':has('Дата',(624.10,687.96))}
    return {'recognized':all(fields.values()),'fields':fields,'physical_cells':CELLS,
            'profile':PROFILE,'required_identity_role_fields':'unproven'}

def boundary(fields):
    # Identifier readiness is deliberately absent: wrapped/damaged identifiers
    # and operation codes must keep a separate row, never leak into prior purpose.
    return (bool(re.fullmatch(r'[0-9]+',fields['document_number_raw'])) and
            bool(re.fullmatch(r'[0-9?*\ufffd]+',fields['operation_code_raw'])) and
            bool(re.fullmatch(r'[0-9]{2}/[0-9]{2}/[0-9]{4}',fields['transaction_date'])) and
            all(re.fullmatch(r'[0-9][0-9 ,.]*',fields[k]) for k in ('printed_debit','printed_credit')))

def parse_statement(doc,index,dataset_id, *, reviewed=False):
    if reviewed:
        return parse_reviewed(doc,index,dataset_id)
    import archive_adapter as a
    import statement_graph as sg
    contract=header_contract(doc)
    if not contract['recognized']:
        raise ValueError('unproven modern physical column headings')
    lines=[(p['page'],line) for p in doc['pages'] for line in p['lines']]
    blocks=[];current=None;header=[];footer=[];ended=False
    for page,line in lines:
        fields={k:slice_x(line,*bounds) for k,bounds in CELLS.items()}
        left=slice_x(line,65.28,687.96);side=slice_x(line,693.0,850.0)
        if boundary(fields):
            if ended:raise ValueError('bank row after terminal controls')
            if current:blocks.append(current)
            current={'page':page,'last_page':page,'line':line,'fields':fields,'detail':[],
                     'sidecar':[side] if side else [],'purpose':[],'pre_purpose':[],
                     'queue':[],'document_date':None,'gaps':[],'purpose_started':False}
            continue
        if re.fullmatch(r'ОБОРОТЫ',slice_x(line,65.28,397.27)) and not side:
            if current:blocks.append(current);current=None
            ended=True;footer.append((page,line));continue
        if ended:footer.append((page,line));continue
        if current is None:
            header.append(line['raw_text']);continue
        if page!=current['last_page']:
            if not any(x.get('from_page')==current['last_page'] and x.get('to_page')==page and
                       x.get('decision')=='same_operation' and x.get('evidence')
                       for x in doc.get('reviewed_continuations',[])):
                current['gaps'].append('continuation boundary unreviewed')
            current['last_page']=page
        current['detail'].append(line['raw_text'])
        if side:current['sidecar'].append(side)
        # A following same-cell fragment is observed evidence, never completing
        # from expected length. Require metadata phase and no date/money cells.
        if not current['purpose_started'] and not any(fields[k] for k in ('printed_debit','printed_credit','transaction_date')):
            for key,kind in [('bank_code','bank'),('account','account')]:
                fragment=fields[key]
                if fragment and re.fullmatch(r'[A-Z0-9?*\ufffd]+',fragment):
                    current['fields'][key]+='\n'+fragment
        q=re.fullmatch(r'Очередь\s+([0-9]+)',side)
        if q:
            current['queue'].append(q[1]);current['purpose_started']=True
            if left:current['purpose'].append(left)
        elif left:
            if current['purpose_started']:current['purpose'].append(left)
            else:
                current['pre_purpose'].append(left)
                date=fields['document_number_raw']
                if a.DATE.fullmatch(date):
                    if current['document_date'] is not None:current['gaps'].append('duplicate document date')
                    current['document_date']=a.iso(date)
    if current:blocks.append(current)
    if not footer:raise ValueError('modern terminal turnover block absent')
    heading=next(x for x in header if 'ВЫПИСКА ИЗ ЛИЦЕВОГО СЧЕТА ЗА ПЕРИОД' in x)
    period=re.search(r'ПЕРИОД С\s+(\d{2}/\d{2}/\d{4})\s+ПО\s+(\d{2}/\d{2}/\d{4})',heading)
    account_line=next(x for x in header if x.startswith(('Cчет клиента','Счет клиента')))
    am=re.search(r'клиента\s+(\S+)\s+(\S+)\s*$',account_line)
    if not period or not am:raise ValueError('modern statement header unproven')
    opening_raw=next(x for x in header if x.startswith('Входящее сальдо')).split('сальдо',1)[1].strip()
    closing_candidates=[line['raw_text'].split('сальдо',1)[1].strip() for _,line in footer if line['raw_text'].startswith('Исходящее сальдо')]
    if len(closing_candidates)!=1:raise ValueError('modern closing control absent or ambiguous')
    closing_raw=closing_candidates[0];turnover=footer[0][1]
    dr=slice_x(turnover,*CELLS['printed_debit']);cr=slice_x(turnover,*CELLS['printed_credit'])
    alias='statement:'+str(index);acc='account:'+str(index);cur='currency:'+str(index)
    props={'account_id':acc,'currency_context_id':cur,'statement_date':None,'period_start':a.iso(period[1]),'period_end':a.iso(period[2]),
           'opening':a.component_money(opening_raw),'closing':a.component_money(closing_raw),'printed_debit_total':a.component_money(dr),
           'printed_credit_total':a.component_money(cr),'debit_is':'outflow','opening_sign_rule':'signed','rows_complete':False,
           'expected_operations':len(blocks),'selection':'include','selection_evidence':[],'year':int(period[1][-4:]),
           'heading_raw':heading,'executor_raw':None,'previous_operation_date':None,'first_statement_marker':None,
           'account_number_raw':am[1],'first_account_evidence':[],'opening_origin':'printed'}
    st={'alias':alias,'properties':props,'observations':[a.obs(k,raw,props[k]) for k,raw in [('opening',opening_raw),('closing',closing_raw),('printed_debit_total',dr),('printed_credit_total',cr)]],
        'unrecognized_fields':[],'operations':[]}
    st['observations'].append(a.obs('diagnostic_anatomy_profile',PROFILE,PROFILE))
    st['observations'].append(a.obs('business_header_raw','\n'.join(header),None))
    entities=[a.entry(acc,'Account',{'number_raw':am[1],'format':'historical' if am[1].isdigit() else 'unknown','identity_status':'observed',
              'iban_effective_date':None,'iban_raw':None,'bic_raw':None}),
              a.entry(cur,'CurrencyContext',{'printed_currency':am[2],'unit_raw':None,'semantics_status':'unknown','epoch':None,'conversion_factor':None,'evidence_refs':[]})]
    relations=[];rows=[]
    for ordinal,b in enumerate(blocks,1):
        f=b['fields'];debit=a.component_money(f['printed_debit']);credit=a.component_money(f['printed_credit'])
        direction='outflow' if debit is not None and credit is not None and sg.decimal(debit)>0 and sg.decimal(credit)==0 else 'inflow' if debit is not None and credit is not None and sg.decimal(credit)>0 and sg.decimal(debit)==0 else 'unknown'
        purpose='\n'.join(b['purpose']);row_alias=alias+':row:'+str(ordinal)
        # Purpose is diagnostic: the sidecar's semantics and full metadata
        # boundary are not admitted. Never manufacture old role/name labels.
        p={'transaction_date':a.iso(f['transaction_date']),'booking_date':None,'value_date':None,'document_date':b['document_date'],
           'service_period_raw':None,'document_number_raw':f['document_number_raw'],'document_type_raw':None,'unlabelled_numeric_raw':None,
           'printed_debit':debit,'printed_credit':credit,'direction':direction,'amount':debit if direction=='outflow' else credit if direction=='inflow' else None,
           'bank_debit_is':'outflow','purpose_raw':purpose,'payment1c_comment_raw':purpose,'purpose_status':'unknown',
           'classification':None,'classification_status':'unknown','row_status':'partial','duplicate_status':'unreviewed',
           'operation_code_raw':f['operation_code_raw'],'executor_code_raw':None,'priority_raw':b['queue'][0] if len(b['queue'])==1 else None,
           'counterparty':{'role':'payee' if direction=='outflow' else 'payer' if direction=='inflow' else 'unknown','party_id':None,'name_raw':None,'unp_raw':None,'status':'unknown'}}
        st['operations'].append({'alias':row_alias,'properties':p,'observations':[
            a.obs('diagnostic_anatomy_profile',PROFILE,PROFILE),a.obs('diagnostic_purpose_raw',purpose,purpose,'unknown'),
            a.obs('bank_row_raw',b['line']['raw_text'],None),a.obs('identity_detail_raw','\n'.join(b['detail']),None,'unknown'),
            a.obs('printed_debit',f['printed_debit'],debit),a.obs('printed_credit',f['printed_credit'],credit)],
            'unrecognized_fields':[a.unknown('unlabelled_identity_sidecar_raw','\n'.join(b['sidecar'])),a.unknown('document_date_urgency_acceptance_raw','\n'.join(b['pre_purpose']))]})
        ids={key:bi.recognize(f[key],kind) for key,kind in [('bank_code','bank'),('account','account')]}
        ai=ids['account'];bc=ids['bank_code'];account_alias=row_alias+':account';bank_alias=row_alias+':bank'
        entities += [a.entry(account_alias,'Account',{'number_raw':ai['normalized'] if bi.usable(ai) else None,'format':'iban' if ai['format']=='iban' else 'historical' if ai['format']=='historical_numeric' else 'unknown',
                     'identifier':ai,'identity_status':'observed' if bi.usable(ai) else 'unknown','iban_effective_date':None,'iban_raw':ai['raw'] if ai['format']=='iban' else None,'bic_raw':bc['raw'] if bc['format']=='bic' else None}),
                     a.entry(bank_alias,'Bank',{'name_raw':None,'code_raw':bc['normalized'] if bi.usable(bc) else None,'identifier':bc,'code_system_raw':'Код','valid_from':None,'valid_to':None,'identity_status':'observed' if bi.usable(bc) else 'unknown','branch_raw':None,'city_raw':None,'country_raw':None})]
        relations.append({'type':'SERVICED_BY','from':account_alias,'to':bank_alias})
        if direction!='unknown':relations.append({'type':'TO_ACCOUNT' if direction=='outflow' else 'FROM_ACCOUNT','from':row_alias,'to':account_alias})
        rows.append({'ordinal':ordinal,'page':b['page'],'last_page':b['last_page'],'fields':f,'bank_identifiers':ids,'purpose_raw':purpose,'detail_raw':'\n'.join(b['detail']),'pre_purpose_raw':'\n'.join(b['pre_purpose']),'sidecar_raw':'\n'.join(b['sidecar']),'gaps':b['gaps']+['counterparty role/name/UNP field types unproven']})
    qa={'profile':PROFILE,'operations':len(blocks),'column_contract':contract,'rows':rows,'processing_status':'blocked_format',
        'required_field_proof':{'counterparty_name':'unproven','payer_unp':'unproven','payee_unp':'unproven'},
        'money_controls_derived_from_rows':False,'residual_business_lines':[],
        'header_raw':header,'footer_raw':[line['raw_text'] for _,line in footer],
        'controls_raw':{'opening':opening_raw,'closing':closing_raw,'debit':dr,'credit':cr}}
    return st,entities,relations,qa


# This profile is intentionally separate from diagnostic-v1 and from the old
# explicitly labelled monospaced format. The caller must verify actual private
# owner evidence; these reproducible consistency checks cannot authenticate it.
REVIEWED_PROFILE = 'belarusbank-landscape-owner-role-v2'
ROLE_QUESTION = 'правый блок с названием организации, банком и «УНП:» — это получатель при списании и плательщик при поступлении?'
ROLE_ANSWER = 'Да'
ROLE_MEANING = 'outflow:payee;inflow:payer;origin:owner_confirmed_format_only'


def role_confirmation_valid(value):
    return (isinstance(value,dict) and value.get('question') == ROLE_QUESTION and
            value.get('answer') == ROLE_ANSWER and value.get('scope') == REVIEWED_PROFILE and
            all(isinstance(value.get(k),str) and value[k].strip() for k in ('answered_by','evidence_ref')) and
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',value['evidence_ref']) is not None)


def reviewed_boundary(fields):
    # A candidate is independent of required-field readiness. It becomes a row
    # only with the separate bounded metadata block checked below.
    return all(re.fullmatch(r'[0-9?*\ufffd][0-9 ,.?*\ufffd]*',fields[k])
               for k in ('printed_debit','printed_credit'))


def reviewed_row_context(page, offset):
    """Prove right-block field/queue before another bank anchor on this page.

    Numeric-looking purpose alone cannot start an operation. Values may be
    blank or damaged: the separate printed labels establish field anatomy.
    """
    unp=False
    for line in page['lines'][offset+1:]:
        fields={k:slice_x(line,*bounds) for k,bounds in CELLS.items()}
        if reviewed_boundary(fields) or re.fullmatch(r'ОБОРОТЫ',slice_x(line,65.28,397.27)):
            return False
        side=slice_x(line,693.0,float('inf'))
        if re.match(r'^УНП\s*:',side):unp=True
        if re.fullmatch(r'Очередь\s+[0-9]+',side):return unp
    return False


def date_value(raw):
    import archive_adapter as a
    return a.iso(raw) if isinstance(raw,str) and re.fullmatch(r'[0-9]{2}/[0-9]{2}/[0-9]{4}',raw) else None


HEADING_WORDS = {
    'document_number_raw': {'N','дата','документа','Дата'},
    'operation_code_raw': {'Вид','Опер,','Опер','срочность','вид акцепта'},
    'bank_code': {'Код'}, 'account': {'Корреспондент','Счет'},
    'printed_debit': {'Обор','Дебет'}, 'printed_credit': {'оты','Кредит'},
    'transaction_date': {'Дата'}}


def heading_rows(doc):
    rows=[]
    for line in doc['pages'][0]['lines']:
        cells={k:slice_x(line,*bounds) for k,bounds in CELLS.items()}
        if reviewed_boundary(cells):break
        if any(cells.values()) and all(not v or v in HEADING_WORDS[k] for k,v in cells.items()):
            rows.append({'raw':line['raw_text'].strip(),'cells':cells})
    return rows


def headings_valid(raw,header):
    """Recompute column proof from retained raw cells, never the profile flag."""
    import json
    try: rows=json.loads(raw)
    except (ValueError,TypeError):return False
    if not isinstance(rows,list) or not rows or not isinstance(header,str):return False
    values={k:[] for k in CELLS}; retained=header.split('\n'); previous=-1
    for row in rows:
        if not isinstance(row,dict) or set(row)!= {'raw','cells'}:return False
        cells=row['cells']
        if not isinstance(row['raw'],str) or not isinstance(cells,dict) or set(cells)!=set(CELLS):return False
        if any(not isinstance(v,str) or v and v not in HEADING_WORDS[k] for k,v in cells.items()):return False
        if re.sub(r'\s','',row['raw'])!=re.sub(r'\s','',''.join(cells[k] for k in CELLS)):return False
        try:previous=retained.index(row['raw'],previous+1)
        except ValueError:return False
        for k,v in cells.items():
            if v:values[k].append(v)
    compact={k:[v] for k,v in zip(CELLS,('Дата','Опер','Код','Счет','Дебет','Кредит','Дата'))}
    detailed={'document_number_raw':['N','дата','документа'],
              'operation_code_raw':['Вид','Опер,','срочность','вид акцепта'],
              'bank_code':['Код'],'account':['Корреспондент','Счет'],
              'printed_debit':['Обор','Дебет'],'printed_credit':['оты','Кредит'],'transaction_date':['Дата']}
    return values==compact or values==detailed


def metadata_analysis(rows,raw):
    """Classify every retained metadata cell; unknown text holds its own row."""
    gaps=[];dates=[];urgency=[];acceptance=[]
    if not isinstance(rows,list) or not all(isinstance(x,dict) and set(x)=={'raw','cells'} and
            isinstance(x['raw'],str) and isinstance(x['cells'],dict) and set(x['cells'])==set(CELLS) and
            all(isinstance(v,str) for v in x['cells'].values()) for x in rows):
        return {'document_date':None,'urgency_raw':None,'acceptance_raw':None,'gaps':['modern_metadata_source_unresolved']}
    if '\n'.join(x['raw'] for x in rows)!=raw:gaps.append('modern_metadata_source_unresolved')
    for row in rows:
        f=row['cells']
        if re.sub(r'\s','',row['raw'])!=re.sub(r'\s','',''.join(f[k] for k in CELLS)):gaps.append('modern_unknown_metadata')
        if f['document_number_raw']:
            dates.append(f['document_number_raw'])
            if date_value(f['document_number_raw']) is None:gaps.append('modern_unknown_metadata')
        typ=f['operation_code_raw']
        if typ:
            if re.fullmatch(r'Несрочный|Срочный|НЕСРОЧНЫЙ|СРОЧНЫЙ',typ):urgency.append(typ)
            elif re.fullmatch(r'Без акцепта|С акцептом|БЕЗ АКЦЕПТА|С АКЦЕПТОМ',typ):acceptance.append(typ)
            else:gaps.append('modern_unknown_metadata')
        for key in ('bank_code','account'):
            if f[key] and (not re.fullmatch(r'[A-Za-z0-9?*\ufffd]+',f[key]) or any(f[k] for k in ('printed_debit','printed_credit','transaction_date'))):
                gaps.append('modern_unknown_metadata')
        if any(f[k] for k in ('printed_debit','printed_credit','transaction_date')):gaps.append('modern_unknown_metadata')
    for category, assertions in [('urgency',urgency),('acceptance',acceptance)]:
        if len({x.casefold() for x in assertions}) > 1:
            gaps.append('modern_conflicting_'+category)
    date=date_value(dates[0]) if len(dates)==1 else None
    if date is None:gaps.append('modern_document_date_unresolved')
    return {'document_date':date,'urgency_raw':'\n'.join(urgency) or None,
            'acceptance_raw':'\n'.join(acceptance) or None,'gaps':sorted(set(gaps))}


PASSPORT_BOUNDARY = re.compile(r'(?<!\w)[A-ZА-ЯЁ]{2}[ \n]+[0-9]{7}[ \n]+(?i:ВЫДАН)\b')
PASSPORT_LIKE = re.compile(r'(?<!\w)[A-Za-zА-Яа-яЁё]{2}[ \n]+[0-9?*\ufffd]+[ \n]+(?i:ВЫДАН)\b')
FULL_NAME_PREFIX = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’\-]*(?:[ \n]+[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’\-]*){2}")


def identity_name_from_block(raw):
    """Bounded printed full-name prefix before separately printed passport data.

    Three complete name words must precede one series/number/issued assertion.
    No personal names, directories, purpose text or another row supply a value.
    The complete block stays immutable in modern_fields and source observations.
    """
    import archive_adapter as a
    if not isinstance(raw, str):return None
    boundaries=list(PASSPORT_BOUNDARY.finditer(raw))
    document=bool(re.search(r'паспорт|passport|удостоверени[ея]|личн(?:ый|ого)\s+номер',raw,re.IGNORECASE) or PASSPORT_LIKE.search(raw))
    if not document:return raw if a.readable_name(raw) else None
    if len(boundaries)!=1:return None
    prefix=raw[:boundaries[0].start()].rstrip(' \n')
    if not FULL_NAME_PREFIX.fullmatch(prefix) or not a.readable_name(prefix):return None
    return prefix


def name_observation_matches(raw,value):
    return isinstance(value,str) and identity_name_from_block(raw)==value


def sidecar_fields(raw):
    """Retain the complete block; only narrow bank-bearing anatomy splits it.

    A city prefix alone is never a delimiter. A candidate needs a city/comma,
    a bank legal introducer AND an actual bank lexeme before the generic UNP.
    Multiple candidates are uncertain, never silently choose one.
    """
    import archive_adapter as a
    lines=raw.split('\n'); gaps=[]
    us=[i for i,x in enumerate(lines) if re.match(r'^УНП\s*:',x)]
    qs=[i for i,x in enumerate(lines) if re.match(r'^Очередь(?:\s|$)',x)]
    ui=us[0] if len(us)==1 else len(lines);qi=qs[0] if len(qs)==1 else len(lines)
    if len(us)!=1:gaps.append('modern_unp_assertion_missing_or_multiple')
    if len(qs)!=1:gaps.append('modern_priority_assertion_missing_or_multiple')
    if ui>=qi:gaps.append('modern_metadata_order_unresolved')
    banks=[]
    for i,x in enumerate(lines[:ui]):
        # Explicit case variants, not city-prefix casefold segmentation. The
        # whole remaining bank block must contain a bank-specific word.
        if (re.match(r'^(?:г\.|Г\.)[^,]+,\s*(?:ОАО\b|ЗАО\b|ФИЛИАЛ\b|Филиал\b|[\'"«])',x) and
                re.search(r'банк|bank',re.sub(r'\s','',''.join(lines[i:ui])),re.IGNORECASE)):
            banks.append(i)
    if len(banks)!=1:gaps.append('modern_name_bank_boundary_unresolved')
    bi=banks[0] if len(banks)==1 else None
    name='\n'.join(lines[:bi]) if bi is not None else None
    bank='\n'.join(lines[bi:ui]) if bi is not None else None
    unp=re.sub(r'^УНП\s*:', '', lines[ui],count=1) if len(us)==1 else None
    # The next line is an unlabelled field, not a continuation of tax identity.
    codes=lines[ui+1:qi] if ui<qi else []
    priority=re.fullmatch(r'Очередь\s+([0-9]+)',lines[qi]) if len(qs)==1 else None
    if not priority:gaps.append('modern_priority_unreadable')
    if qi<len(lines)-1 and any(lines[qi+1:]):gaps.append('modern_content_after_priority')
    passport=bool(name and (re.search(r'паспорт|passport|удостоверени[ея]|личн(?:ый|ого)\s+номер',name,re.IGNORECASE) or PASSPORT_LIKE.search(name)))
    identity_name=identity_name_from_block(name)
    if passport and identity_name is None:gaps.append('modern_document_text_not_proven_party_name')
    if identity_name is None:gaps.append('missing_or_unreadable_full_name')
    # Printed ASCII spaces are formatting; controls/Unicode lookalikes are not.
    normalized=unp.replace(' ','') if unp is not None else None
    if not a.readable_unp(normalized):gaps.append('missing_or_unreadable_unp')
    return {'name_raw':name,'name_identity_raw':identity_name,'bank_name_raw':bank,'unp_raw':unp,
            'unp_normalized':normalized if a.readable_unp(normalized) else None,
            'priority_raw':priority[1] if priority else None,'unlabelled_codes_raw':codes,
            'name_status':'observed' if identity_name is not None else 'unknown',
            'gaps':sorted(set(gaps))}


def source_fields(op):
    """Deterministically bind saved typed fields to retained original text."""
    observations=op.get('observations',[])
    def one(field):
        values=[o for o in observations if o['field']==field]
        return values[0] if len(values)==1 else None
    side=one('modern_sidecar_raw');detail=one('modern_business_detail_raw');meta=one('modern_metadata_raw');purpose=one('modern_purpose_raw')
    if not all(x and isinstance(x['raw'],str) for x in (side,detail,meta,purpose)):
        return {'gaps':['modern_original_fields_missing'],'name_raw':None,'unp_normalized':None,'purpose_raw':None}
    result=sidecar_fields(side['raw']);result['purpose_raw']=purpose['raw']
    result['metadata_raw']=meta['raw']
    cells=one('modern_metadata_cells_raw')
    try:metadata=__import__('json').loads(cells['raw']) if cells and cells['status']=='observed' else None
    except (ValueError,TypeError):metadata=None
    result['metadata']=metadata_analysis(metadata,meta['raw'])
    result['gaps'].extend(result['metadata']['gaps'])
    if isinstance(metadata,list) and 'modern_metadata_source_unresolved' not in result['metadata']['gaps']:
        for key,field in [('bank_code','modern_bank_code_raw'),('account','modern_account_raw')]:
            value=one(field)
            fragments=[x['cells'][key] for x in metadata if x['cells'][key] and
                       re.fullmatch(r'[A-Za-z0-9?*\ufffd]+',x['cells'][key]) and
                       not any(x['cells'][k] for k in ('printed_debit','printed_credit','transaction_date'))]
            if not value or not isinstance(value['raw'],str) or value['raw'].split('\n')[1:]!=fragments:
                result['gaps'].append('modern_metadata_source_unresolved')
    transaction=one('transaction_date')
    if (not transaction or transaction['status']!='observed' or
            date_value(transaction['raw']) is None):result['gaps'].append('modern_transaction_date_unresolved')
    code=one('operation_code_raw')
    if (not code or code['status']!='observed' or not isinstance(code['raw'],str) or
            re.fullmatch(r'[0-9]+',code['raw']) is None):
        result['gaps'].append('modern_operation_code_unresolved')
    # Separate source streams remain exact; no legacy labels are manufactured.
    if detail['raw'] != '\n'.join(x for x in (meta['raw'],purpose['raw']) if x):
        result['gaps'].append('modern_original_business_coverage_mismatch')
    if any(x['status']!='observed' for x in (side,detail,meta,purpose)):
        result['gaps'].append('identity_source_boundary_or_glyphs_unresolved')
    return result


def format_supported(statement,operations):
    obs=statement.get('observations',[])
    def one(field,raw,normalized,status):
        return sum(o['field']==field and o['raw']==raw and o['normalized']==normalized and o['status']==status for o in obs)==1
    labels={match[0] for op in operations for o in op.get('observations',[]) if o['field']=='modern_sidecar_raw' and isinstance(o['raw'],str) for match in re.finditer(r'(?m)^УНП\s*:',o['raw'])}
    proof=[o for o in obs if o['field']=='modern_role_confirmation']
    try:confirmation=__import__('json').loads(proof[0]['raw']) if len(proof)==1 else None
    except (ValueError,TypeError):confirmation=None
    headings=[o for o in obs if o['field']=='modern_column_headings']
    headers=[o for o in obs if o['field']=='business_header_raw']
    return (one('modern_anatomy_profile',REVIEWED_PROFILE,REVIEWED_PROFILE,'observed') and
            len(proof)==1 and proof[0]['status']=='inferred' and proof[0]['normalized']==ROLE_MEANING and
            isinstance(proof[0]['raw'],str) and role_confirmation_valid(confirmation) and
            any(one('modern_generic_identity_label',label,'counterparty_unp','observed') for label in labels) and labels and
            len(headings)==1 and headings[0]['status']=='observed' and headings[0]['normalized']==REVIEWED_PROFILE and
            len(headers)==1 and headers[0]['status']=='observed' and headings_valid(headings[0]['raw'],headers[0]['raw']))


def electronic_signature_lines(page):
    """Only the complete four-line export signature at the left bottom margin.

    Text elsewhere, partial blocks, unknown lines or displaced geometry remain
    business evidence; no keyword or page-height truncation is used.
    """
    lines=page['lines'];height=page.get('height');excluded=set()
    if not isinstance(height,(int,float)):return excluded
    patterns=(r'-{3}Электронно цифровая подпись:-+',r'ИД эл\. кл\.:\s+\S+(?:\s+\S+)*',
              r'Владелец ключа:\s+\S.*',r'Подписано:\s+[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}')
    for start in range(len(lines)-3):
        block=lines[start:start+4]
        if not all(re.fullmatch(pattern,line['raw_text'].strip()) for pattern,line in zip(patterns,block)):continue
        if not 34<=height-block[0].get('top',0)<=36:continue
        if not all(line.get('glyphs') and abs(min(g['x0'] for g in line['glyphs'])-15)<0.5 and
                   abs(line.get('top',0)-block[0]['top']-5*i)<0.5 for i,line in enumerate(block)):continue
        if any(x['raw_text'].strip() for x in lines[start+4:]):continue
        excluded.update(range(start,start+4))
    return excluded


def reviewed_export_header_pages(doc):
    """Recognize only the observed repeated landscape export header geometry.

    A first-page header proves the decoration template for this original. Each
    later page must retain that same full text, at its own proven first-line
    position; a damaged/different page is held without discarding its evidence.
    """
    from datetime import datetime
    pages=doc.get('pages',[])
    def near(value,expected):
        return isinstance(value,(int,float)) and abs(value-expected)<=0.05
    def header(page):
        if (not near(page.get('width'),842.04) or
                not near(page.get('height'),595.32) or not page.get('lines')):
            return None
        line=page['lines'][0];raw=line.get('raw_text');glyphs=line.get('glyphs',[])
        if not isinstance(raw,str):return None
        match=re.fullmatch(r'([0-9]{2}\.[0-9]{2}\.[0-9]{4})г\. ОАО АСБ "Беларусбанк"\. ПК "Клиент-банк \(WEB\)"',raw)
        if not match:return None
        try:datetime.strptime(match[1],'%d.%m.%Y')
        except ValueError:return None
        if not near(line.get('top'),5.128) or len(glyphs)!=len(raw):return None
        for i,(glyph,char) in enumerate(zip(glyphs,raw)):
            if (glyph.get('text')!=char or not near(glyph.get('x0'),15+3.6*i) or
                    not near(glyph.get('x1'),18.6+3.6*i) or
                    not near(glyph.get('top'),5.128)):return None
        return raw
    reference=header(pages[0]) if pages else None
    return {page['page'] for page in pages if reference and header(page)==reference}


def structural_line(line,page,last_page,terminal=False,reviewed_export_header=False):
    """Bounded page decoration, never keyword deletion inside business text."""
    raw=line['raw_text'].strip();top=line.get('top',0)
    if reviewed_export_header:return 'page_header'
    if not raw:return 'blank'
    if top<12 and re.fullmatch(r'Клиент-банк \(WEB\)(?: [0-9]+)?',raw):return 'page_header'
    if terminal:return 'terminal_control_or_seal'
    return None


def reviewed_blocks(doc):
    """Physical bank rows and full-width purpose with reviewed page joins."""
    blocks=[];current=None;header=[];footer=[];excluded=[];residual=[];ended=False
    last_page=doc['pages'][-1]['page']
    export_header_pages=reviewed_export_header_pages(doc)
    for page in doc['pages']:
        signature=electronic_signature_lines(page)
        for offset,line in enumerate(page['lines']):
            pn=page['page'];raw=line['raw_text'].strip()
            if offset in signature:
                excluded.append({'page':pn,'line':offset,'reason':'electronic_signature','raw':line['raw_text']})
                continue
            why=structural_line(line,pn,last_page,ended,offset==0 and pn in export_header_pages)
            if why:
                excluded.append({'page':pn,'line':offset,'reason':why,'raw':line['raw_text']})
                if ended:footer.append((pn,line))
                continue
            fields={k:slice_x(line,*bounds) for k,bounds in CELLS.items()}
            left=slice_x(line,65.28,687.96);side=slice_x(line,693.0,float('inf'))
            # Only a terminal last-page label aligned with both monetary cells
            # and followed by closing control establishes the footer.
            if (pn==last_page and re.fullmatch(r'ОБОРОТЫ',slice_x(line,65.28,397.27)) and not side and
                    all(fields[k] and re.fullmatch(r'[0-9][0-9 ,.]*',fields[k]) for k in ('printed_debit','printed_credit')) and
                    any(x['raw_text'].strip().startswith('Исходящее сальдо') for x in page['lines'][offset+1:])):
                if current:blocks.append(current);current=None
                ended=True;footer.append((pn,line));continue
            if reviewed_boundary(fields):
                if reviewed_row_context(page,offset):
                    if current:blocks.append(current)
                    current={'page':pn,'last_page':pn,'line':line,'fields':fields,'sidecar':[side] if side else [],
                             'metadata':[],'metadata_cells':[],'purpose':[],'queue':[],
                             'gaps':[],'purpose_started':False}
                    continue
                # Keep spelling in the original stream AND private QA. Unknown
                # boundaries must never certify a valid subset of operations.
                residual.append({'page':pn,'line':offset,'raw':line['raw_text'],
                                 'reason':'unproven bank-row boundary'})
            if current is None:
                header.append(line['raw_text'].strip());continue
            unknown_margin=(isinstance(page.get('height'),(int,float)) and line.get('glyphs') and
                    14<=page['height']-line.get('top',0)<=36 and
                    min(g['x0'] for g in line['glyphs'])<65.28)
            # Unknown top-margin anatomy is independent of spelling. Any
            # purpose glyph left of the proven business origin would otherwise
            # vanish during column slicing; retain the whole raw line and hold.
            unknown_margin = unknown_margin or (current['purpose_started'] and
                    (line.get('top',0)<12 or any(g['x0']<65.28 for g in line.get('glyphs',[]))))
            if unknown_margin:
                current['gaps'].append('unreviewed margin service anatomy')
            if pn!=current['last_page']:
                if not any(x.get('from_page')==current['last_page'] and x.get('to_page')==pn and
                           x.get('decision')=='same_operation' and x.get('operation_ordinal')==len(blocks)+1 and x.get('evidence') for x in doc.get('reviewed_continuations',[])):
                    current['gaps'].append('continuation boundary unreviewed')
                current['last_page']=pn
            if current['purpose_started']:
                # The original purpose spans past the table and sidecar columns.
                current['purpose'].append(line['raw_text'].strip() if unknown_margin else slice_x(line,65.28,float('inf')))
                continue
            if side:current['sidecar'].append(side)
            q=re.fullmatch(r'Очередь\s+([0-9]+)',side)
            if q:
                current['queue'].append(q[1]);current['purpose_started']=True
                if left:current['purpose'].append(left)
                continue
            if left:
                current['metadata'].append(left)
                current['metadata_cells'].append({'raw':left,'cells':fields})
                # Same-cell wrapping is observed, never completed by length.
                if not any(fields[k] for k in ('printed_debit','printed_credit','transaction_date')):
                    for key in ('bank_code','account'):
                        frag=fields[key]
                        if frag and re.fullmatch(r'[A-Za-z0-9?*\ufffd]+',frag):current['fields'][key]+='\n'+frag
    if current:blocks.append(current)
    if not footer:raise ValueError('modern terminal turnover block absent')
    return blocks,header,footer,excluded,residual


def parse_reviewed(doc,index,dataset_id):
    import json
    import archive_adapter as a
    import statement_graph as sg
    contract=header_contract(doc)
    if not contract['recognized']:raise ValueError('unproven modern physical headings')
    if not role_confirmation_valid(doc.get('role_confirmation')):raise ValueError('separately verified original owner role evidence required')
    blocks,header,footer,excluded,residual=reviewed_blocks(doc)
    heading_raw=json.dumps(heading_rows(doc),ensure_ascii=False,sort_keys=True)
    if not headings_valid(heading_raw,'\n'.join(header)):raise ValueError('unproven modern retained heading cells')
    if not blocks:raise ValueError('modern profile requires actual operation identity fields')
    # The last-page control region has a reviewed bounded seal grammar. Unknown
    # business text or an extra bank row after totals is not called decoration.
    seal_patterns=(r'Исходящее сальдо\s*-?[0-9][0-9 ,.]*',r'(?:ОАО|ЗАО)\s+.*(?:банк|Банк|БАНК).*',
                   r'Отдел расчетно-кассового',r'обслуживания',r'БИК [A-Z0-9]{8,11}(?: [0-9]{2}\.[0-9]{2}\.[0-9]{4})?',
                   r'[0-9]+ [0-9]{2}\.[0-9]{2}\.[0-9]{4}',r'Минск')
    if any(line['raw_text'].strip() and not any(re.fullmatch(p,line['raw_text'].strip()) for p in seal_patterns)
           for _,line in footer[1:]):raise ValueError('unreviewed terminal business text or seal anatomy')

    heading=next(x for x in header if 'ВЫПИСКА ИЗ ЛИЦЕВОГО СЧЕТА ЗА ПЕРИОД' in x)
    period=re.search(r'ПЕРИОД С\s+(\d{2}/\d{2}/\d{4})\s+ПО\s+(\d{2}/\d{2}/\d{4})',heading)
    account_line=next(x for x in header if x.startswith(('Cчет клиента','Счет клиента')))
    am=re.search(r'клиента\s+(\S+)\s+(\S+)\s*$',account_line)
    opening_raw=next(x for x in header if x.startswith('Входящее сальдо')).split('сальдо',1)[1].strip()
    closing=[line['raw_text'].strip().split('сальдо',1)[1].strip() for _,line in footer if line['raw_text'].strip().startswith('Исходящее сальдо')]
    if not period or not am or len(closing)!=1:raise ValueError('modern statement controls/header unproven')
    dr=slice_x(footer[0][1],*CELLS['printed_debit']);cr=slice_x(footer[0][1],*CELLS['printed_credit'])
    alias='statement:'+str(index);acc='account:'+str(index);cur='currency:'+str(index)
    props={'account_id':acc,'currency_context_id':cur,'statement_date':None,'period_start':a.iso(period[1]),'period_end':a.iso(period[2]),
           'opening':a.component_money(opening_raw),'closing':a.component_money(closing[0]),'printed_debit_total':a.component_money(dr),'printed_credit_total':a.component_money(cr),
           'debit_is':'outflow','opening_sign_rule':'signed','rows_complete':True,'expected_operations':len(blocks),'selection':'include','selection_evidence':[],
           'year':int(period[1][-4:]),'heading_raw':heading,'executor_raw':None,'previous_operation_date':None,'first_statement_marker':None,
           'account_number_raw':am[1],'first_account_evidence':[],'opening_origin':'printed'}
    st={'alias':alias,'properties':props,'observations':[a.obs(k,raw,props[k]) for k,raw in [('opening',opening_raw),('closing',closing[0]),('printed_debit_total',dr),('printed_credit_total',cr)]], 'unrecognized_fields':[],'operations':[]}
    st['observations'] += [a.obs('business_header_raw','\n'.join(header),None),a.obs('modern_anatomy_profile',REVIEWED_PROFILE,REVIEWED_PROFILE),
                          a.obs('modern_role_confirmation',json.dumps(doc['role_confirmation'],ensure_ascii=False,sort_keys=True),ROLE_MEANING,'inferred'),
                          a.obs('modern_generic_identity_label',next((match[0] for b in blocks for x in b['sidecar'] if (match:=re.match(r'^УНП\s*:',x))),''),'counterparty_unp'),
                          a.obs('modern_column_headings',heading_raw,REVIEWED_PROFILE)]
    st['observations'] += [a.obs('unassigned_business_raw',x['raw'],None,'unknown') for x in residual]
    own=bi.recognize(am[1],'account')
    entities=[a.entry(acc,'Account',{'number_raw':am[1],'format':'iban' if own['format']=='iban' else 'historical' if own['format']=='historical_numeric' else 'unknown',
              'identifier':own,'identity_status':'observed' if bi.usable(own) else 'unknown','iban_effective_date':None,'iban_raw':am[1] if own['format']=='iban' else None,'bic_raw':None}),
              a.entry(cur,'CurrencyContext',{'printed_currency':am[2],'unit_raw':None,'semantics_status':'unknown','epoch':None,'conversion_factor':None,'evidence_refs':[]})]
    relations=[];rows=[]
    a.materialize_source_header(st, entities, relations, header, index)
    signatures=[]
    for ordinal,b in enumerate(blocks,1):
        f=b['fields'];debit=a.component_money(f['printed_debit']);credit=a.component_money(f['printed_credit'])
        direction='outflow' if debit is not None and credit is not None and sg.decimal(debit)>0 and sg.decimal(credit)==0 else 'inflow' if debit is not None and credit is not None and sg.decimal(credit)>0 and sg.decimal(debit)==0 else 'unknown'
        role={'outflow':'payee','inflow':'payer'}.get(direction,'unknown');row_alias=alias+':row:'+str(ordinal);party=row_alias+':'+role
        side='\n'.join(b['sidecar']);sf=sidecar_fields(side);purpose='\n'.join(b['purpose']);meta='\n'.join(b['metadata'])
        md=metadata_analysis(b['metadata_cells'],meta)
        transaction_date=date_value(f['transaction_date'])
        date_gaps=[] if transaction_date else ['modern_transaction_date_unresolved']
        gaps=sorted(set(b['gaps']+sf['gaps']+md['gaps']+date_gaps))
        # A closed, source-reviewed empty purpose is allowed; missing/unreviewed
        # boundaries remain unknown independently of the text's length.
        ps='observed' if not b['gaps'] and len(b['queue'])==1 else 'unknown'
        complete=not b['gaps'] and not md['gaps'] and transaction_date is not None and direction!='unknown' and re.fullmatch(r'[0-9]+',f['operation_code_raw']) is not None
        known=role!='unknown' and not sf['gaps'] and not b['gaps']
        name=sf['name_identity_raw'] if sf['name_status']=='observed' else None
        p={'transaction_date':transaction_date,'booking_date':None,'value_date':None,'document_date':md['document_date'],
           'service_period_raw':None,'document_number_raw':f['document_number_raw'],'document_type_raw':None,'unlabelled_numeric_raw':None,
           'printed_debit':debit,'printed_credit':credit,'direction':direction,'amount':debit if direction=='outflow' else credit if direction=='inflow' else None,
           'bank_debit_is':'outflow','purpose_raw':purpose,'payment1c_comment_raw':purpose,'purpose_status':ps,
           'classification':None,'classification_status':'unknown','row_status':'complete' if complete else 'partial','duplicate_status':'unreviewed',
           'operation_code_raw':f['operation_code_raw'],'executor_code_raw':None,'priority_raw':sf['priority_raw'],
           'modern_fields':{'sidecar_raw':side,'name_raw':sf['name_raw'],'bank_name_raw':sf['bank_name_raw'],'unp_raw':sf['unp_raw'],
                            'unlabelled_codes_raw':sf['unlabelled_codes_raw'],'urgency_raw':md['urgency_raw'],'acceptance_raw':md['acceptance_raw']},
           'counterparty':{'role':role,'party_id':party if role!='unknown' else None,'name_raw':name,'unp_raw':sf['unp_normalized'],'status':'observed' if known else 'unknown'}}
        standalone=ps=='observed' and a.cash_acceptance_fee(purpose,direction)
        if standalone:p.update(classification='bank_fee',classification_status='inferred')
        components=a.fee_basis(purpose) if standalone else a.amount_components(purpose,direction,p['amount'],am[2])
        if ps=='unknown':components=[(*c[:4],'unknown') for c in components]
        p['settlement']=a.settlement_view(components,direction,p['amount'],standalone)
        for ci,(offset,kind,value_raw,mention,inclusion) in enumerate(components,1):
            comp=row_alias+':component:'+str(ci);unit,currency=a.component_unit(purpose,offset,mention)
            entities.append(a.entry(comp,'AmountComponent',{'kind':kind,'value_raw':value_raw,'value':a.component_money(value_raw),'unit_raw':unit,'currency_raw':currency,'inclusion':inclusion},[a.obs('mention_raw',mention,None)]))
            relations.append({'type':'HAS_COMPONENT','from':row_alias,'to':comp})
        signature=(f['document_number_raw'],f['transaction_date'],f['operation_code_raw'],f['bank_code'],f['account'],f['printed_debit'],f['printed_credit'],side,purpose)
        signatures.append(signature)
        observations=[a.obs('bank_row_raw',b['line']['raw_text'],None),a.obs('identity_detail_raw',side,None,'observed' if not b['gaps'] else 'unknown'),
                      a.obs('modern_sidecar_raw',side,None,'observed' if not b['gaps'] else 'unknown'),
                      a.obs('modern_metadata_raw',meta,None,'observed' if not b['gaps'] else 'unknown'),
                      a.obs('modern_metadata_cells_raw',json.dumps(b['metadata_cells'],ensure_ascii=False,sort_keys=True),None),
                      a.obs('modern_purpose_raw',purpose,purpose,ps),a.obs('modern_business_detail_raw','\n'.join(x for x in (meta,purpose) if x),None,'observed' if not b['gaps'] else 'unknown'),
                      a.obs('printed_debit',f['printed_debit'],debit),a.obs('printed_credit',f['printed_credit'],credit),
                      a.obs('modern_bank_code_raw',f['bank_code'],None),a.obs('modern_account_raw',f['account'],None),
                      a.obs('transaction_date',f['transaction_date'],p['transaction_date']),a.obs('document_number_raw',f['document_number_raw'],f['document_number_raw']),a.obs('operation_code_raw',f['operation_code_raw'],f['operation_code_raw'])]
        st['operations'].append({'alias':row_alias,'properties':p,'observations':observations,'unrecognized_fields':[a.unknown('unlabelled_code',x) for x in sf['unlabelled_codes_raw']]})
        ids={k:bi.recognize(f[k],kind) for k,kind in [('bank_code','bank'),('account','account')]};ai=ids['account'];bc=ids['bank_code']
        if not bi.usable(ai):p['row_status']='partial'
        entities += [a.entry(row_alias+':account','Account',{'number_raw':ai['normalized'] if ai['format_status']=='recognized' else None,'format':'iban' if ai['format']=='iban' else 'historical' if ai['format']=='historical_numeric' else 'unknown',
                     'identifier':ai,'identity_status':'observed' if bi.usable(ai) else 'unknown','iban_effective_date':None,'iban_raw':ai['raw'] if ai['format']=='iban' else None,'bic_raw':bc['raw'] if bc['format']=='bic' else None}),
                     a.entry(row_alias+':bank','Bank',{'name_raw':sf['bank_name_raw'],'code_raw':bc['normalized'] if bc['format_status']=='recognized' else None,'identifier':bc,'code_system_raw':'Код','valid_from':None,'valid_to':None,'identity_status':'observed' if bi.usable(bc) else 'unknown','branch_raw':None,'city_raw':None,'country_raw':None})]
        relations.append({'type':'SERVICED_BY','from':row_alias+':account','to':row_alias+':bank'})
        if role!='unknown':
            relations += [{'type':'TO_ACCOUNT' if direction=='outflow' else 'FROM_ACCOUNT','from':row_alias,'to':row_alias+':account'},{'type':role.upper(),'from':row_alias,'to':party}]
            entities.append(a.entry(party,'Party',{'name_raw':name,'unp_raw':sf['unp_normalized'],'role_raw':role,'identity_status':'observed' if known else 'unknown'},
                            [a.obs('name_raw',sf['name_raw'],name,sf['name_status']),a.obs('unp_raw',sf['unp_raw'],sf['unp_normalized'],'observed' if sf['unp_normalized'] else 'unknown')]))
        rows.append({'ordinal':ordinal,'page':b['page'],'last_page':b['last_page'],'fields':f,'bank_identifiers':ids,'purpose_raw':purpose,'sidecar_raw':side,'identity':sf,'metadata_raw':meta,'gaps':gaps,'document_view':dict(p)})
    from collections import Counter
    counts=Counter(signatures)
    for row,sig in zip(st['operations'],signatures):row['properties']['duplicate_status']='unique' if counts[sig]==1 else 'unreviewed'
    st['properties']['rows_complete']=all(o['properties']['row_status']=='complete' for o in st['operations'])
    qa={'profile':REVIEWED_PROFILE,'operations':len(blocks),'column_contract':contract,'rows':rows,'excluded_structural_lines':excluded,
        'role_semantics_origin':'owner_confirmed_format_only','header_raw':header,'footer_raw':[x['raw_text'] for _,x in footer],
        'controls_raw':{'opening':opening_raw,'closing':closing[0],'debit':dr,'credit':cr},'residual_business_lines':residual}
    return st,entities,relations,qa



def metadata_values(raw):
    import archive_adapter as a
    dates=[a.iso(x) for x in re.findall(r'(?<![0-9])[0-9]{2}/[0-9]{2}/[0-9]{4}(?![0-9])',raw)]
    urgency=re.findall(r'(?<![А-Яа-я])(?:Несрочный|Срочный|Несрочный|НЕСРОЧНЫЙ|СРОЧНЫЙ)(?![А-Яа-я])',raw)
    acceptance=re.findall(r'Без акцепта|С акцептом|БЕЗ АКЦЕПТА|С АКЦЕПТОМ',raw)
    return dates,'\n'.join(urgency) or None,'\n'.join(acceptance) or None



def refresh_document_views(graph, qa):
    """Use the actual materialized graph for private downstream views."""
    operations=sorted((n for n in graph['nodes'] if n['type']=='Operation'),key=lambda n:n['properties']['ordinal'])
    if len(operations)!=len(qa['rows']):raise ValueError('modern QA row coverage mismatch')
    for node,row in zip(operations,qa['rows']):
        p=node['properties']
        if p['ordinal']!=row['ordinal']:raise ValueError('modern QA row sequence mismatch')
        ready=p.get('processing',{}).get('status')=='ready'
        row['document_view']={**p,'amount_origin':'same_operation_bank_cells' if p['amount'] is not None else 'unknown',
                              'bank_document_kind_candidate':('Поступление на расчетный счет' if p['direction']=='inflow' else 'Списание с расчетного счета') if ready else None}
    return qa
