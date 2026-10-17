#!/usr/bin/env python3
"""Observed Belarusbank monospaced archive adapter. Offline; no source mutation.

prepare creates private geometry QA; assemble takes a JSON array of geometry
paths and writes reviewed business input, plus separate coverage QA. Dataset ID
is supplied as an opaque business namespace. Every body block remains raw.
"""
from __future__ import annotations
import argparse
import json
import re
import statistics
import unicodedata
from datetime import datetime
from pathlib import Path
import statement_graph as sg
import bank_identifiers as identifiers

DATE = re.compile(r'^\d{2}/\d{2}/\d{4}$')

# These are character cells, not offsets in a proportional-font text export.
# Keep the reviewed header evidence in private QA, never in the business graph.
COLUMNS = {'document_number_raw': (0, 6), 'operation_code_raw': (6, 11),
           'executor_code_raw': (11, 15), 'bank_code': (15, 25),
           'account': (25, 39), 'printed_debit': (39, 62),
           'printed_credit': (62, 84), 'date': (84, None)}
HEADER_BARS = [6, 11, 15, 25, 39, 62, 84]
DATE_SUFFIX = re.compile(r'^\s*(\d\s*\d\s*/\s*\d\s*\d\s*/\s*\d\s*\d\s*\d\s*\d)\s+(\d+)\s*$')


WIDE_BARS = [6, 11, 15, 27, 62, 85, 107]


def column_layout(doc):
    """Only two independently reviewed printed column geometries are supported."""
    for page in doc['pages']:
        for line in page['lines']:
            bars = [i for i, c in enumerate(line['text']) if c == '|']
            if bars == WIDE_BARS and all(x in line['text'] for x in ('Код', 'Счет', 'Дебет', 'Кредит')):
                return dict(zip(COLUMNS, zip([0] + WIDE_BARS, WIDE_BARS + [None]))), WIDE_BARS
    return COLUMNS, HEADER_BARS


def header_contract(doc):
    """Recognize this observed layout; missing/shifted headings hold assembly."""
    columns, bars_expected = column_layout(doc)
    bank_start, bank_end = columns['bank_code']
    account_start, account_end = columns['account']
    debit_start, debit_end = columns['printed_debit']
    credit_start, date_start = columns['printed_credit']
    def middle_fragment(text):
        return bool(re.fullmatch(r'\s*\|опер\|исп\|-{' + str(debit_start - 16) + r'}\+-{45}\s+Дата\s*', text))

    def bottom_fragment(text):
        bars = [i for i, c in enumerate(text) if c == '|']
        return (len(bars) == len(bars_expected) and
                [b - bars[0] for b in bars] == [b - bars_expected[0] for b in bars_expected] and
                not text[:bars[0]].strip() and not text[bars[-1] + 1:].strip() and
                not text[bars[0] + 1:bars[1]].strip() and not text[bars[1] + 1:bars[2]].strip() and
                all(label in text[bars[n] + 1:bars[n + 1]]
                    for n, label in ((2, 'Код'), (3, 'Счет'), (4, 'Дебет'), (5, 'Кредит'))))

    evidence = []
    for page in doc['pages']:
        covered = set()
        for offset, line in enumerate(page['lines']):
            if offset in covered:
                continue
            following = page['lines'][offset + 1] if offset + 1 < len(page['lines']) else None
            if middle_fragment(line['text']) and following is not None and bottom_fragment(following['text']):
                # A surviving middle/bottom pair proves a table boundary, but
                # cannot establish the missing top line's column contract.
                text = following['text']
                evidence.append({'page': page['page'], 'line_offset': offset,
                                 'heading_offsets': [offset, offset + 1],
                                 'heading_raw': following['raw_text'], 'heading_cells': text,
                                 'bars': [i for i, c in enumerate(text) if c == '|'],
                                 'recognized': False, 'gap': 'top heading line missing'})
                covered.update((offset, offset + 1))
                continue
            if middle_fragment(line['text']) or bottom_fragment(line['text']):
                text = line['text']
                evidence.append({'page': page['page'], 'line_offset': offset,
                                 'heading_offsets': [offset], 'heading_raw': line['raw_text'],
                                 'heading_cells': text, 'bars': [i for i, c in enumerate(text) if c == '|'],
                                 'recognized': False, 'gap': 'only one structural heading line survives'})
                covered.add(offset)
                continue
            if not line['text'].lstrip().startswith('N док'):
                continue
            # Each repeated heading starts a new contract, including incomplete
            # headings. Evidence from another page cannot repair this segment.
            heading = page['lines'][offset:offset + 3]
            top = line['text']
            middle = heading[1]['text'] if len(heading) > 1 else ''
            bottom = heading[2] if len(heading) > 2 else {'text': '', 'raw_text': ''}
            text = bottom['text']
            # Prose such as "N документа ..." is not a table boundary. An
            # incomplete real heading still holds its segment when its full
            # distinctive top labels or both adjacent structural lines survive.
            top_shape = ('Вид' in top and 'Корресподнент' in top and 'Обороты' in top)
            structural_offsets = [n for n, item in enumerate(heading[1:], 1)
                                  if middle_fragment(item['text']) or bottom_fragment(item['text'])]
            if not (top_shape or len(structural_offsets) == 2):
                continue
            bars = [i for i, c in enumerate(text) if c == '|']
            valid = (len(heading) == 3 and 'Вид' in top and 'Корресподнент' in top and
                     'Обороты' in top and '|опер|исп|' in middle and 'Дата' in middle[date_start:] and
                     bars == bars_expected and 'Код' in text[bank_start:bank_end] and 'Счет' in text[account_start:account_end] and
                     'Дебет' in text[debit_start:debit_end] and 'Кредит' in text[credit_start:date_start] and
                     not any(x.get('overlaps') for x in heading))
            evidence.append({'page': page['page'], 'line_offset': offset,
                             'heading_offsets': ([offset, offset + 1, offset + 2] if valid else
                                                 [offset] + [offset + n for n in structural_offsets]),
                             'heading_raw': bottom['raw_text'], 'heading_cells': text,
                             'bars': bars, 'recognized': valid})
            covered.update(evidence[-1]['heading_offsets'])
    return bool(evidence) and all(x['recognized'] for x in evidence), evidence


_COMPONENT_UNIT_PATTERN = r'(?:[Рр]\s*[Уу][Бб](?:[Лл][Ее]\s*[Йй])?\.?|[A-Za-z]{3}\b)'


def amount_components(purpose, direction, amount=None, currency=None):
    """Explicit labelled details only; never infer a cash movement from text.

    Bank direction is primary. An incoming acquiring/withholding fee does not
    require a printed gross. Generic description totals remain references.
    """
    # Observed archive glyph spellings: Latin C in the first Cyrillic с slot,
    # and an inserted horizontal space after К. Matches retain the raw mention.
    commission = r'к[ \u00a0]*оми[сc]си'
    separator = r'(?::\s*|-\s+)?'  # attached minus belongs to the numeric token
    labels = [(commission + r'[яию](?: банка)?\s*' + separator, 'fee'),
              (r'удержан[а-я]*\s+' + commission + r'[а-я]*\s*' + separator, 'fee'),
              (r'(?:(?:общая сумма|сумма операций|сумма брутто|брутто|brutto|gross)\s*(?:[-:]\s*)?|на\s+(?:общую\s+)?сумму(?:\s*[-:]\s*|\s+))', 'gross'),
              (r'(?:сумма нетто|net)\s*(?:[-:]\s*)?', 'net')]
    found = {}
    for label, kind in labels:
        # Consume the whole numeric token before checking its unit. Keeping
        # unit rejection outside the regex prevents backtracking into a prefix.
        for match in re.finditer(r'(?i)(?<!\w)(' + label + r')(-?[\d?*][\d?*., \u00a0]*)', purpose):
            remaining = purpose[match.end(2):]
            if re.match(r'(?i)^(?:[.,]\s*\d|\s*(?:\d|%|процент[а-я]*\b|проц\b\.?|percents?\b))', remaining):
                continue
            value_raw = match[2]
            # A sentence/list separator after the number is not a fractional
            # digit. Interior repeated separators and unreadable tails remain
            # in the token, so normalisation cannot rescue a numeric prefix.
            end = match.end(2)
            stripped = value_raw.rstrip()
            if stripped.endswith(('.', ',')) and not stripped.endswith(('..', ',,', '.,', ',.')):
                end = match.start(2) + len(stripped) - 1
                value_raw = purpose[match.start(2):end]
            if (remaining and not match[2][-1].isspace() and end == match.end(2) and
                    not remaining[0].isspace() and remaining[0] not in ';\n'):
                unit, _ = component_unit(purpose, match.start(), purpose[match.start():end])
                if unit is None:
                    attached = re.match(r'[^\s;\n]+', remaining)
                    if attached:
                        end = match.end(2) + attached.end()
                        value_raw = purpose[match.start(2):end]
            inclusion = 'unknown'
            key = (match.start(2), match.end(2))
            if key not in found or match.start() < found[key][0]:
                found[key] = (match.start(), kind, value_raw, purpose[match.start():end], inclusion)
    result = sorted(found.values())
    # All explicit money labels remain assertions even when empty/unreadable.
    # A second damaged fee/gross is not an absent fee/gross. Rate-only details
    # remain in purpose; an ordinary "на сумму ..." prose phrase is not an
    # empty gross assertion.
    assertions = sorted((m.start(), m.end(), kind, m[0]) for label, kind in labels
                        for m in re.finditer(r'(?i)(?<!\w)' + label, purpose)
                        if not (m.end() < len(purpose) and purpose[m.end()].isalpha() and
                                not m[0][-1].isspace() and m[0][-1] not in ':-'))
    for start, end_label, kind, label_raw in assertions:
        if any(x[0] <= start < x[0] + len(x[3]) for x in result):
            continue
        if kind == 'gross' and re.match(r'(?i)на\s+', label_raw):
            continue
        boundary = min([len(purpose)] + [s for s, _, _, _ in assertions if s >= end_label and s > start])
        tail = re.match(r'[^;\n]*', purpose[end_label:boundary])[0]
        if re.match(r'(?i)^[\d?*., \u00a0]*(?:%|процент[а-я]*\b|проц\b\.?|percents?\b)', tail):
            continue
        if kind == 'fee' and ':' not in label_raw and re.match(r'(?i)^(?:за|по|от|согласно)\b', tail):
            # A service description alone promises no amount. A trailing
            # price with an explicit unit is unambiguous; a bare contract
            # number remains part of an unknown assertion, never money.
            price = re.search(r'(?i)(?<![\w.,])(-?\d+(?:[.,]\d+)?)\s+(' + _COMPONENT_UNIT_PATTERN + r')\s*$', tail)
            if price and not re.search(r'\d', tail[:price.start()]):
                start_value = end_label + price.start(1)
                end_value = end_label + price.end(1)
                result.append((start, kind, purpose[start_value:end_value], purpose[start:end_value], 'unknown'))
                continue
            if not re.search(r'\d', tail):
                continue
        value_raw = tail.rstrip()
        # Units are readable independently of the adjacent unreadable value.
        unit_tail = re.search(r'\s+' + _COMPONENT_UNIT_PATTERN + r'\s*$', value_raw)
        if unit_tail:
            value_raw = value_raw[:unit_tail.start()].rstrip()
        end = end_label + len(value_raw)
        result.append((start, kind, value_raw, purpose[start:end], 'unknown'))
    result.sort()
    # Only an explicit gross/brutto assertion identifies this settlement's
    # total. "На сумму" and "общая сумма" can be unrelated reference amounts.
    gross = [x for x in result if same_settlement_gross(x)]
    fees = [x for x in result if x[1] == 'fee']
    nets = [x for x in result if x[1] == 'net']
    gross_value = component_money(gross[0][2]) if len(gross) == 1 else None
    units_agree = all(component_unit_compatible(purpose, x, currency) for x in gross + fees + nets)
    fee_value = component_money(fees[0][2]) if len(fees) == 1 else None
    if (direction == 'inflow' and amount is not None and fee_value is not None and
            not unclear_fee_relationship(purpose) and
            re.search(r'(?i)эквайринг|перечисление\s+денежных\s+средств|перевод\s+денежных\s+средств|по\s+(?:платежным\s+)?карточкам|с\s+(?:платежными\s+)?карточками|E-POS|удержан[а-я]*\s+к[ \u00a0]*оми[сc]си', purpose)):
        from decimal import localcontext
        with localcontext() as ctx:
            ctx.prec = max(28, sum(len(c[2]) for c in result) + len(amount) + 4)
            net_value = component_money(nets[0][2]) if len(nets) == 1 else None
            net_agrees = not nets or (len(nets) == 1 and net_value is not None and
                                      sg.decimal(net_value) == sg.decimal(amount))
            gross_agrees = not gross or (len(gross) == 1 and gross_value is not None and
                            sg.decimal(gross_value) == sg.decimal(amount) + sg.decimal(fee_value))
            if (units_agree and gross_agrees and
                    sg.decimal(fee_value) >= 0 and net_agrees):
                result = [(*x[:4], 'included' if x[1] in ('fee','net') or same_settlement_gross(x)
                           else 'unknown') for x in result]
    return [(*x[:4], 'unknown') if component_money(x[2]) is None else x for x in result]


def same_settlement_gross(component):
    return component[1] == 'gross' and bool(re.match(r'(?i)(?:gross|сумма брутто|брутто|brutto)(?=\s|[:\d?*.,;-]|$)', component[3]))


def unclear_fee_relationship(purpose):
    """Explicit refund/separate-service qualification defeats lexical context."""
    return bool(re.search(r'(?i)возврат|refund|возмещен|отдельн[^;\n]{0,80}(?:услуг|комис|плат)|'
                          r'коми[сc]си[яию](?: банка)?\s+(?:за|по|от|согласно)\b|коми[сc]си[^;\n]{0,80}\bза\s+(?:\S+\s+){0,3}услуг', purpose))


def component_unit(purpose, offset, mention):
    """Preserve adjacent printed units without inferring denomination semantics."""
    tail = purpose[offset + len(mention):]
    match = re.match(r'\s*(' + _COMPONENT_UNIT_PATTERN + r')', tail)
    unit = match[1] if match else None
    # Preserve unknown currency-like codes as explicit units. Only supported
    # codes can be compared, with their original spelling retained in raw data.
    currency = unit if unit and unit.upper() in ('BYR', 'BYN', 'USD', 'EUR') else None
    if (unit is not None and re.fullmatch(r'[A-Za-z]{3}', unit) and currency is None and not mention[-1:].isspace()
            and not tail[:1].isspace()):
        # An unrecognized suffix attached to a number remains part of its
        # unreadable token; it must not rescue a readable numeric prefix.
        return None, None
    return unit, currency


def component_unit_compatible(purpose, component, currency):
    """Unlabelled amounts share the operation unit; explicit units need proof.

    A printed currency code must match this statement. A generic ruble label
    does not prove BYR/BYN or a denomination and remains interpretation-only.
    """
    unit, code = component_unit(purpose, component[0], component[3])
    if unit is None:
        return True
    return code is not None and code.upper() == currency


def component_gaps(components, direction, amount, purpose=None, currency=None):
    """Address only the component interpretation, not a proven bank movement."""
    if not components or any(c[4] == 'included' for c in components):
        return []
    if all(c[1] == 'basis' for c in components):
        return []
    # Generic reference totals alone promise neither a fee nor a settlement.
    if not any(c[1] in ('fee', 'net') or same_settlement_gross(c) for c in components):
        return []
    gaps = []
    if purpose and any(not component_unit_compatible(purpose, c, currency) for c in components
                       if c[1] in ('fee', 'net') or same_settlement_gross(c)):
        gaps.append('component_unit_incompatible_or_unproven')
    if purpose and unclear_fee_relationship(purpose):
        gaps.append('fee_relationship_separate_service_or_refund')
    for kind in ('gross', 'fee'):
        matching = [c for c in components if (same_settlement_gross(c) if kind == 'gross' else c[1] == kind)]
        if kind == 'gross' and not matching:
            continue
        if len(matching) != 1:
            gaps.append('printed_' + kind + '_missing' if not matching else 'printed_' + kind + '_ambiguous')
        elif component_money(matching[0][2]) is None:
            gaps.append('printed_' + kind + '_unreadable')
    nets = [c for c in components if c[1] == 'net']
    if len(nets) > 1:
        gaps.append('printed_net_ambiguous')
    elif nets and (component_money(nets[0][2]) is None or amount is None or
                   sg.decimal(component_money(nets[0][2])) != sg.decimal(amount)):
        gaps.append('printed_net_conflicts_or_unreadable')
    if not gaps:
        gaps.append('withheld_fee_context_or_same_settlement_equation_not_established')
    if direction != 'inflow':
        gaps.append('not_an_incoming_net_settlement')
    return gaps


def settlement_view(components, direction, amount, standalone_fee=False):
    """Supported net and withheld fee; optional printed same-settlement gross."""
    included = [c for c in components if c[4] == 'included']
    verified = direction == 'inflow' and any(c[1] == 'fee' for c in included)
    return {'status': 'not_applicable' if standalone_fee or direction == 'outflow' or not components else
                      ('verified_equation' if any(same_settlement_gross(c) for c in included)
                       else 'verified_withheld_fee') if verified else 'unknown',
            'gross': next((component_money(c[2]) for c in included if c[1] == 'gross'), None) if verified else None,
            'commission': next((component_money(c[2]) for c in included if c[1] == 'fee'), None) if verified else None,
            'net': amount if verified else None}


def cash_acceptance_fee(purpose, direction):
    return (direction == 'outflow' and
            bool(re.search(r'(?i)(?:комис[^\n]*вознагр|вознагр)[^\n]*за\s+при[её]м\s+налич', purpose)))


def fee_basis(purpose):
    """Calculation base is a detail of a fee movement, never another payment."""
    match = re.search(r'(?i)с\s+суммы\s+оборотов\s+', purpose)
    if not match:
        return []
    # Reuse the complete assertion-token parser, including malformed tails,
    # empty values and independent readable units; never rescue a prefix.
    synthetic = 'net ' + purpose[match.end():]
    values = amount_components(synthetic, 'unknown')
    if not values or values[0][0] != 0 or values[0][1] != 'net':
        return [(match.start(), 'basis', '', match[0], 'unknown')]
    value = values[0]
    end = match.end() + len(value[3]) - len('net ')
    return [(match.start(), 'basis', value[2], purpose[match.start():end], 'unknown')]


# Source-state grammar, not a blacklist of words in legal names. Predicates
# about the name/its parts may occur anywhere, including across continuations.
# Inflected subjects + polarity/degree predicates, rather than enumerated
# complete annotations. These components also cover wrapped continuations.
_RU_END = r'(?:о|а|ы|ое|ая|ый|ые|ую|ой|ым|ыми|ых|ого|ому|ом)?'
_RU_PARTICIPLE_END = r'(?:о|а|ы|н(?:ое|ая|ый|ые|ую|ой|ым|ыми|ых|ого|ому|ом))?'
_RU_NAME = r'(?:наименовани(?:е|я|ю|ем|и)|имя|имени|именем|названи(?:е|я|ю|ем|и))'
_RU_FULL_NAME = r'(?:(?:полн(?:ое|ого|ому|ым|ом)|вс[её])\s+)?'+_RU_NAME
_RU_PART = r'(?:часть|части|частей|фрагмент(?:ы|а|ов)?|остат(?:ок|ка)|окончани[ея]|текст|букв[аы]|буквы|букв|символ(?:ы|а|ов)?)'
_RU_PARTIAL = r'(?:частично|неполностью|не\s+полностью|не\s+до\s+конца)'
_RU_READ = r'(?:(?:распознан|прочитан|разобран)'+_RU_PARTICIPLE_END+r'|(?:читаем|разборчив)'+_RU_END+r'|(?:распозна|чита)(?:[её]тся|ются))'
_RU_STATE = (r'(?:(?:неразборчив|нечитаем|неизвестн)'+_RU_END+r'|неизвестен|'
             r'(?:нераспознан|непрочитан|неразобран)'+_RU_PARTICIPLE_END+r'|'
             r'не\s+(?:(?:полностью|до\s+конца)\s+)?'+_RU_READ+r'|'
             r'не\s+чита(?:ется|ются)|не\s+разобрать|не\s+удалось\s+(?:прочитать|распознать|разобрать)|'
             r'не\s+(?:указан|заполнен|известен|известн)'+_RU_END+r'|'
             r'(?:отсутствует|отсутствуют|(?:утрачен|поврежд[её]н)'+_RU_PARTICIPLE_END+r')|'
             +_RU_PARTIAL+r'\s+'+_RU_READ+r'|'+_RU_READ+r'\s+'+_RU_PARTIAL+r')')
_EN_NAME = r'(?:(?:full|complete|partial|counterparty|beneficiary)\s+)?name'
_EN_PART = r'(?:parts?|portions?|fragments?|remainder|ending|text|letters?|characters?)(?:\s+of\s+(?:the\s+)?'+_EN_NAME+r')?'
_EN_READ = r'(?:known|provided|readable|legible|specified|recogni[sz]ed|read|deciphered)'
_EN_PARTIAL = r'(?:(?:only\s+)?(?:partially|partly|incompletely))'
_EN_STATE = (r'(?:unreadable|illegible|unknown|missing|absent|damaged|incomplete|unrecogni[sz]ed|'
             r'not\s+(?:(?:fully|completely|entirely)\s+)?'+_EN_READ+r'|'
             +_EN_PARTIAL+r'\s+'+_EN_READ+r'|'+_EN_READ+r'\s+'+_EN_PARTIAL+r')')
_NAME_STATE = re.compile(
    r'\b(?:'+_RU_PART+r'(?:\s+'+_RU_FULL_NAME+r')?|'+_RU_FULL_NAME+r')'
    r'\s*(?::|[-–—])?\s*(?:(?:частично|полностью|неполностью)\s+)?'+_RU_STATE+r'\b|'
    r'\b'+_RU_STATE+r'\s+'+_RU_PART+r'(?:\s+'+_RU_FULL_NAME+r')?\b|'
    r'\b(?:'+_EN_PART+r'|'+_EN_NAME+r'(?:[’\']s\s+'+_EN_PART+r')?)\s*(?::|[-–—])?\s*(?:(?:is|are|was|were)\s+)?'
    r'(?:(?:partially|partly|completely)\s+)?'+_EN_STATE+r'\b|'
    r'\b'+_EN_STATE+r'\s+'+_EN_PART+r'\b', re.I)
_NAME_MARKER = re.compile(
    r'(?:'+_RU_STATE+r'|(?:частично|полностью)\s+'+_RU_STATE+r'|'
    r'(?:(?:partially|partly|completely)\s+)?'+_EN_STATE+r'|'
    r'нет\s+данных|не\s+заполнено|null|none|not\s+applicable|n/a|н/д)', re.I)


def explicit_name_unknown(value):
    """Detect explicit source annotations without treating Unknown Books as one."""
    compact = ' '.join(value.split())
    if _NAME_STATE.search(compact):
        return True
    # A status alone is explicit on its own line or inside annotation brackets.
    segments = value.split('\n') + re.findall(r'[\[({]([^\[\](){}]*)[\])}]', value)
    if any(_NAME_MARKER.fullmatch(' '.join(x.strip().strip('[](){}').split())) for x in segments):
        return True
    # Russian impersonal readings are annotations even without brackets;
    # adjective words (e.g. Нечитаемые книги) remain ordinary name text.
    return bool(re.search(r'\b(?:неразборчиво|нечитаемо|не\s+читается|нет\s+данных)\b', compact, re.I))


def readable_name(value):
    """Readable field anatomy, not authentication of an arbitrary legal name."""
    if not isinstance(value, str) or not value.strip():
        return False
    if METADATA_LABEL.search(value) or IDENTITY_MARKER.search(value) and ':' in value:
        return False
    if explicit_name_unknown(value):
        return False
    for line in value.split('\n'):
        if not line.strip() or not any(unicodedata.category(c).startswith('L') for c in line):
            return False
        for c in line:
            category = unicodedata.category(c)
            if not (category[0] in 'LNM' or c in ' .,-–—’/\'"«»()[]{}&:;№'):
                return False
    return True


def readable_unp(value):
    # Belarus: organizations/IP use nine digits; physical persons two uppercase
    # Latin letters and seven digits. Syntax does not prove registration/checksum.
    # Preserve the existing all-zero numeric hold; never fold case/transliterate.
    return (isinstance(value, str) and value != '000000000' and
            bool(re.fullmatch(r'(?:[0-9]{9}|[A-Z]{2}[0-9]{7})', value)))


def observed_unp_matches(raw, value):
    """Normalization may remove printed ASCII spaces, never control/damage."""
    return (readable_unp(value) and isinstance(raw, str) and
            bool(re.fullmatch(r'[A-Z0-9 ]+', raw)) and compact(raw) == value)


METADATA_LABEL = re.compile(
    r'Наименование +(?:корреспондента|банка|плательщика|получателя|за кого) *:'
    r'|УНП +(?:плательщика|получателя|за кого) *:'
    r'|Очередность(?: +платежа)? *:')


def _damaged_keyword(keyword):
    # Exactly one replacement glyph within an established structural keyword.
    return '(?:' + '|'.join(re.escape(keyword[:i]) + r'[?*\ufffd]' +
                           re.escape(keyword[i+1:]) for i in range(len(keyword))) + ')'


_DAMAGED_UNP = _damaged_keyword('УНП')
_DAMAGED_NAME = _damaged_keyword('Наименование')
IDENTITY_MARKER = re.compile(r'(?i)(?<!\w)(?:УНП|У[?*\ufffd]П|Наименование|Наименован[?*\ufffd]е)(?!\w)')
DAMAGED_IDENTITY_LABEL = re.compile(
    _DAMAGED_UNP + r' +(?:плательщика|получателя|за кого) *:'
    r'|' + _DAMAGED_NAME + r' +(?:корреспондента|банка|плательщика|получателя|за кого) *:')


IDENTITY_FIELD_LABELS = {
    'Наименование корреспондента:': 'counterparty_full_name',
    'УНП плательщика:': 'payer_unp',
    'УНП получателя:': 'payee_unp',
}


def metadata_fields(detail, evidence):
    """Inventory every assertion, including inline repeats and damaged labels.

    Keep full raw lines in private QA. Only supported label/value spans leave
    purpose; independently printed prefix prose remains verbatim purpose.
    """
    assertions, purpose, gaps = [], [], []
    active_name = None
    for offset, text in enumerate(detail):
        damaged = list(DAMAGED_IDENTITY_LABEL.finditer(text))
        matches = sorted(list(METADATA_LABEL.finditer(text)) + damaged, key=lambda m: m.start())
        if damaged:
            gaps.append('unsupported_or_damaged_identity_label')
        markers = list(IDENTITY_MARKER.finditer(text))
        name_continuation = (active_name is not None and len(text) - len(text.lstrip()) >= 25 and
                             not matches and (not markers or
                             (all(m[0].casefold() == 'наименование' for m in markers) and
                              explicit_name_unknown(text))))
        if name_continuation:
            active_name['value_raw'] += '\n' + text
            active_name['raw_span'] += '\n' + text
            active_name['line_offsets'].append(offset)
            active_name['overlapping'] |= bool(evidence[offset].get('overlaps'))
            active_name['value_overlapping'] |= bool(evidence[offset].get('overlaps'))
            continue
        if any(not any(m.start() <= marker.start() < m.end() for m in matches) for marker in markers):
            gaps.append('unsupported_or_damaged_identity_label')
        if matches:
            prefix = text[:matches[0].start()]
            if prefix.strip():
                purpose.append(prefix.rstrip())
            for n, match in enumerate(matches):
                label = re.sub(r'\s+', ' ', match[0][:-1]).strip() + ':'
                end = matches[n+1].start() if n+1 < len(matches) else len(text)
                value = text[match.end():end].strip(' ')
                assertion = {'label': label, 'value_raw': value, 'line_offsets': [offset],
                             'label_raw': match[0], 'raw_span': text[match.start():end],
                             'overlapping': bool(field_overlaps(evidence[offset], match.start(), end)),
                             'label_overlapping': bool(field_overlaps(evidence[offset], match.start(), match.end())),
                             'value_overlapping': bool(field_overlaps(evidence[offset], match.end(), end))}
                assertions.append(assertion)
                active_name = assertion if label.startswith('Наименование') else None
            continue
        active_name = None
        purpose.append(text.rstrip())
    metadata = {}
    for assertion in assertions:
        metadata.setdefault(assertion['label'], []).append(assertion['value_raw'])
        if assertion['overlapping']:
            gaps.append('overlapping_metadata_assertion')
    for label, values in metadata.items():
        if label in ('Наименование плательщика:', 'Наименование получателя:', 'Наименование за кого:'):
            gaps.append('unsupported_role_name_anatomy:' + label)
        if label.startswith('УНП') and any(
                not re.fullmatch(r'[0-9 ]*', value) and
                not observed_unp_matches(value, compact(value)) for value in values):
            gaps.append('damaged_metadata_unp:' + label)
        if (label.startswith(('Наименование', 'УНП')) and len(values) != 1):
            gaps.append('duplicate_metadata_assertion:' + label)
    return metadata, assertions, purpose, gaps


def geometry(pdf):
    import pdfplumber
    pages = []
    with pdfplumber.open(pdf) as reader:
        for page in reader.pages:
            main = [c for c in page.chars if c['size'] > 6.5]
            decoration = [c for c in page.chars if c['size'] <= 6.5]
            if not main or not any(c['text'] == ' ' for c in main):
                raise ValueError('unobserved layout: no business monospaced text')
            nominal_width = statistics.median(c['width'] for c in main if c['text'] == ' ')
            origin = min(c['x0'] for c in main if c['text'] != ' ')
            groups = []
            for char in sorted(main, key=lambda c: (c['top'], c['x0'])):
                if not groups or abs(groups[-1][0] - char['top']) > 1:
                    groups.append((char['top'], []))
                groups[-1][1].append(char)
            # PDF nominal glyph width is not necessarily the printed advance.
            # At the foot of archived pages a 4.176 width with 4.2 advances
            # accumulates a full-cell drift. Prove pitch from adjacent glyphs.
            advances = [right['x0'] - left['x0'] for _, chars in groups
                        for left, right in zip(sorted(chars, key=lambda c: c['x0']),
                                               sorted(chars, key=lambda c: c['x0'])[1:])
                        if .8 * nominal_width < right['x0'] - left['x0'] < 1.2 * nominal_width]
            if not advances:
                raise ValueError('unobserved layout: monospaced advance unavailable')
            step = statistics.median(advances)
            lines = []
            for top, chars in groups:
                cells, overlaps = {}, []
                for char in chars:
                    index = round((char['x0'] - origin) / step)
                    if index in cells and cells[index] != char['text']:
                        overlaps.append(index)
                    cells[index] = char['text']
                lines.append({
                    'text': ''.join(cells.get(i, ' ') for i in range(max(cells) + 1)).rstrip(),
                    'raw_text': ''.join(c['text'] for c in sorted(chars, key=lambda c: c['x0'])).rstrip(),
                    'top': top, 'overlaps': overlaps,
                    'glyphs': [{'cell': round((c['x0'] - origin) / step), 'text': c['text'],
                                'x0': c['x0'], 'x1': c['x1'], 'top': c['top']} for c in sorted(chars, key=lambda c: c['x0'])]})
            pages.append({'page': page.page_number, 'origin': origin, 'step': step,
                          'nominal_width': nominal_width, 'step_source': 'observed_adjacent_glyph_advances',
                          'lines': lines, 'business_chars': len(main),
                          'decoration_chars': len(decoration),
                          'decoration_text': ''.join(c['text'] for c in decoration),
                          'business_fonts': sorted(set(c['fontname'] for c in main))})
    return {'pages': pages}


def compact(value):
    return re.sub(r'\s', '', value)


def iso(raw):
    text = compact(raw)
    try:
        return datetime.strptime(text, '%d/%m/%Y').date().isoformat() if DATE.fullmatch(text) else None
    except ValueError:
        return None


def money(raw):
    return sg.normalize_decimal(raw) if raw.strip() else None


def field_slice(line, start, end):
    """Read original glyph spelling by column, rather than re-slicing raw text."""
    if 'glyphs' in line:
        seen = set()
        text = []
        for g in line['glyphs']:
            if g['cell'] < start or (end is not None and g['cell'] >= end):
                continue
            # Equal overlays occupy one proven cell even when PDF glyph
            # coordinates differ slightly. Keep every glyph in private QA;
            # genuine repeated digits in adjacent cells remain separate.
            key = (g['cell'], g['text'])
            if key not in seen:
                text.append(g['text'])
                seen.add(key)
        return ''.join(text)
    return line['text'][start:end]


def field_overlaps(line, start, end):
    return [i for i in line.get('overlaps', []) if i >= start and (end is None or i < end)]


def readable_bank_code(value):
    return isinstance(value, str) and identifiers.recognize(value, 'bank')['format_status'] == 'recognized'


def readable_account_number(value):
    return isinstance(value, str) and identifiers.recognize(value, 'account')['format_status'] == 'recognized'


def component_money(raw):
    """Unnormalizable purpose details remain raw/unknown, never abort a row."""
    try:
        return money(raw)
    except ValueError:
        return None


def obs(field, raw, normalized=None, status=None):
    return {'field': field, 'raw': raw, 'normalized': normalized,
            'status': status or ('observed' if raw is not None and str(raw).strip() else 'absent')}


def unknown(label, value):
    return {'label': label, 'value': value, 'status': 'unknown'}


def entry(alias, typ, properties, observations=None, unrecognized=None):
    return {'alias': alias, 'type': typ, 'properties': properties,
            'observations': observations or [], 'unrecognized_fields': unrecognized or []}


def source_header_fields(lines):
    """Printed header facts for the reviewed legacy/modern profiles only.

    Keep distinct labelled names and literal bank codes; absence is null.
    No owner identity, identifier correction or transaction role is inferred.
    """
    original_lines = list(lines)
    lines = [x.strip() for x in original_lines]
    opening = next((i for i, x in enumerate(lines) if x.startswith('Входящее сальдо')), len(lines))
    lines = lines[:opening]
    def unique(values):
        return values[0] if len(values) == 1 else None
    account = unique([x for x in lines if re.match(r'^[CС]чет клиента\s', x)])
    previous = unique([x.split('Дата пред. опер.', 1)[1].strip()
                       for x in lines if x.startswith('Дата пред. опер.')])
    date = re.fullmatch(r'[0-9]{2}/[0-9]{2}/[0-9]{4}', previous or '')
    executors = [m[1].strip() for x in lines
                 if (m := re.match(r'^(?:Ответственный исполнитель|Исполнитель)\s+(.*)$', x))]
    bank_name = bank_code = bank_label = None
    bank_lines = [(i, m) for i, x in enumerate(lines)
                  if (m := re.match(r'^(.*?)\s*\b(БИК|МФО)\s+(.*?)\s*$', x))
                  and not x.startswith('Номер счета с')]
    if len(bank_lines) == 1:
        i, m = bank_lines[0]
        bank_label, bank_code = m[2], m[3] or None
        bank_name = m[1].strip() or next((x for x in reversed(lines[:i]) if x), None)
    names = []
    for i, line in enumerate(lines):
        m = re.match(r'^(Наименование клиента|Наименование)\s+(.*)$', line)
        if not m:
            continue
        # Only an indented continuation before the next named header boundary.
        value = m[2].strip()
        for j in range(i + 1, len(lines)):
            tail = lines[j]
            if (not tail or not original_lines[j][:1].isspace() or
                re.match(r'^(Наименование|Входящее|Дата пред\.|[CС]чет|Номер счета|Исполнитель|Ответственный|ВЫПИСКА|БИК|МФО)\b', tail) or
                re.fullmatch(r'[-=*\s|]{3,}', tail)):
                break
            value += '\n' + tail
        names.append({'label': m[1], 'raw': value})
    result = {'own_bank_name_raw': bank_name, 'own_bank_code_raw': bank_code,
              'own_bank_code_label': bank_label, 'executor_raw': unique(executors),
              'previous_operation_marker_raw': previous,
              'previous_operation_date_raw': date[0] if date else None,
              'own_account_line_raw': account, 'own_organization_name_assertions': names}
    future = [x for x in lines if x.startswith('Номер счета с')]
    if len(future) == 1:
        result['future_account_line_raw'] = future[0]
    return result


def materialize_source_header(statement, entities, relations, lines, index):
    """Apply the same source header contract to each supported assembler."""
    header = source_header_fields(lines)
    props = statement['properties']
    props.update(header_fields=header, executor_raw=header['executor_raw'],
                 previous_operation_date=iso(header['previous_operation_date_raw'] or ''),
                 first_statement_marker=(header['previous_operation_marker_raw']
                   if header['previous_operation_date_raw'] is None and
                   'выписка' in (header['previous_operation_marker_raw'] or '').lower() else None))
    for key, value in header.items():
        if key != 'own_organization_name_assertions':
            statement['observations'].append(obs('header_' + key, value, value))
    account = next(x for x in entities if x['alias'] == props['account_id'])
    account['observations'].append(obs('number_raw', header['own_account_line_raw'], account['properties']['number_raw']))
    holder_alias, bank_alias = 'holder:' + str(index), 'bank:' + str(index)
    names = [x['raw'] for x in header['own_organization_name_assertions'] if x['label'] == 'Наименование']
    name = names[0] if len(names) == 1 else None
    holder = next((x for x in entities if x['alias'] == holder_alias), None)
    if holder is None:
        holder = entry(holder_alias, 'Party', {'name_raw': name, 'unp_raw': None,
                       'role_raw': 'account holder', 'identity_status': 'observed' if readable_name(name) else 'unknown'})
        entities.append(holder)
    holder['observations'].append(obs('name_raw', name, name, 'observed' if readable_name(name) else 'unknown' if name else 'absent'))
    for assertion in header['own_organization_name_assertions']:
        holder['observations'].append(obs('header_organization_' + assertion['label'], assertion['raw'], assertion['raw']))
    bank = next((x for x in entities if x['alias'] == bank_alias), None)
    if bank is None:
        code = header['own_bank_code_raw']
        bank = entry(bank_alias, 'Bank', {'name_raw': header['own_bank_name_raw'], 'code_raw': code,
                     'code_system_raw': header['own_bank_code_label'], 'valid_from': None, 'valid_to': None,
                     'identity_status': 'observed' if identifiers.usable(identifiers.recognize(code, 'bank')) else 'unknown',
                     'branch_raw': None, 'city_raw': None, 'country_raw': None})
        entities.append(bank)
    bank_identifier = identifiers.recognize(bank['properties']['code_raw'], 'bank')
    bank['properties'].update(identifier=bank_identifier,
                              identity_status='observed' if identifiers.usable(bank_identifier) else 'unknown')
    for key, field in [('name_raw', 'header_bank_name_raw'), ('code_raw', 'header_bank_code_raw'),
                       ('code_system_raw', 'header_bank_code_label')]:
        value = bank['properties'][key]
        bank['observations'].append(obs(field, value, value))
    code = bank['properties']['code_raw']
    bank['observations'].append(obs('code_raw', code, bank_identifier['normalized'] if identifiers.usable(bank_identifier) else None,
                                    'observed' if identifiers.usable(bank_identifier) else 'unknown' if code else 'absent'))
    bank['observations'].append(obs('name_raw', bank['properties']['name_raw'], bank['properties']['name_raw']))
    for typ, target in [('OWNED_BY', holder_alias), ('SERVICED_BY', bank_alias)]:
        edge = {'type': typ, 'from': props['account_id'], 'to': target}
        if edge not in relations:
            relations.append(edge)
    return header


SIGNATURE_PREFIXES = ('---Электронно', 'ИД эл.', 'Владелец ключа', 'Подписано:')


def footer_contract(doc):
    """Identify the final control block, not words occurring in purpose."""
    columns, _ = column_layout(doc)
    debit_start, debit_end = columns['printed_debit']
    credit_start, date_start = columns['printed_credit']
    lines = [(page['page'], offset, line) for page in doc['pages']
             for offset, line in enumerate(page['lines'])]
    closing = [(n, page, offset, line) for n, (page, offset, line) in enumerate(lines)
               if re.fullmatch(r'Исходящее сальдо\s*[\d\s.,?*+\-]*', line['raw_text'].strip())]
    if not closing:
        raise ValueError('unobserved closing control block')
    n, page, offset, line = closing[-1]
    # A closing label inside a payment is not a statement control. Require
    # final-page terminal anatomy and an adjacent turnover/rule boundary.
    terminal = all(not item['text'].strip() or re.fullmatch(r'[-=*\s|]+', item['text']) or
                   item['raw_text'].strip().startswith(SIGNATURE_PREFIXES)
                   for _, _, item in lines[n + 1:])
    previous = lines[n - 1][2]['text'] if n else ''
    boundary = bool(re.fullmatch(r'[-=*\s|]{30,}', previous) or
                    previous[:debit_start + 1].strip(' |') == 'ОБОРОТЫ')
    _, headings = header_contract(doc)
    heading_locations = {(h['page'], o) for h in headings for o in h['heading_offsets']}
    heading_end = max((i for i, (p, o, _) in enumerate(lines[:n]) if (p, o) in heading_locations), default=-1)
    no_rows = heading_end >= 0 and all(not item['text'].strip() or re.fullmatch(r'[-=*\s|]+', item['text'])
                                      for _, _, item in lines[heading_end + 1:n])
    if page != doc['pages'][-1]['page'] or not terminal or not (boundary or no_rows):
        raise ValueError('unproven closing control anatomy')
    result = {'closing': (page, offset), 'closing_line': line, 'turnover': None, 'turnover_line': None,
              'trailers': [(p, o) for p, o, x in lines[n + 1:]
                           if x['raw_text'].strip().startswith(SIGNATURE_PREFIXES)]}
    # Turnover has its own fixed-width control anatomy: exact label field,
    # monetary columns, no date suffix, then the closing control and rules.
    for prior in range(n - 1, -1, -1):
        p, o, candidate = lines[prior]
        text = candidate['text']
        if not text.strip() or re.fullmatch(r'[-=*\s|]+', text):
            continue
        if text[:debit_start + 1].strip(' |') == 'ОБОРОТЫ' and not text[date_start:].strip():
            result.update(turnover=(p, o), turnover_line=candidate)
        break
    return result


def parse_statement(doc, index, dataset_id):
    columns, bars_expected = column_layout(doc)
    bank_start, bank_end = columns['bank_code']
    account_start, account_end = columns['account']
    debit_start, debit_end = columns['printed_debit']
    credit_start, date_start = columns['printed_credit']
    columns_known, column_evidence = header_contract(doc)
    controls = footer_contract(doc)
    lines = []
    local_columns_known = False
    contracts = {(x['page'], x['line_offset']): x['recognized'] for x in column_evidence}
    heading_lines = {(x['page'], offset)
                     for x in column_evidence for offset in x['heading_offsets']}
    page_decoration = set()
    for page in doc['pages']:
        starts = [x['line_offset'] for x in column_evidence if x['page'] == page['page']]
        if starts:
            first_heading = min(starts)
            prefix = page['lines'][:first_heading]
            # The entire structurally repeated statement header is decoration.
            # Individual words in business detail cannot trigger this rule.
            if (any('ВЫПИСКА ИЗ' in x['text'] for x in prefix) and
                    any(re.match(r'^[CС]чет клиента', x['text']) for x in prefix) and
                    any(x['text'].startswith('Входящее сальдо') for x in prefix)):
                page_decoration.update((page['page'], o) for o in range(first_heading))
        for offset, line in enumerate(page['lines']):
            local_columns_known = contracts.get((page['page'], offset), local_columns_known)
            lines.append((page['page'], {**line, '_control_footer':
                          (page['page'], offset) in (controls['turnover'], controls['closing']),
                          '_page_decoration': (page['page'], offset) in page_decoration or
                          (page['page'], offset) in controls['trailers']},
                          local_columns_known, (page['page'], offset) in heading_lines))
    first = [line['raw_text'] for _, line, _, _ in lines[:40]]
    heading = next(x.strip() for x in first if 'ВЫПИСКА ИЗ' in x)
    period = re.search(r'ПЕРИОД С\s+([\d/ ]+?)\s+ПО\s+([\d/ ]+?)\s+ПО\s+(\S+)', heading)
    if not period:
        raise ValueError('unobserved period heading')
    begin, end, currency = iso(period[1]), iso(period[2]), period[3]
    account_line = next(x for x in first if re.match(r'^[CС]чет клиента', x))
    account = re.search(r'клиента\s+(\S+)\s+(\S+)', account_line)[1]
    holder = next(x for x in first if re.match(r'^Наименование\s', x)).split('Наименование', 1)[1].strip()
    bank_line = next(x for x in first if 'Филиал' in x and 'БИК' in x)
    previous = next(x for x in first if x.startswith('Дата пред. опер.')).split('Дата пред. опер.', 1)[1].strip()
    first_marker = previous if 'выписка' in previous else None
    executor = next(x for x in first if x.lstrip().startswith('Ответственный исполнитель')).split('исполнитель', 1)[1].strip()
    opening_line = next(x for x in first if x.startswith('Входящее сальдо'))
    opening_raw = opening_line.split('сальдо', 1)[1].strip()
    closing_line = controls['closing_line']['raw_text']
    closing_raw = closing_line.split('сальдо', 1)[1].strip()
    turnover = controls['turnover_line']
    # The turnover footer prints a separator at cell39; row bodies do not.
    debit_raw, credit_raw = (field_slice(turnover, debit_start + 1, debit_end), field_slice(turnover, credit_start, date_start)) if turnover else ('', '')
    alias = 'statement:' + str(index)
    props = {'account_id': 'account:' + str(index), 'currency_context_id': 'currency:' + str(index),
             'statement_date': None, 'period_start': begin, 'period_end': end,
             'opening': money(opening_raw),
             'closing': component_money(closing_raw) if not field_overlaps(controls['closing_line'], len('Исходящее сальдо'), None) else None,
             'printed_debit_total': component_money(debit_raw) if columns_known and turnover and not field_overlaps(turnover, debit_start + 1, debit_end) else None,
             'printed_credit_total': component_money(credit_raw) if columns_known and turnover and not field_overlaps(turnover, credit_start, date_start) else None,
             'debit_is': 'outflow' if columns_known else 'unknown', 'opening_sign_rule': 'signed',
             'rows_complete': True, 'expected_operations': None, 'selection': 'include',
             'selection_evidence': [], 'year': int(begin[:4]), 'heading_raw': heading,
             'executor_raw': executor, 'previous_operation_date': iso(previous),
             'first_statement_marker': first_marker, 'account_number_raw': account,
             'first_account_evidence': [], 'opening_origin': 'printed' if opening_raw else 'unknown'}
    statements = {'alias': alias, 'properties': props, 'observations': [
        obs('opening', opening_raw, props['opening']), obs('closing', closing_raw, props['closing'], 'unknown' if closing_raw and props['closing'] is None else None),
        obs('printed_debit_total', debit_raw, props['printed_debit_total'], 'unknown' if debit_raw.strip() and props['printed_debit_total'] is None else None),
        obs('printed_credit_total', credit_raw, props['printed_credit_total'], 'unknown' if credit_raw.strip() and props['printed_credit_total'] is None else None),
        obs('statement_date', None, None, 'unknown'),
        obs('debit_is', 'Дебет / Кредит; printed opening + credit - debit = closing', props['debit_is'],
            'inferred' if columns_known else 'unknown')],
        'unrecognized_fields': [unknown('bank_header_raw', bank_line), unknown('account_header_raw', account_line)],
        'operations': []}
    entities = [
        entry('account:' + str(index), 'Account', {'number_raw': account, 'format': 'historical', 'identity_status': 'observed',
              'iban_effective_date': None, 'iban_raw': None, 'bic_raw': None}),
        entry('currency:' + str(index), 'CurrencyContext', {'printed_currency': currency, 'unit_raw': None,
              'semantics_status': 'unknown', 'epoch': None, 'conversion_factor': None, 'evidence_refs': []},
              [obs('printed_currency', currency, currency), obs('unit_raw', None, None, 'unknown')]),
        entry('holder:' + str(index), 'Party', {'name_raw': holder, 'unp_raw': None, 'role_raw': 'account holder', 'identity_status': 'observed'}),
        entry('bank:' + str(index), 'Bank', {'name_raw': bank_line.split('БИК')[0].strip(),
              'code_raw': bank_line.split('БИК', 1)[1].strip() or None, 'code_system_raw': 'БИК',
              'valid_from': None, 'valid_to': None, 'identity_status': 'observed',
              'branch_raw': None, 'city_raw': None, 'country_raw': None}, [obs('header_raw', bank_line, None)])]
    relations = [{'type': 'OWNED_BY', 'from': 'account:' + str(index), 'to': 'holder:' + str(index)},
                 {'type': 'SERVICED_BY', 'from': 'account:' + str(index), 'to': 'bank:' + str(index)}]
    for line in first:
        if line.startswith('Номер счета с'):
            m = re.search(r'с\s+(\d{2}\.\d{2}\.\d{4})\s+в формате IBAN:\s*(.*?)\s*,\s*БИК\s*(.*)', line)
            if m:
                entities[0]['properties'].update({'iban_effective_date': datetime.strptime(m[1], '%d.%m.%Y').date().isoformat(),
                                                   'iban_raw': m[2] or None, 'bic_raw': m[3] or None})
                entities[0]['observations'] += [obs('iban_raw', m[2], m[2] or None), obs('bic_raw', m[3], m[3] or None)]
            else:
                entities[0]['unrecognized_fields'].append(unknown('future_account_raw', line))
        elif line.strip() == 'БИК':
            entities[0]['observations'].append(obs('bic_raw', '', None))
    blocks, current, residual = [], None, []
    in_body = False
    for page, line, row_columns_known, heading_line in lines:
        fixed, raw = line['text'], line['raw_text']
        stripped = raw.strip()
        # Row boundaries are independent of successful date/suffix decoding.
        # Every boundary also needs the observed padding around the
        # operation code. A long numeric reference can fill every numeric slice,
        # including the money cells, without being a separately printed row.
        # A date-shaped purpose reference cannot supply its own row evidence.
        # Executor is nullable/damaged in genuine rows. Document and padded
        # operation code plus independent aligned movement/date prove a boundary.
        identity = compact(fixed[6:11]).isdigit()
        cells = (fixed[debit_start:debit_end], fixed[credit_start:date_start])
        money_shape = (any(cell.strip() for cell in cells) and
                       all(re.fullmatch(r'[\d\s.,?*\-]*', cell) for cell in cells))
        tail_shape = bool(DATE_SUFFIX.fullmatch(fixed[date_start:]) or (bars_expected == WIDE_BARS and DATE.fullmatch(compact(fixed[date_start:]))))
        code_separated = bool(re.fullmatch(r' {1,2}\d{1,2}  ', fixed[6:11]))
        # A damaged operation-code cell has an independent boundary only with
        # document, correspondent columns, money and date all aligned. Numeric
        # purpose references without this anatomy remain business detail.
        damaged_code = (not compact(fixed[6:11]).isdigit() and
                        bool(re.fullmatch(r' [0-9 ?*\[\]]{3} ', fixed[6:11])))
        fallback_boundary = (damaged_code and compact(fixed[:6]).isdigit() and
                             identifiers.boundary_candidate(field_slice(line, bank_start, bank_end), 'bank') and
                             identifiers.boundary_candidate(field_slice(line, account_start, account_end), 'account') and
                             money_shape and tail_shape)
        if identity and code_separated and (tail_shape or money_shape) or fallback_boundary:
            if current:
                blocks.append(current)
            current = {'page': page, 'last_page': page, 'line': line, 'identifier_continuations': {}, 'identifier_continuation_evidence': [], 'identifier_overlap_fields': set(), 'detail': [], 'detail_evidence': [], 'gaps': [],
                       'columns_known': row_columns_known}
            in_body = True
            continue
        if line.get('_page_decoration'):
            continue
        if line.get('_control_footer'):
            if current:
                blocks.append(current); current = None
            in_body = False
            continue
        if (not stripped or re.fullmatch(r'[-=*\s|]{30,}', stripped) or
                re.fullmatch(r'\*{4} Ответственный исполнитель\s+\S+\s+\*{10,}', stripped)):
            continue
        if heading_line:
            in_body = True
            continue
        if current and not current['detail'] and page == current['page']:
            fragments={key: field_slice(line, *columns[key]) for key in ('bank_code','account')}
            outside=''.join(field_slice(line,*columns[key]) for key in columns if key not in fragments)
            # Only immediate, physically same-column fragments; no purpose/name
            # text, monetary/date/document cells or invented final characters.
            if (not outside.strip() and any(v.strip() for v in fragments.values()) and
                    all(not v.strip() or re.fullmatch(r'[A-Z0-9 ?*\ufffd]+',v) for v in fragments.values()) and
                    0 <= line.get('top',0)-current['line'].get('top',0) <= 12):
                for key,value in fragments.items():
                    if value.strip():
                        current['identifier_continuations'].setdefault(key,[]).append(value)
                        if field_overlaps(line, *columns[key]):
                            current['identifier_overlap_fields'].add(key)
                current['identifier_continuation_evidence'].append({'page': page, **line})
                continue
        if current:
            if page != current['last_page']:
                proven = any(x.get('from_page') == current['last_page'] and x.get('to_page') == page and
                             x.get('decision') == 'same_operation' and x.get('evidence')
                             for x in doc.get('reviewed_continuations', []))
                if not proven:
                    current['gaps'].append('continuation boundary unreviewed: ' + str(current['last_page']) + ' -> ' + str(page))
                current['last_page'] = page
            current['detail'].append(raw.rstrip())
            current['detail_evidence'].append({'page': page, 'raw_text': raw, 'cells': fixed,
                                               'overlaps': line.get('overlaps', [])})
            if line.get('overlaps'):
                current['gaps'].append('overlapping detail glyphs on page ' + str(page))
        elif in_body:
            residual.append({'page': page, 'text': raw})
    if current:
        blocks.append(current)
    partial, row_evidence = [], []
    for ordinal, block in enumerate(blocks, 1):
        fixed, raw = block['line']['text'], block['line']['raw_text']
        row_alias = alias + ':row:' + str(ordinal)
        raw_fields = {key: field_slice(block['line'], start, end) for key, (start, end) in columns.items()}
        for key,parts in block['identifier_continuations'].items():
            raw_fields[key]+='\n'+'\n'.join(parts)
        fields = dict(raw_fields)
        tail = DATE_SUFFIX.fullmatch(fixed[date_start:])
        raw_tail = DATE_SUFFIX.fullmatch(fields['date'] if 'glyphs' in block['line'] else raw[date_start:])
        wide_date_only = (bars_expected == WIDE_BARS and DATE.fullmatch(compact(raw_fields['date']))
                          and not field_overlaps(block['line'], date_start, None))
        tail_known = bool(raw_tail and not field_overlaps(block['line'], date_start, None) and
                          ('glyphs' in block['line'] or
                           (tail and compact(tail[1]) == compact(raw_tail[1]) and tail[2] == raw_tail[2])))
        fields['date'], fields['suffix'] = (raw_tail[1], raw_tail[2]) if tail_known else (raw_fields['date'], '') if wide_date_only else ('', '')
        tail_known = tail_known or bool(wide_date_only)
        # Retain the wrapped source token separately only with immediate metadata anatomy.
        if (wide_date_only and len(block['detail']) >= 2 and re.fullmatch(r'[0-9]+', block['detail'][0])
                and block['detail'][1].startswith('Наименование корреспондента:')):
            fields['suffix'] = block['detail'][0]
            block['wrapped_suffix_raw'] = block['detail'][0]
            block['detail'] = block['detail'][1:]
            block['detail_evidence'] = block['detail_evidence'][1:]
        for key in ('document_number_raw', 'operation_code_raw', 'executor_code_raw', 'bank_code', 'account', 'suffix'):
            fields[key] = compact(fields[key])
        # A text-tail regex must not repair or shift the reviewed column geometry.
        row_columns_known = block['columns_known']
        valid = row_columns_known and tail_known and not block['line'].get('overlaps') and not block['gaps']
        field_status = {}
        for key, (start, end) in columns.items():
            field_status[key] = ('unknown' if not row_columns_known or field_overlaps(block['line'], start, end) or key in block['identifier_overlap_fields']
                                 else 'observed' if raw_fields[key].strip() else 'absent')
        debit = component_money(fields['printed_debit']) if field_status['printed_debit'] != 'unknown' else None
        credit = component_money(fields['printed_credit']) if field_status['printed_credit'] != 'unknown' else None
        row_date = iso(fields['date']) if row_columns_known else None
        if not tail_known or row_date is None:
            field_status['date'] = 'unknown'
        for key, value in [('printed_debit', debit), ('printed_credit', credit)]:
            if fields[key].strip() and value is None:
                field_status[key] = 'unknown'
                valid = False
        account_identifier = sg.recognize_source_identifier(raw_fields['account'], 'account', 'account' in block['identifier_overlap_fields'])
        bank_identifier = sg.recognize_source_identifier(raw_fields['bank_code'], 'bank', 'bank_code' in block['identifier_overlap_fields'])
        # Recognized source spelling remains available even when its checksum fails.
        # Usability and row readiness remain separate from this source observation.
        account_known = account_identifier['format_status'] == 'recognized' and field_status['account'] == 'observed'
        account_usable = account_known and identifiers.usable(account_identifier)
        document_known = fields['document_number_raw'].isdigit() and field_status['document_number_raw'] == 'observed'
        executor_known = fields['executor_code_raw'].isdigit() and field_status['executor_code_raw'] == 'observed'
        if not executor_known:
            field_status['executor_code_raw'] = 'unknown'
            valid = False
        operation_code_known = fields['operation_code_raw'].isdigit() and field_status['operation_code_raw'] == 'observed'
        if not operation_code_known:
            field_status['operation_code_raw'] = 'unknown'
        if not row_date or not account_usable or not document_known or not operation_code_known:
            valid = False
        detail = block['detail']
        metadata, metadata_assertions, purpose_lines, metadata_gaps = metadata_fields(detail, block['detail_evidence'])
        tax_lines = [text for text in purpose_lines if 'По разделу налога' in text]
        cash_lines = [text for text in purpose_lines if 'символ' in text and 'По разделу налога' not in text]
        def single(label):
            values = metadata.get(label, [])
            return values[0] if len(values) == 1 else None
        cname = single('Наименование корреспондента:') or None
        bname = single('Наименование банка:') or None
        payer_unp, payee_unp, behalf_unp = [compact(single(label) or '') or None for label in
            ('УНП плательщика:', 'УНП получателя:', 'УНП за кого:')]
        priority = single('Очередность платежа:') or single('Очередность:')
        priority = compact(priority) if priority else None
        # A printed zero is an observation, not evidence of a cash movement.
        d, c = sg.decimal(debit or '0'), sg.decimal(credit or '0')
        money_known = all(field_status[k] != 'unknown' for k in ('printed_debit', 'printed_credit'))
        direction = ('outflow' if money_known and d > 0 and c == 0 else
                     'inflow' if money_known and c > 0 and d == 0 else 'unknown')
        amount = debit if direction == 'outflow' else credit if direction == 'inflow' else None
        if direction == 'unknown':
            valid = False
        row_props = {'transaction_date': row_date, 'booking_date': None, 'value_date': None, 'document_date': None,
                     'service_period_raw': None, 'document_number_raw': fields['document_number_raw'] if document_known else None, 'document_type_raw': None,
                     'unlabelled_numeric_raw': fields['suffix'] or None, 'printed_debit': debit, 'printed_credit': credit,
                     'direction': direction, 'amount': amount,
                     'bank_debit_is': 'outflow' if row_columns_known else 'unknown',
                     'purpose_raw': '\n'.join(purpose_lines),
                     'payment1c_comment_raw': '\n'.join(purpose_lines), 'classification': None,
                     'purpose_status': 'unknown' if block['gaps'] or 'unsupported_or_damaged_identity_label' in metadata_gaps else 'observed',
                     'classification_status': 'unknown', 'row_status': 'complete' if valid else 'partial', 'duplicate_status': 'unique',
                     'operation_code_raw': fields['operation_code_raw'] if field_status['operation_code_raw'] == 'observed' else None,
                     'executor_code_raw': fields['executor_code_raw'] if executor_known else None, 'priority_raw': priority}
        standalone_fee = (row_props['purpose_status'] == 'observed' and
                          cash_acceptance_fee(row_props['purpose_raw'], direction))
        if standalone_fee:
            row_props.update(classification='bank_fee', classification_status='inferred')
        row = {'alias': row_alias, 'properties': row_props,
               'observations': [obs('printed_debit', fields['printed_debit'], debit, field_status['printed_debit']),
                                obs('printed_credit', fields['printed_credit'], credit, field_status['printed_credit']),
                                obs('transaction_date', fields['date'], row_date, field_status['date']), obs('booking_date', None, None, 'unknown'),
                                obs('value_date', None, None, 'unknown'), obs('document_date', None, None, 'unknown')],
               'unrecognized_fields': []}
        row['observations'] += [obs('bank_row_raw', raw, None),
                                obs('identity_detail_raw', '\n'.join(detail), None,
                                    'unknown' if block['gaps'] or metadata_gaps else 'observed')]
        if block.get('wrapped_suffix_raw'):
            row['observations'].append(obs('wrapped_unlabelled_numeric_raw', block['wrapped_suffix_raw'], fields['suffix']))
        statements['operations'].append(row)
        if not valid:
            partial.append({'ordinal': ordinal, 'page': block['page'], 'reason': 'row fields or geometry unresolved',
                            'gaps': block['gaps'] + (['overlapping row glyphs'] if block['line'].get('overlaps') else []) +
                            ([] if row_columns_known else ['column heading missing or shifted']) +
                            ([] if tail_known else ['date/suffix boundary unresolved']) +
                            ([] if document_known else ['document_number_raw unreadable or absent']) +
                            ([] if executor_known else ['executor_code_raw unreadable or absent']) +
                            ([] if account_known else ['correspondent account unreadable or absent'])})
        role = 'payer' if direction == 'inflow' else 'payee' if direction == 'outflow' else 'unknown'
        counterparty_unp = payer_unp if role == 'payer' else payee_unp if role == 'payee' else None
        # This adapter's supported name anatomy is the correspondent block.
        # Other explicit role-name assertions are retained, never silently used
        # to override it or mistaken for business purpose.
        unsupported_role_names = any(label in metadata for label in
            ('Наименование плательщика:', 'Наименование получателя:', 'Наименование за кого:'))
        identity_known = (role != 'unknown' and readable_name(cname) and
                          readable_unp(counterparty_unp) and not metadata_gaps and
                          not unsupported_role_names and not block['gaps'] and
                          len(metadata.get('Наименование корреспондента:', [])) == 1 and
                          len(metadata.get('УНП ' + ('плательщика:' if role == 'payer' else 'получателя:'), [])) == 1)
        row_props['counterparty'] = {'role': role, 'party_id': row_alias + ':' + role if role != 'unknown' else None,
                                     'name_raw': cname, 'unp_raw': counterparty_unp or None,
                                     'status': 'observed' if identity_known else 'unknown'}
        # Retain each printed payer/payee assertion separately, even for the account holder.
        for role, unp, name in [('PAYER', payer_unp, holder if direction == 'outflow' else cname if direction == 'inflow' else None),
                                ('PAYEE', payee_unp, cname if direction == 'outflow' else holder if direction == 'inflow' else None)]:
            party_alias = row_alias + ':' + role.lower()
            entities.append(entry(party_alias, 'Party', {'name_raw': name or None, 'unp_raw': unp if unp else None,
                                  'role_raw': role.lower(), 'identity_status': ('observed' if identity_known else 'unknown') if role.lower() == row_props['counterparty']['role'] else 'unknown'},
                                  [obs('unp_raw', unp, unp or None, 'observed' if readable_unp(unp) else 'unknown' if unp else 'absent'),
                                   obs('name_raw', name, name or None, 'observed' if readable_name(name) else 'unknown' if name else 'absent')]))
            relations.append({'type': role, 'from': row_alias, 'to': party_alias})
        row['observations'].append(obs('on_behalf_unp', behalf_unp, behalf_unp or None))
        if behalf_unp:
            party_alias = row_alias + ':on-behalf'
            entities.append(entry(party_alias, 'Party', {'name_raw': None, 'unp_raw': behalf_unp,
                                  'role_raw': 'УНП за кого', 'identity_status': 'observed'}))
            relations.append({'type': 'ON_BEHALF_OF', 'from': row_alias, 'to': party_alias})
        corr_account = row_alias + ':correspondent-account'
        entities.append(entry(corr_account, 'Account', {'number_raw': fields['account'] if account_known else None, 'format': 'iban' if account_identifier['format'] == 'iban' else 'historical' if account_identifier['format'] == 'historical_numeric' else 'unknown',
                              'identifier': account_identifier, 'identity_status': 'observed' if account_usable else 'unknown', 'iban_effective_date': None, 'iban_raw': raw_fields['account'] if account_identifier['format'] == 'iban' else None, 'bic_raw': raw_fields['bank_code'] if bank_identifier['format'] == 'bic' else None},
                              [obs('number_raw', raw_fields['account'], fields['account'] if account_known else None, 'observed' if account_known else 'unknown')]))
        bank_alias = row_alias + ':correspondent-bank'
        bank_code_known = field_status['bank_code'] == 'observed' and identifiers.usable(bank_identifier)
        if not bank_code_known and field_status['bank_code'] == 'observed':
            field_status['bank_code'] = 'unknown'
        entities.append(entry(bank_alias, 'Bank', {'name_raw': bname or None, 'code_raw': fields['bank_code'] if bank_code_known else None,
                              'identifier': bank_identifier, 'code_system_raw': 'Код', 'valid_from': None, 'valid_to': None,
                              'identity_status': 'observed' if bank_code_known else 'unknown',
                              'branch_raw': None, 'city_raw': None, 'country_raw': None},
                              [obs('name_raw', bname, bname or None),
                               obs('code_raw', raw_fields['bank_code'], fields['bank_code'] if bank_code_known else None,
                                   field_status['bank_code'])]))
        for key, entity in [('account', entities[-2]), ('bank_code', entities[-1])]:
            if key in block['identifier_overlap_fields']:
                entity['observations'].append(obs('identifier_glyphs_raw', raw_fields[key], None, 'unknown'))
        relations.append({'type': 'SERVICED_BY', 'from': corr_account, 'to': bank_alias})
        if direction != 'unknown':
            relations.append({'type': 'TO_ACCOUNT' if direction == 'outflow' else 'FROM_ACCOUNT', 'from': row_alias, 'to': corr_account})
        for n, text in enumerate(tax_lines, 1):
            tax_alias = row_alias + ':tax:' + str(n)
            match = re.search(r'По разделу налога\s*:?\s*(\d+)', text)
            entities.append(entry(tax_alias, 'TaxDetail', {'tax_kind_raw': None, 'budget_code_raw': match[1] if match else None,
                                  'payment_kind_raw': None, 'period_raw': None, 'payer_unp_raw': payer_unp or None},
                                  [obs('tax_detail_raw', text, None)], [unknown('tax_subline_raw', text)]))
            relations.append({'type': 'HAS_TAX', 'from': row_alias, 'to': tax_alias})
        for n, text in enumerate(cash_lines, 1):
            cash_alias = row_alias + ':cash:' + str(n)
            match = re.search(r'символ\s+(\d+)\s*:\s*(.*)', text)
            entities.append(entry(cash_alias, 'CashSymbol', {'code_raw': match[1] if match else None,
                                  'amount_raw': match[2] if match else None, 'amount': None,
                                  'markers_raw': text, 'semantics_status': 'unknown'}, [obs('cash_subline_raw', text, None)]))
            relations.append({'type': 'HAS_CASH_SYMBOL', 'from': row_alias, 'to': cash_alias})
        # Only explicit labels yield detail nodes. The complete purpose stays authoritative.
        purpose = row_props['purpose_raw']
        row_props['payment1c_text'] = sg.prepare_payment_text(purpose, row_props['purpose_status'])
        component_counts = {}
        components = fee_basis(purpose) if standalone_fee else amount_components(purpose, direction, amount, currency)
        if row_props['purpose_status'] == 'unknown':
            components = [(*c[:4], 'unknown') for c in components]
        row_props['settlement'] = settlement_view(components, direction, amount, standalone_fee)
        for offset, kind, value_raw, mention_raw, inclusion in components:
            component_counts[kind] = component_counts.get(kind, 0) + 1
            component = row_alias + ':' + kind + ':' + str(component_counts[kind])
            value = component_money(value_raw)
            unit, component_currency = component_unit(purpose, offset, mention_raw)
            entities.append(entry(component, 'AmountComponent', {'kind': kind, 'value_raw': value_raw,
                                  'value': value, 'currency_raw': component_currency, 'inclusion': inclusion, 'unit_raw': unit},
                                  [obs('mention_raw', mention_raw, None, 'observed'),
                                   obs('value', value_raw, value, 'observed' if value is not None else 'unknown'),
                                   obs('kind', mention_raw, kind, 'inferred')]))
            relations.append({'type': 'HAS_COMPONENT', 'from': row_alias, 'to': component})
        for n, match in enumerate(re.finditer(r'(?i)(дог(?:овор(?:а|у|ом)?)?\.?|сф|счет(?:у)?|отчет(?:у|а|ам)?)[ \t]*(?:[N№][ \t]*)?([\w\d][\w\d/.-]*)', purpose), 1):
            # Avoid treating ordinary language as a numbered reference.
            if not any(c.isdigit() for c in match[2]):
                continue
            reference = row_alias + ':reference:' + str(n)
            entities.append(entry(reference, 'DocumentReference', {'kind_raw': match[1], 'number_raw': match[2],
                                  'date': None, 'period_raw': None, 'availability': 'mentioned_only', 'artifact_ref': None},
                                  [obs('mention_raw', match[0], None)]))
            relations.append({'type': 'MENTIONS', 'from': row_alias, 'to': reference})
        row_evidence.append({'alias': row_alias, 'page': block['page'], 'bank_row_raw': raw,
                             'bank_row_cells': fixed, 'identifier_continuations': block['identifier_continuation_evidence'], 'detail': block['detail_evidence'],
                             'fields': {key: {'raw': raw_fields[key], 'column_cells': list(columns[key]),
                                              'status': field_status[key],
                                              'normalized': (debit if key == 'printed_debit' else credit if key == 'printed_credit'
                                                             else row_date if key == 'date'
                                                             else fields.get(key) if field_status[key] == 'observed' else None)}
                                        for key in columns},
                             'bank_identifiers': {'bank': bank_identifier, 'account': account_identifier},
                             'metadata_blocks': metadata, 'metadata_assertions': metadata_assertions,
                             'metadata_gaps': metadata_gaps, 'purpose_lines': purpose_lines,
                             'document_view': {**row_props, 'payer_unp_raw': payer_unp,
                                               'payee_unp_raw': payee_unp, 'on_behalf_unp_raw': behalf_unp,
                                               'correspondent_name_raw': cname, 'bank_name_raw': bname,
                                               'correspondent_account_raw': fields['account'] if account_known else None,
                                               'bank_document_kind_candidate': ('Поступление на расчетный счет' if direction == 'inflow' else
                                                                                'Списание с расчетного счета' if direction == 'outflow' else None),
                                               'reference_totals': [{'value_raw': c[2], 'printed_value': component_money(c[2]),
                                                                     'mention_raw': c[3], 'relationship': 'unknown'}
                                                                    for c in components if c[1] == 'gross' and not same_settlement_gross(c)],
                                               'amount_origin': 'same_operation_bank_cells' if amount is not None else 'unknown'},
                             'component_interpretation': ('standalone_fee_basis' if standalone_fee else
                                  row_props['settlement']['status'] if any(c[4] == 'included' for c in components) else
                                  'unknown' if components else 'not_applicable'),
                             'component_gaps': component_gaps(components, direction, amount, purpose, currency),
                             'unverified_components': [{'kind': c[1], 'value_raw': c[2], 'printed_value': component_money(c[2]),
                                                        'mention_raw': c[3], 'inclusion': c[4]}
                                                       for c in components if c[4] != 'included'],
                             'gaps': block['gaps']})
    # Field-type evidence is a label inventory of this original statement,
    # independent of the number of nonblank/readable values. Holder headers,
    # bank/account columns and adapter-wide support never supply these types.
    for item in residual:
        statements['observations'].append(obs('unassigned_business_raw', item['text'], None, 'unknown'))
    for evidence in row_evidence:
        for assertion in evidence['metadata_assertions']:
            field = IDENTITY_FIELD_LABELS.get(assertion['label'])
            if field and not assertion['label_overlapping']:
                observation = obs('counterparty_field_type', assertion['label_raw'], field)
                if observation not in statements['observations']:
                    statements['observations'].append(observation)
    materialize_source_header(statements, entities, relations,
                              [line['raw_text'] for line in doc['pages'][0]['lines']], index)
    props['expected_operations'] = len(blocks)
    props['rows_complete'] = columns_known and not partial and not residual
    reviewed = {'dataset_id': dataset_id, 'statements': [statements], 'entities': entities, 'relations': relations}
    graph = sg.build(reviewed, diagnostic=True)
    admission = sg.identity_admission(graph)['statements'][0]
    admission['missing_identity_rows'] = [r['operation_id'] for r in admission['rows'] if r['gaps']]
    for row, evidence, state in zip(statements['operations'], row_evidence, admission['rows']):
        row['properties']['processing'] = {k: state[k] for k in ('status', 'gaps', 'questions', 'effective_counterparty')}
        view = evidence['document_view']
        view['processing'] = row['properties']['processing']
        view['statement_processing_allowed'] = admission['status'] != 'blocked'
        view['row_processing_allowed'] = state['status'] == 'ready'
        if state['status'] != 'ready':
            view['bank_document_kind_candidate'] = None
    props['expected_operations'] = len(blocks)
    props['rows_complete'] = columns_known and not partial and not residual
    return statements, entities, relations, {'statement_id': 'dataset:' + dataset_id + ':' + alias,
        'pages': len(doc['pages']), 'operations': len(blocks), 'partial_rows': partial,
        'identity_admission': admission,
        'column_contract': {'recognized': columns_known, 'columns': columns, 'evidence': column_evidence},
        'residual_business_lines': residual, 'rows': row_evidence,
        'page_header_decoration': [{'page': page, 'offset': offset} for page, offset in sorted(page_decoration)],
        'footer_raw': turnover['raw_text'] if turnover else None,
        'money_controls_derived_from_rows': False,
        'business_fonts': sorted({f for page in doc['pages'] for f in page.get('business_fonts', page.get('other_fonts', []))})}


def assemble(paths, dataset_id, *, diagnostic=False):
    reviewed = {'dataset_id': dataset_id, 'entities': [], 'statements': [], 'relations': []}
    coverage = []
    for index, path in enumerate(paths, 1):
        statement, entities, relations, qa = parse_statement(json.loads(Path(path).read_text()), index, dataset_id)
        reviewed['statements'].append(statement); reviewed['entities'] += entities; reviewed['relations'] += relations
        coverage.append(qa)
    if not diagnostic:
        sg.require_processing_admission(sg.build(reviewed))
    return reviewed, {'statements': coverage}



def balance_only_archive(doc, dataset_id):
    """Narrow zero-operation archival output; never processing acceptance.

    The observed one-page monospaced form has real signed balances and no
    turnover block. Its table/body/terminal anatomy must prove emptiness. No
    invented zero controls or counterparty metadata are introduced.
    """
    if len(doc.get('pages',[]))!=1:raise ValueError('balance-only profile requires one complete original page')
    statement,entities,relations,qa=parse_statement(doc,1,dataset_id)
    footer=footer_contract(doc)
    if (statement['operations'] or statement['properties']['expected_operations']!=0 or
        qa['residual_business_lines'] or not qa['column_contract']['recognized'] or footer['turnover'] is not None or
        any(statement['properties'][k] is None for k in ('opening','closing'))):
        raise ValueError('unproven balance-only empty source')
    statement['properties']['archive_only']=True
    statement['observations'].append(obs('archive_profile','belarusbank-monospaced-balance-only-v1','archive_only'))
    reviewed={'dataset_id':dataset_id,'statements':[statement],'entities':entities,'relations':relations}
    graph=sg.build(reviewed,diagnostic=True);sg.materialize_processing(graph)
    qa.update(archive_only=True,accepted=False,processing_status='blocked_format',validation=sg.validate(graph))
    if not qa['validation']['valid']:raise ValueError('invalid balance-only archive')
    return graph,qa

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'assemble', 'modern-prepare', 'modern-diagnostic', 'modern-assemble', 'balance-only-archive'])
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--qa-output', type=Path)
    parser.add_argument('--dataset-id')
    args = parser.parse_args()
    if args.action == 'modern-prepare':
        import modern_adapter
        sg.write_new(args.output, modern_adapter.prepare(args.input))
    elif args.action == 'balance-only-archive':
        if not args.dataset_id or not args.qa_output:parser.error('balance-only-archive requires dataset-id and qa-output')
        graph,qa=balance_only_archive(json.loads(args.input.read_text()),args.dataset_id)
        sg.write_new_pair(args.qa_output,qa,args.output,graph)
        print(json.dumps({'action':args.action,'archive_only':True,'accepted':False,'operations':0,'status':'needs_review'}))
        return 0
    elif args.action == 'modern-assemble':
        import modern_adapter
        if not args.dataset_id or not args.qa_output:parser.error('modern-assemble requires dataset-id and qa-output')
        statement,entities,relations,qa=modern_adapter.parse_statement(json.loads(args.input.read_text()),1,args.dataset_id,reviewed=True)
        reviewed={'dataset_id':args.dataset_id,'statements':[statement],'entities':entities,'relations':relations}
        if qa['residual_business_lines']:
            graph=sg.build(reviewed,diagnostic=True);sg.materialize_processing(graph)
            qa['validation']=sg.validate(graph);qa['processing_status']='blocked'
            qa['diagnostic_reviewed_input']=reviewed
            modern_adapter.refresh_document_views(graph,qa)
            sg.write_new(args.qa_output,qa)
            print(json.dumps({'action':args.action,'status':'blocked','operations':qa['operations'],'accepted':False,'private_qa_saved':True}))
            return 2
        graph=sg.build(reviewed);validation=sg.validate(graph);qa['validation']=validation
        if not validation['valid']:raise ValueError('modern graph source validation failed')
        qa['processing_status']=validation['processing_admission']['status']
        modern_adapter.refresh_document_views(graph,qa)
        sg.write_new_pair(args.qa_output,qa,args.output,reviewed)
        print(json.dumps({'action':args.action,'status':qa['processing_status'],'operations':qa['operations'],'accepted':False}))
        return 0
    elif args.action == 'modern-diagnostic':
        import modern_adapter
        if not args.dataset_id:parser.error('modern-diagnostic requires --dataset-id')
        statement, entities, relations, qa = modern_adapter.parse_statement(json.loads(args.input.read_text()), 1, args.dataset_id)
        reviewed={'dataset_id':args.dataset_id,'statements':[statement],'entities':entities,'relations':relations}
        graph=sg.build(reviewed,diagnostic=True)
        sg.materialize_processing(graph)
        qa['validation']=sg.validate(graph)
        if qa['validation'].get('processing_admission',{}).get('status')!='blocked':raise ValueError('modern diagnostics must remain processing blocked')
        qa['diagnostic_reviewed_input']=reviewed
        sg.write_new(args.output,qa)
        print(json.dumps({'action':args.action,'status':'blocked_format','private_qa_saved':True,'operations':qa['operations'],'accepted':False}))
        return 2
    elif args.action == 'prepare':
        sg.write_new(args.output, geometry(args.input))
    else:
        if not args.dataset_id or not args.qa_output:
            parser.error('assemble requires --dataset-id and --qa-output')
        reviewed, coverage = assemble(json.loads(args.input.read_text()), args.dataset_id, diagnostic=True)
        if any(q['identity_admission']['status'] == 'blocked' for q in coverage['statements']):
            sg.write_new(args.qa_output, coverage)
            print(json.dumps({'action': args.action, 'status':'blocked_format', 'output_saved':False, 'private_qa_saved':True}))
            return 2
        # Gate the actual output through the same semantic admission as build.
        sg.require_processing_admission(sg.build(reviewed))
        sg.write_new_pair(args.qa_output, coverage, args.output, reviewed)
    print(json.dumps({'action': args.action, 'output_saved': True,
                      'status': ('partial' if any(q['identity_admission']['status'] == 'partial' for q in coverage['statements']) else 'admitted')
                                if args.action == 'assemble' else None}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
