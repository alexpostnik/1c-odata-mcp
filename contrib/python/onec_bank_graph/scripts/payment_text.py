"""Reproducible business text for an XML 1.0 client/server boundary.

This is an offline preparation profile, not proof that a 1C form opens or a
particular configuration accepts the value. Original source text is never edited.
"""
import re
import unicodedata

PROFILE = 'xml10-single-line-escp-cash-v1'
SOURCE_BOUND_PROFILE = 'xml10-single-line-source-bound-cash-v1'
SOURCE_PROJECTION_PROFILE = 'source-bound-winansi-1b-cash-service-v1'
# Only the observed complete cash-symbol subline: the parameterless ESC/P italic
# pair surrounds the business text. PDF extraction can render either ESC as
# literal (cid:27); the two independently represented tokens may be mixed.
# Other sequences, unmatched/nested markers or command-like digits in another
# context are not guessed away. A marker-like spelling is held, never erased.
CID_MARKER = re.compile(r'\(cid(?=[:)\s]|$)', re.IGNORECASE)
ITALIC_CASH_LINE = re.compile(
    r'(?m)^[ \t\u00a0]*(?P<start>\x1b4|\(cid:27\)4)'
    r'символ[ \t\u00a0]+[0-9]+[ \t\u00a0]*:'
    r'(?:(?!(?i:\(cid(?=[:)\s]|$)))[^\x1b\r\n])*'
    r'(?P<end>\x1b5|\(cid:27\)5)[ \t\u00a0]*(?=\r?$)')


def xml10_character(char):
    n = ord(char)
    return n in (9, 10, 13) or 0x20 <= n <= 0xD7FF or 0xE000 <= n <= 0xFFFD or 0x10000 <= n <= 0x10FFFF


def prepare_payment_text(raw, purpose_status='observed', *, source_text=None):
    """Return a closed value/status/diagnostics object; held values are null.

    Indices are zero-based Unicode code-point offsets in the untouched raw input,
    not UTF-8 byte offsets or 1C UTF-16 offsets. No truncation, Unicode folding,
    escaping, monetary parsing, placeholder substitution or ASCII filtering.
    """
    if not isinstance(raw, str):
        raise ValueError('payment source must be text')
    diagnostics, removed = [], set()
    for match in ITALIC_CASH_LINE.finditer(raw):
        for group, command in (('start', '4'), ('end', '5')):
            start, end = match.span(group)
            removed.update(range(start, end))
            diagnostics.append({'code': 'removed_escp_italic_' + command, 'index': start,
                                'codepoint': 'U+' + format(ord(raw[start]), '04X')})
    unsafe = False
    if source_text is not None:
        # This opt-in route is for a separately source-reviewed detached copy.
        # An archived source_text object alone is not authenticated evidence.
        cleaned = ''.join(c for i, c in enumerate(raw) if i not in removed)
        if (not isinstance(source_text, dict) or set(source_text) != {'profile', 'value', 'raw_preserved'}
                or source_text.get('profile') != SOURCE_PROJECTION_PROFILE
                or source_text.get('raw_preserved') is not True or not removed
                or source_text.get('value') != cleaned):
            raise ValueError('source_text differs from complete bounded print-command projection')
    for index, char in enumerate(raw):
        if index in removed:
            continue
        code = None
        if char == '\x1b':
            code = 'unknown_escape_sequence'
        elif not xml10_character(char):
            code = 'xml10_forbidden_character'
        elif unicodedata.category(char) == 'Cc' and char not in '\t\r\n':
            # XML1.0 permits these C1/DEL controls, but no source/1C meaning has
            # been established. Hold conservatively; do not call them forbidden.
            code = 'unsupported_control_character'
        if code:
            unsafe = True
            diagnostics.append({'code': code, 'index': index, 'codepoint': 'U+' + format(ord(char), '04X')})
    retained_indices = [i for i in range(len(raw)) if i not in removed]
    cleaned = ''.join(raw[i] for i in retained_indices)
    # Removing a closing token can expose a damaged marker at the end of the
    # business line, e.g. '(cid' immediately before ESC5. Check the retained
    # text, mapping diagnostics back to the untouched source. Otherwise a ready
    # value could become held on its next preparation.
    for match in CID_MARKER.finditer(cleaned):
        unsafe = True
        diagnostics.append({'code': 'unknown_escape_sequence',
                            'index': retained_indices[match.start()], 'codepoint': 'U+0028'})
    value = ' '.join(cleaned.split())
    if value != cleaned:
        diagnostics.append({'code': 'whitespace_collapsed', 'index': 0, 'codepoint': None})
    if purpose_status != 'observed':
        unsafe = True
        diagnostics.append({'code': 'purpose_boundary_unresolved', 'index': 0, 'codepoint': None})
    # A proven empty source purpose is a valid empty string, never a fabricated
    # placeholder. Unknown boundaries and unsafe controls still hold the row.
    if not value and raw.strip():
        unsafe = True
        diagnostics.append({'code': 'empty_business_text', 'index': 0, 'codepoint': None})
    return {'profile': PROFILE if source_text is None else SOURCE_BOUND_PROFILE, 'status': 'held' if unsafe else 'ready',
            'value': None if unsafe else value, 'diagnostics': diagnostics}


def writable_payment_text(properties, *, source_text=None):
    """Pre-write guard for consumers. There is deliberately no raw fallback."""
    if source_text is not None and properties.get('source_text') != source_text:
        raise ValueError('source_text differs from bound operation projection')
    expected = prepare_payment_text(properties['purpose_raw'], properties.get('purpose_status', 'unknown'), source_text=source_text)
    if properties.get('payment1c_text') != expected or expected['status'] != 'ready':
        raise ValueError('payment1c_text missing, stale or held; no writable text')
    if properties.get('payment1c_comment_raw', properties['purpose_raw']) != properties['purpose_raw']:
        raise ValueError('source comment differs from full original purpose')
    return expected['value']
