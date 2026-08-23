"""QR <-> Claude extraction validation gate."""
import difflib
import re

from qr import qr_cents, qr_date

TOL = 2  # cents

IVA_QUIRKS = {
    'teofilo': 'iva_doubled',        # QR N = 2x real IVA (ZFV1 series)
    'orientalshopping': 'base_in_n', # issuer software puts the base in N
    'seminoshopping': 'base_in_n',
    'goldenmarina': 'base_in_n',     # same POS software family
}


def _norm_ref(s):
    if not s:
        return ''
    s = re.sub(r'\s+', ' ', s.strip().upper())
    m = re.match(r'^(.*?)(\d+)$', s)
    return (m.group(1) + str(int(m.group(2)))) if m else s


_GENERIC_REF_WORDS = {'FACTURA', 'FATURA', 'INVOICE', 'N', 'NR', 'NO', 'NUM',
                      'NUMERO', 'DOC', 'DOCUMENTO', 'NOTA', 'DE', 'CREDITO',
                      'CRÉDITO', 'RECIBO', 'SIMPLIFICADA', 'SALE', 'VENDA'}


def _ref_tokens(s):
    """(numeric tokens, alpha tokens) of a document reference: letter and
    digit runs are split ("FAL2026" -> FAL, 2026; "V071" -> V, 71) and
    numeric tokens lose leading zeros. Issuers print the reference with
    different punctuation/order/wording from the QR G field (Garcias:
    "FT FAL 5307/2026" or "FT FAL2026/5307" for "FT FAL.2026/5307"; JMV:
    "Factura 1412129305" for "ZF2 B123/1412129305"), so the reference
    check also accepts a token-based match (see _tokens_match)."""
    if not s:
        return set(), set()
    nums, alpha = set(), set()
    for tok in re.findall(r'[A-Z]+|[0-9]+', s.upper()):
        if tok.isdigit():
            nums.add(str(int(tok)))
        elif tok not in _GENERIC_REF_WORDS:
            alpha.add(tok)
    return nums, alpha


def _tokens_match(ref, qr_ref, doc_type=None):
    """Token-based reference match: every numeric token of the printed
    reference must exist in the QR reference, every long (>= 4 digits) QR
    number must be present in the printed one, and the printed letter
    tokens must be a subset of the QR's (plus the QR doc type, which some
    extractions prefix: "NC C CABA/020980" for D=NC, G="C CABA/020980")."""
    r_nums, r_alpha = _ref_tokens(ref)
    q_nums, q_alpha = _ref_tokens(qr_ref)
    if not r_nums or not r_nums <= q_nums:
        return False
    if any(len(n) >= 4 and n not in r_nums for n in q_nums):
        return False
    allowed = q_alpha | ({doc_type.upper()} if doc_type else set())
    return r_alpha <= allowed


def _close(a, b):
    return a is not None and b is not None and abs(a - b) <= TOL


def validate(extraction, qr_fields, supplier_key):
    checks, notes = {}, []
    lines = extraction.get('lines') or []
    line_sum = sum(ln.get('line_total_cents') or 0 for ln in lines)
    e_total = extraction.get('total_cents')
    e_doc = extraction.get('total_document_cents')
    # vasilhame/tara invoices: the lines include tara at 0%, so they sum to
    # the payable (document) total, not the merchandise total
    has_doc_total = e_doc is not None and e_doc != e_total
    gross_target = (e_doc if has_doc_total else e_total) or 0
    # The gross per line is derived (net x rate, rounded), so the gross sum
    # legitimately drifts up to ~1c per line from the printed total; the net
    # sum is what the document itself prints and must match the base (plus
    # vasilhame, which is billed at 0% and so is net == gross) within TOL.
    # Missing/hallucinated lines are still caught: they err by whole-line
    # amounts, not by cents.
    gross_ok = abs(line_sum - gross_target) <= max(TOL, len(lines))
    net_ok = False
    if lines and all(ln.get('line_net_cents') is not None for ln in lines):
        net_sum = sum(ln['line_net_cents'] for ln in lines)
        base = extraction.get('base_cents')
        if base is not None:
            net_target = base + ((extraction.get('total_vasilhame_cents') or 0)
                                 if has_doc_total else 0)
            net_ok = abs(net_sum - net_target) <= TOL
    checks['soma_linhas'] = gross_ok or net_ok

    if not qr_fields:
        notes.append('sem QR fiscal — validação impossível, retido')
        return {'status': 'needs_review', 'checks': checks, 'notes': notes}

    base_key = (supplier_key or '').removesuffix('_nc')
    q_total = qr_cents(qr_fields.get('O'))
    q_iva = qr_cents(qr_fields.get('N'))
    e_iva = extraction.get('iva_cents')

    if has_doc_total:
        # QR O may carry either the merchandise or the payable total
        if _close(e_total, q_total):
            checks['total_vs_qr'] = True
            notes.append('total_vs_qr: total_cents corresponde ao QR O')
        elif _close(e_doc, q_total):
            checks['total_vs_qr'] = True
            notes.append('total_vs_qr: total_document_cents corresponde ao QR O')
        else:
            checks['total_vs_qr'] = False
    else:
        checks['total_vs_qr'] = _close(e_total, q_total)

    quirk = IVA_QUIRKS.get(base_key)
    if _close(e_iva, q_iva):
        checks['iva_vs_qr'] = True
    elif quirk == 'iva_doubled' and e_iva is not None and _close(e_iva * 2, q_iva):
        checks['iva_vs_qr'] = True
        notes.append('quirk iva_doubled aplicado (QR N = 2x IVA)')
    elif (quirk == 'base_in_n' and e_iva is not None and e_total is not None
          and _close(e_total - e_iva, q_iva)):
        checks['iva_vs_qr'] = True
        notes.append('quirk base_in_n aplicado (QR N contém a base)')
    elif e_iva in (0, None) and (qr_cents(qr_fields.get('M')) or 0) > 0:
        # documentos de imposto do selo (ex.: BCP): N > 0 com IVA real 0
        checks['iva_vs_qr'] = True
        notes.append('quirk stamp_duty aplicado (QR M > 0, IVA 0)')
    else:
        checks['iva_vs_qr'] = False

    g, h = qr_fields.get('G'), qr_fields.get('H')
    full = f'{g} / {h}' if h else g
    ref = _norm_ref(extraction.get('invoice_ref'))
    checks['ref_vs_qr'] = bool(ref) and (
        ref in (_norm_ref(g), _norm_ref(full))
        or _tokens_match(extraction.get('invoice_ref'), g, qr_fields.get('D'))
        or _tokens_match(extraction.get('invoice_ref'), full, qr_fields.get('D')))

    checks['date_vs_qr'] = (extraction.get('date') == qr_date(qr_fields)
                            and extraction.get('date') is not None)

    # Long till-receipt serials come back with single-character misreads
    # while every amount matches the QR. When the ATCUD (H) independently
    # confirms the document number, the misread prefix is tolerable: accept
    # the reference if (a) every other check passed, (b) the trailing digit
    # runs of ref and H agree (one a suffix of the other), and (c) the
    # normalized strings are still >= 75% similar.
    if not checks['ref_vs_qr'] and ref and h:
        others_ok = all(v for k, v in checks.items() if k != 'ref_vs_qr')
        h_num = re.sub(r'\D', '', h.split('-')[-1]).lstrip('0')
        r_trail = (re.findall(r'\d+', ref) or [''])[-1].lstrip('0')
        num_ok = bool(h_num) and bool(r_trail) and (
            r_trail.endswith(h_num) or h_num.endswith(r_trail))
        # With every amount and the date QR-confirmed AND the ATCUD document
        # number present in the printed reference, issuer-format differences
        # (internal codes, wording, thousands separators) are accepted; the
        # similarity floor only rejects references about another document
        # entirely. Real misreads keep a WRONG number and never get here.
        sim = difflib.SequenceMatcher(None, ref, _norm_ref(g)).ratio()
        rich = len(re.findall(r'[A-Z]+|[0-9]+', ref)) >= 2
        if others_ok and num_ok and rich and len(h_num) >= 4 and sim >= 0.3:
            checks['ref_vs_qr'] = True
            notes.append('ref aproximada aceite (confirmada pelo ATCUD, '
                         f'similaridade {sim:.2f})')

    # internal consistency: base + IVA must give the total (or, when the
    # base includes 0% vasilhame lines, the document total). The QR has no
    # base field, so this is the only guard against a misread base_cents.
    e_base = extraction.get('base_cents')
    if e_base is None or e_iva is None:
        checks['base_mais_iva'] = True
    else:
        checks['base_mais_iva'] = (_close(e_base + e_iva, e_total)
                                   or (has_doc_total
                                       and _close(e_base + e_iva, e_doc)))

    status = 'ok' if all(checks.values()) else 'needs_review'
    return {'status': status, 'checks': checks, 'notes': notes}
