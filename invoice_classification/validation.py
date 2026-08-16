"""QR <-> Claude extraction validation gate."""
import re

from qr import qr_cents, qr_date

TOL = 2  # cents

IVA_QUIRKS = {
    'teofilo': 'iva_doubled',        # QR N = 2x real IVA (ZFV1 series)
    'orientalshopping': 'base_in_n', # issuer software puts the base in N
    'seminoshopping': 'base_in_n',
}


def _norm_ref(s):
    if not s:
        return ''
    s = re.sub(r'\s+', ' ', s.strip().upper())
    m = re.match(r'^(.*?)(\d+)$', s)
    return (m.group(1) + str(int(m.group(2)))) if m else s


def _close(a, b):
    return a is not None and b is not None and abs(a - b) <= TOL


def validate(extraction, qr_fields, supplier_key):
    checks, notes = {}, []
    lines = extraction.get('lines') or []
    line_sum = sum(ln.get('line_total_cents') or 0 for ln in lines)
    checks['soma_linhas'] = line_sum == (extraction.get('total_cents') or 0)

    if not qr_fields:
        notes.append('sem QR fiscal — validação impossível, retido')
        return {'status': 'needs_review', 'checks': checks, 'notes': notes}

    base_key = (supplier_key or '').removesuffix('_nc')
    q_total = qr_cents(qr_fields.get('O'))
    q_iva = qr_cents(qr_fields.get('N'))
    e_total, e_iva = extraction.get('total_cents'), extraction.get('iva_cents')

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
    else:
        checks['iva_vs_qr'] = False

    g, h = qr_fields.get('G'), qr_fields.get('H')
    full = f'{g} / {h}' if h else g
    ref = _norm_ref(extraction.get('invoice_ref'))
    checks['ref_vs_qr'] = ref in (_norm_ref(g), _norm_ref(full)) and bool(ref)

    checks['date_vs_qr'] = (extraction.get('date') == qr_date(qr_fields)
                            and extraction.get('date') is not None)

    status = 'ok' if all(checks.values()) else 'needs_review'
    return {'status': status, 'checks': checks, 'notes': notes}
