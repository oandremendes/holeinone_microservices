"""Envio de extrações para o webhook Odoo /webhook/claude/invoice.

Núcleo standalone: sem imports de app/db/config/Flask; todo o I/O
(rclone, HTTP) é injetável.
"""
from pathlib import Path


def _eur(cents):
    """Cêntimos inteiros -> euros float (2 casas); None -> 0.0."""
    return 0.0 if cents is None else round(cents / 100, 2)


def _line_net_cents(ln):
    """Total da linha SEM IVA: o extraído ou, na falta dele, derivado do
    total COM IVA e da taxa da linha (sem taxa conhecida, devolve o bruto)."""
    net = ln.get('line_net_cents')
    if net is not None:
        return net
    gross = ln.get('line_total_cents')
    if gross is None:
        return None
    rate = ln.get('iva_rate_pct') or 0.0
    return round(gross / (1 + rate / 100))


def build_payload(file_row, extraction, document_url=None):
    """Payload do webhook Odoo a partir de uma row de `files` (Task 1) e de
    um dict de extração (Task 3, `Extraction.model_dump()`).

    Levanta ValueError sem supplier_key ou invoice_ref.
    """
    vendor = file_row.get('supplier_key')
    if not vendor:
        raise ValueError('fatura sem fornecedor (supplier_key)')
    vendor = vendor.removesuffix('_nc').capitalize()
    ref = extraction.get('invoice_ref')
    if not ref:
        raise ValueError('extração sem invoice_ref')

    lines = extraction.get('lines') or []
    taxes = extraction.get('taxes') or []

    payload = {
        'vendor_name': vendor,
        'invoice_number': ref,
        'total_amount': _eur(extraction.get('total_cents')),
        'vat_amount': _eur(extraction.get('iva_cents')),
        'untaxed_amount': _eur(extraction.get('base_cents')),
        'document_name': Path(file_row['current_path']).name,
        'external_document_id': str(file_row['id']),
        'items': [{
            'supplier_code': ln.get('supplier_code') or '',
            'designation': ln.get('description') or '',
            'unit_price': ln.get('unit_price_eur') or 0.0,
            'quantity': ln.get('quantity') or 0.0,
            'discount': ln.get('discount_pct') or 0.0,
            'total_price': _eur(_line_net_cents(ln)),
            'tax': ln.get('iva_rate_pct') or 0.0,
        } for ln in lines],
        'taxes': [{
            'tax_rate': t.get('rate_pct') or 0.0,
            'base': _eur(t.get('base_cents')),
            'tax_value': _eur(t.get('value_cents')),
        } for t in taxes],
    }
    optional = {
        'customer_NIF': extraction.get('customer_nif'),
        'emission_date': extraction.get('date'),
        'expiration_date': extraction.get('due_date'),
        'md5': file_row.get('md5'),
        'document_url': document_url,
    }
    payload.update({k: v for k, v in optional.items() if v})
    if extraction.get('total_document_cents') is not None:
        payload['total_document'] = _eur(extraction['total_document_cents'])
    if extraction.get('total_vasilhame_cents') is not None:
        payload['total_vasilhame'] = _eur(extraction['total_vasilhame_cents'])
    return payload


def resolve_drive_id(remote_path, run=None):
    """ID Drive de um ficheiro via `rclone lsf --format i`; None em falha."""
    if run is None:
        import subprocess
        run = subprocess.run
    try:
        proc = run(['rclone', 'lsf', '--format', 'i', remote_path],
                   capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip().splitlines()[0]


def drive_preview_url(drive_id):
    """URL embeddable (iframe) e privado do ficheiro no Google Drive."""
    return f'https://drive.google.com/file/d/{drive_id}/preview'


def _post_json(url, body, headers):
    import json
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def send(payload, webhook_url, api_key, poster=None):
    """POST do payload; devolve o dict `result` do Odoo.

    O payload é enviado como corpo JSON simples (o Odoo devolve envelope
    JSON-RPC). RuntimeError num erro JSON-RPC; erros de rede propagam do
    poster.
    """
    url = webhook_url.rstrip('/') + '/webhook/claude/invoice'
    body = {**payload, 'api_key': api_key}
    headers = {'Content-Type': 'application/json', 'X-API-Key': api_key}
    resp = (poster or _post_json)(url, body, headers)
    if resp.get('error'):
        err = resp['error']
        raise RuntimeError((err.get('data') or {}).get('message')
                           or err.get('message') or 'erro JSON-RPC')
    return resp.get('result') or {}
