import json
import pytest
import pipeline
import state
from qr import QrHit, parse_payload

PAYLOAD = ('A:504350900*B:508179947*C:PT*D:FT*E:N*F:20260315*'
           'G:FT FT100/011485*H:JFZ4PTHN-011485*N:72.35*O:446.93')
GOOD_EXT = {'supplier_name': 'Novadis', 'supplier_nif': '504350900',
            'customer_nif': None, 'invoice_ref': 'FT FT100/011485',
            'date': '2026-03-15', 'due_date': None, 'base_cents': 37458,
            'iva_cents': 7235, 'total_cents': 44693,
            'total_vasilhame_cents': None, 'total_document_cents': None,
            'lines': [{'description': 'x', 'supplier_code': None, 'quantity': 1.0,
                       'unit_price_eur': None, 'discount_pct': None,
                       'iva_rate_pct': 23.0, 'line_net_cents': 37458,
                       'line_total_cents': 44693}],
            'taxes': []}
ODOO = {'webhook_url': 'https://odoo.example', 'api_key': 'K',
        'drive_remote': 'gdrive:ScanSnap'}


@pytest.fixture
def env(conn, tmp_path, monkeypatch):
    dirs = {'integrated': tmp_path / 'INTEGRATED', 'review': tmp_path / 'REVIEW',
            'duplicados': tmp_path / 'Duplicados', 'extracted': tmp_path / 'EXTRACTED'}
    for p in dirs.values():
        p.mkdir()
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    monkeypatch.setattr(pipeline, 'qr_decode',
                        lambda p: [QrHit(1, PAYLOAD, parse_payload(PAYLOAD))])
    src = tmp_path / 'scan.pdf'
    src.write_bytes(b'%PDF x')
    out = pipeline.handle_new_file(src, conn, dirs, None)
    return conn, dirs, out['file_id']


def _extractor(result):
    class E:
        def model_dump(self): return result
    return lambda pdf, supplier: (E(), json.dumps(result))


def test_drain_happy_path_files_sends_writes(env):
    conn, dirs, fid = env
    posts = []
    stats = pipeline.drain(conn, dirs, ODOO, extractor=_extractor(GOOD_EXT),
                           poster=lambda u, b, h: posts.append(b) or {'result': {'id': 9}},
                           resolver=lambda remote: 'DRIVEID')
    assert stats == {'extracted': 1, 'sent': 1, 'needs_review': 0,
                     'retried': 0, 'failed': 0}
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'sent'
    assert '/INTEGRATED/2026/03/' in row['current_path']
    art = json.loads((dirs['extracted'] / '20260315_Novadis.json').read_text())
    assert art['validation']['status'] == 'ok'
    assert art['odoo']['status'] == 'sent'
    assert posts[0]['document_url'] == 'https://drive.google.com/file/d/DRIVEID/preview'
    assert posts[0]['md5'] == row['md5']


def test_drain_gate_holds_mismatch(env):
    conn, dirs, fid = env
    bad = dict(GOOD_EXT, total_cents=52284)
    posts = []
    stats = pipeline.drain(conn, dirs, ODOO, extractor=_extractor(bad),
                           poster=lambda u, b, h: posts.append(b) or {},
                           resolver=lambda r: None)
    assert stats['needs_review'] == 1 and posts == []
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'needs_review'
    assert '/INTEGRATED/2026/' not in row['current_path']   # stays flat
    art = json.loads((dirs['extracted'] / '20260315_Novadis.json').read_text())
    assert art['odoo']['status'] == 'held'


def test_drain_transient_error_retries_then_caps(env):
    conn, dirs, fid = env
    def boom(pdf, supplier):
        raise RuntimeError('api down')
    for i in range(5):
        stats = pipeline.drain(conn, dirs, ODOO, extractor=boom,
                               poster=lambda *a: {}, resolver=lambda r: None)
    row = conn.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'failed'
    assert pipeline.drain(conn, dirs, ODOO, extractor=boom, poster=lambda *a: {},
                          resolver=lambda r: None)['retried'] == 0  # not picked up


def test_drain_refusal_fails_immediately(env):
    conn, dirs, fid = env
    import claude_extract
    def refuse(pdf, supplier):
        raise claude_extract.PermanentExtractionError('refusal')
    pipeline.drain(conn, dirs, ODOO, extractor=refuse, poster=lambda *a: {},
                   resolver=lambda r: None)
    assert conn.execute("SELECT status FROM files WHERE id=?",
                        (fid,)).fetchone()[0] == 'failed'


def test_drain_odoo_error_is_transient(env):
    conn, dirs, fid = env
    def bad_poster(u, b, h):
        raise OSError('network')
    pipeline.drain(conn, dirs, ODOO, extractor=_extractor(GOOD_EXT),
                   poster=bad_poster, resolver=lambda r: None)
    row = conn.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'retry'   # extraction succeeded but send failed -> retry later
