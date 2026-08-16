import json
import artifacts


def test_write_json_atomic(tmp_path):
    p = artifacts.write_json(tmp_path, '20260315_Novadis', {'x': 1})
    assert p == tmp_path / '20260315_Novadis.json'
    assert json.loads(p.read_text()) == {'x': 1}
    assert not list(tmp_path.glob('*.tmp'))


def test_build_artifact_shape():
    row = {'id': 1, 'supplier_key': 'novadis', 'nif': '504350900',
           'doc_date': '2026-03-15', 'doc_type': 'FT', 'id_source': 'qr',
           'current_path': '/g/ScanSnap/INTEGRATED/2026/03/20260315_Novadis.pdf'}
    a = artifacts.build_artifact(
        row, {'A': '504350900'}, {'total_cents': 1},
        {'status': 'ok', 'checks': {}, 'notes': []},
        {'sent_at': 't', 'status': 'sent'}, 'claude-opus-5', 1)
    assert a['identification']['supplier_key'] == 'novadis'
    assert a['identification']['source'] == 'qr'
    assert a['pdf'].endswith('INTEGRATED/2026/03/20260315_Novadis.pdf')
    assert a['extraction'] == {'total_cents': 1}
    assert a['odoo']['status'] == 'sent'
    assert a['model'] == 'claude-opus-5' and a['attempts'] == 1
