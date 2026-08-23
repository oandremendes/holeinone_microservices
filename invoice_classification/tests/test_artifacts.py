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


def test_artifact_carries_md5():
    # o md5 é a identidade estável do documento: o QA_Faturas emparelha
    # artefacto <-> fatura por ele (nomes de ficheiro colidem)
    import artifacts
    art = artifacts.build_artifact(
        {'current_path': '/x/ScanSnap/INTEGRATED/a.pdf', 'md5': 'abc123',
         'supplier_key': 's', 'nif': None, 'doc_date': None, 'doc_type': None,
         'id_source': 'qr'},
        None, {}, {'status': 'ok'}, {'sent_at': None, 'status': None}, 'm', 1)
    assert art['md5'] == 'abc123'
