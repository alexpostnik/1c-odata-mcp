"""Lossless offline bank identifier recognition; no registry/existence claim."""
import re

PROFILE = 'bank-identifiers-by-v1'

def recognize(raw, kind):
    if kind not in ('bank', 'account'):
        raise ValueError('identifier kind must be bank or account')
    if raw is not None and not isinstance(raw, str):
        raise ValueError('bank identifiers must be strings or null')
    result = {'raw': raw, 'normalized': None, 'format': 'unknown',
              'format_status': 'unknown', 'value_check': 'not_checked',
              'existence': 'unknown', 'profile': PROFILE, 'diagnostics': []}
    value = re.sub(r'\s', '', raw or '')
    if not value:
        result['format_status'] = 'absent'
        return result
    # No case folding, transliteration, punctuation removal or prefix repair.
    if re.fullmatch(r'[0-9]+', value):
        result.update(normalized=value, format='historical_numeric',
                      format_status='recognized', value_check='not_applicable')
        result['diagnostics'] = ['historical_checksum_contract_unavailable']
    elif kind == 'bank' and re.fullmatch(r'[A-Z0-9]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?', value):
        result.update(normalized=value, format='bic', format_status='recognized')
        result['diagnostics'] = ['country_and_bic_registry_not_checked']
    elif kind == 'account' and re.match(r'[A-Z]{2}[0-9]{2}', value):
        result.update(format='iban')
        if value.startswith('BY'):
            if re.fullmatch(r'BY[0-9]{2}[A-Z0-9]{4}[0-9]{4}[A-Z0-9]{16}', value):
                result.update(normalized=value, format_status='recognized')
                # ISO7064 MOD97-10; arbitrary-precision integer, never identifier coercion.
                expanded = ''.join(str(ord(c) - 55) if 'A' <= c <= 'Z' else c
                                   for c in value[4:] + value[:4])
                result['value_check'] = 'passed' if 2 <= int(value[2:4]) <= 98 and int(expanded) % 97 == 1 else 'failed'
                if result['value_check'] == 'failed':
                    result['diagnostics'] = ['iban_checksum_failed']
            else:
                result.update(format_status='damaged', value_check='failed')
                result['diagnostics'] = ['belarus_iban_length_or_bban_shape_failed']
        else:
            result['diagnostics'] = ['iban_country_contract_unsupported']
    else:
        result['format_status'] = 'damaged' if re.search(r'[?*\ufffd\x00-\x1f]', value) else 'unknown'
        result['diagnostics'] = ['identifier_format_unrecognized']
    return result

def usable(value):
    return value['format_status'] == 'recognized' and value['value_check'] != 'failed'

def boundary_candidate(raw, kind):
    """Boundary grammar independent of checksum/value readiness, shared by all paths."""
    value = recognize(raw, kind)
    if value['format_status'] == 'recognized':
        return True
    # Damaged same-column values preserve a row only with the caller's independent
    # aligned document, padded operation, money and date evidence.
    token = re.sub(r'\s', '', raw or '')
    length_ok = 3 <= len(token) <= 35 if kind == 'bank' else 6 <= len(token) <= 35
    # Unknown spelling is not value recognition: keep lowercase/confusable
    # letters only as boundary evidence, never case-fold or transliterate raw.
    # Plain prose such as 'reference' must not satisfy an identifier column.
    characters_ok = all(c.isalpha() or c in '0123456789?*\ufffd' for c in token)
    identifier_shape = (bool(re.fullmatch(r'[A-Z0-9?*\ufffd]+', token)) or
                        any(c in '0123456789?*\ufffd' for c in token) or
                        (kind == 'bank' and len(token) in (8, 11)))
    return bool(token and length_ok and characters_ok and identifier_shape)
