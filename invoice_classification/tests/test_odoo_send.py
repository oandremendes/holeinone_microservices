import pytest
import odoo_send

FILE_ROW = {'id': 7, 'md5': 'abc', 'supplier_key': 'teofilo_nc',
            'current_path': '/x/INTEGRATED/2026/03/20260315_Teofilo_nc.pdf'}
EXT = {'invoice_ref': 'C CAAU/022263', 'date': '2026-07-21', 'due_date': None,
       'customer_nif': '508179947', 'base_cents': 42000, 'iva_cents': 0,
       'total_cents': 42000, 'total_vasilhame_cents': None,
       'total_document_cents': None,
       'lines': [{'description': 'Barril', 'supplier_code': 'B1',
                  'quantity': 2.0, 'unit_price_eur': 10.5, 'discount_pct': None,
                  'iva_rate_pct': 0.0, 'line_net_cents': 42000,
                  'line_total_cents': 42000}],
       'taxes': [{'rate_pct': 0.0, 'base_cents': 42000, 'value_cents': 0}]}


def test_build_payload():
    p = odoo_send.build_payload(FILE_ROW, EXT, document_url='https://d/p')
    assert p['vendor_name'] == 'Teofilo'          # _nc stripped, capitalized
    assert p['invoice_number'] == 'C CAAU/022263'
    assert p['total_amount'] == 420.0 and p['vat_amount'] == 0.0
    assert p['document_name'] == '20260315_Teofilo_nc.pdf'
    assert p['external_document_id'] == '7' and p['md5'] == 'abc'
    assert p['items'][0]['designation'] == 'Barril'
    assert p['document_url'] == 'https://d/p'


def test_build_payload_requires_ref():
    with pytest.raises(ValueError):
        odoo_send.build_payload(FILE_ROW, dict(EXT, invoice_ref=None))


def test_send_ok_and_error():
    seen = {}
    def poster(url, body, headers):
        seen.update(url=url, body=body)
        return {'result': {'ok': 1}}
    r = odoo_send.send({'a': 1}, 'https://odoo.example', 'KEY', poster=poster)
    assert r == {'ok': 1}
    assert seen['url'].endswith('/webhook/claude/invoice')
    assert seen['body']['api_key'] == 'KEY'
    with pytest.raises(RuntimeError):
        odoo_send.send({}, 'https://odoo.example', 'KEY',
                       poster=lambda *a: {'error': {'message': 'nope'}})


def test_resolve_drive_id_passes_flags():
    seen = {}
    def run(cmd, **kw):
        seen['cmd'] = cmd
        class P: returncode, stdout = 0, 'FILEID123\n'
        return P()
    fid = odoo_send.resolve_drive_id('gdrive:ScanSnap/x.pdf', run=run,
                                     flags=['--drive-shared-with-me'])
    assert fid == 'FILEID123'
    assert seen['cmd'][:2] == ['rclone', 'lsf']
    assert '--drive-shared-with-me' in seen['cmd']
    assert seen['cmd'][-1] == 'gdrive:ScanSnap/x.pdf'


def test_resolve_drive_id_no_flags_backcompat():
    seen = {}
    def run(cmd, **kw):
        seen['cmd'] = cmd
        class P: returncode, stdout = 0, 'ID\n'
        return P()
    assert odoo_send.resolve_drive_id('gdrive:ScanSnap/x.pdf', run=run) == 'ID'
    assert '--drive-shared-with-me' not in seen['cmd']
