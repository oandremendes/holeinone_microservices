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
    source = tmp_path / 'ScanSnap'
    source.mkdir()
    dirs = pipeline.dirs_for_source(source)
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    monkeypatch.setattr(pipeline, 'qr_decode',
                        lambda p: [QrHit(1, PAYLOAD, parse_payload(PAYLOAD))])
    src = source / 'scan.pdf'
    src.write_bytes(b'%PDF x')
    out = pipeline.handle_new_file(src, conn, dirs, None)
    return conn, dirs, out['file_id']


def _extractor(result):
    class E:
        def model_dump(self): return result
    return lambda pdf, supplier: (E(), json.dumps(result), 'claude-opus-4-8')


def test_drain_happy_path_files_sends_writes(env):
    conn, dirs, fid = env
    posts = []
    stats = pipeline.drain(conn, dirs, ODOO, extractor=_extractor(GOOD_EXT),
                           poster=lambda u, b, h: posts.append(b) or {'result': {'id': 9}},
                           resolver=lambda remote: 'DRIVEID')
    assert stats == {'extracted': 1, 'sent': 1, 'needs_review': 0,
                     'retried': 0, 'failed': 0, 'approved': 0, 'retired': 0}
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'sent'
    assert '/INTEGRATED/2026/03/' in row['current_path']
    art = json.loads((dirs['extracted'] / '20260315_Novadis.json').read_text())
    assert art['validation']['status'] == 'ok'
    assert art['odoo']['status'] == 'sent'
    assert art['model'] == 'claude-opus-4-8'  # served model, not the constant
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


def test_drain_odoo_retry_reuses_extraction_and_filing(env):
    """I2: an odoo/filing retry must not re-run Claude nor re-file the PDF."""
    conn, dirs, fid = env
    calls = []
    def extractor(pdf, sup):
        calls.append(pdf)
        return _extractor(GOOD_EXT)(pdf, sup)
    posts = {'n': 0}
    def poster(u, b, h):
        posts['n'] += 1
        if posts['n'] == 1:
            raise OSError('network')
        return {'result': {'id': 9}}
    pipeline.drain(conn, dirs, ODOO, extractor=extractor, poster=poster,
                   resolver=lambda r: 'D')
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'retry'
    filed = row['current_path']
    assert '/INTEGRATED/2026/03/' in filed
    stats = pipeline.drain(conn, dirs, ODOO, extractor=extractor, poster=poster,
                           resolver=lambda r: 'D')
    assert stats['sent'] == 1
    assert len(calls) == 1                       # extractor ran only once
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    assert row['current_path'] == filed          # no _1 rename cascade
    assert row['status'] == 'sent'


def test_drain_derives_dirs_per_row(conn, tmp_path, monkeypatch):
    """C2: rows queued under different source roots are filed into their own
    roots, regardless of which folder the drain invocation targeted."""
    root1 = tmp_path / 'ScanSnap'
    root2 = root1 / 'Receipts'
    d1 = pipeline.dirs_for_source(root1)
    d2 = pipeline.dirs_for_source(root2)
    for d in (*d1.values(), *d2.values()):
        d.mkdir(parents=True, exist_ok=True)
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis'),
                                'teofilo': ('500099871', 'Teofilo')})
    monkeypatch.setattr(pipeline, 'qr_decode',
                        lambda p: [QrHit(1, PAYLOAD, parse_payload(PAYLOAD))])
    s1 = root1 / 'a.pdf'
    s1.write_bytes(b'%PDF one')
    pipeline.handle_new_file(s1, conn, d1, None)
    p2 = PAYLOAD.replace('504350900', '500099871')
    monkeypatch.setattr(pipeline, 'qr_decode',
                        lambda p: [QrHit(1, p2, parse_payload(p2))])
    s2 = root2 / 'b.pdf'
    s2.write_bytes(b'%PDF two')
    pipeline.handle_new_file(s2, conn, d2, None)
    def extractor(pdf, sup):
        return _extractor(GOOD_EXT)(pdf, sup)
    # drain invoked with root2's dirs -- each row still files into its own root
    stats = pipeline.drain(conn, d2, ODOO, extractor=extractor,
                           poster=lambda u, b, h: {'result': {'id': 1}},
                           resolver=lambda r: 'D')
    assert stats['sent'] == 2
    assert (root1 / 'INTEGRATED' / '2026' / '03' / '20260315_Novadis.pdf').exists()
    assert (root2 / 'INTEGRATED' / '2026' / '03' / '20260315_Teofilo.pdf').exists()
    # EXTRACTED always derives from the ScanSnap root
    assert (d1['extracted'] / '20260315_Novadis.json').exists()
    assert (d1['extracted'] / '20260315_Teofilo.json').exists()


def test_artifact_name_follows_filed_pdf_name(conn, tmp_path, monkeypatch):
    """I1: two invoices with the same flat stem keep distinct EXTRACTED
    artifacts matching their final (post-filing, _N-suffixed) PDF names."""
    source = tmp_path / 'ScanSnap'
    source.mkdir()
    dirs = pipeline.dirs_for_source(source)
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    ok_poster = lambda u, b, h: {'result': {'id': 1}}
    monkeypatch.setattr(pipeline, 'qr_decode',
                        lambda p: [QrHit(1, PAYLOAD, parse_payload(PAYLOAD))])
    s1 = source / 's1.pdf'
    s1.write_bytes(b'%PDF one')
    pipeline.handle_new_file(s1, conn, dirs, None)
    pipeline.drain(conn, dirs, ODOO, extractor=_extractor(GOOD_EXT),
                   poster=ok_poster, resolver=lambda r: 'D1')
    # second document, same supplier+date -> same flat stem after Phase A
    p2 = PAYLOAD.replace('011485', '022222')
    ext2 = dict(GOOD_EXT, invoice_ref='FT FT100/022222')
    monkeypatch.setattr(pipeline, 'qr_decode',
                        lambda p: [QrHit(1, p2, parse_payload(p2))])
    s2 = source / 's2.pdf'
    s2.write_bytes(b'%PDF two')
    pipeline.handle_new_file(s2, conn, dirs, None)
    pipeline.drain(conn, dirs, ODOO, extractor=_extractor(ext2),
                   poster=ok_poster, resolver=lambda r: 'D2')
    month = dirs['integrated'] / '2026' / '03'
    assert (month / '20260315_Novadis.pdf').exists()
    assert (month / '20260315_Novadis_1.pdf').exists()
    a1 = json.loads((dirs['extracted'] / '20260315_Novadis.json').read_text())
    a2 = json.loads((dirs['extracted'] / '20260315_Novadis_1.json').read_text())
    assert a1['pdf'].endswith('2026/03/20260315_Novadis.pdf')
    assert a2['pdf'].endswith('2026/03/20260315_Novadis_1.pdf')


def test_no_qr_hold_prefills_extracted_date(conn, tmp_path, monkeypatch):
    # No decodable QR: the invoice is held for manual (second) approval, but
    # when the extraction read a date the file is renamed 2026XXXX_ ->
    # YYYYMMDD_ and doc_date stored, so the QA queue is organized by date.
    source = tmp_path / 'ScanSnap'
    source.mkdir()
    dirs = pipeline.dirs_for_source(source)
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    state.seed_suppliers(conn, {'reichurrasco': ('515553565', 'Rei do Churrasco')})
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [])
    src = source / 'scan01.pdf'
    src.write_bytes(b'%PDF norqr')
    out = pipeline.handle_new_file(src, conn, dirs, lambda p: ('reichurrasco', None))
    assert out['new_name'].startswith('2026XXXX_')
    ext = dict(GOOD_EXT, supplier_nif='515553565', date='2026-05-17')
    stats = pipeline.drain(conn, dirs, ODOO, extractor=_extractor(ext),
                           poster=lambda u, b, h: {'result': {}},
                           resolver=lambda remote: None)
    assert stats['needs_review'] == 1
    row = conn.execute("SELECT * FROM files WHERE id=?", (out['file_id'],)).fetchone()
    assert row['status'] == 'needs_review'
    assert row['doc_date'] == '2026-05-17'
    assert row['current_path'].endswith('/INTEGRATED/20260517_Reichurrasco.pdf')
    # the artifact follows the new name
    assert (dirs['extracted'] / '20260517_Reichurrasco.json').exists()
    # a QR-carrying hold (validation mismatch) is never renamed
    src2 = source / 'scan02.pdf'
    src2.write_bytes(b'%PDF y')
    monkeypatch.setattr(pipeline, 'qr_decode',
                        lambda p: [QrHit(1, PAYLOAD, parse_payload(PAYLOAD))])
    out2 = pipeline.handle_new_file(src2, conn, dirs, None)
    bad = dict(GOOD_EXT, total_cents=99999, date='2026-03-16')
    pipeline.drain(conn, dirs, ODOO, extractor=_extractor(bad),
                   poster=lambda u, b, h: {'result': {}},
                   resolver=lambda remote: None)
    row2 = conn.execute("SELECT * FROM files WHERE id=?", (out2['file_id'],)).fetchone()
    assert row2['current_path'].endswith(out2['new_name'])


def test_no_qr_hold_without_date_keeps_name(conn, tmp_path, monkeypatch):
    source = tmp_path / 'ScanSnap'
    source.mkdir()
    dirs = pipeline.dirs_for_source(source)
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    state.seed_suppliers(conn, {'reichurrasco': ('515553565', 'Rei do Churrasco')})
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [])
    src = source / 'scan01.pdf'
    src.write_bytes(b'%PDF norqr')
    out = pipeline.handle_new_file(src, conn, dirs, lambda p: ('reichurrasco', None))
    ext = dict(GOOD_EXT, date=None)
    pipeline.drain(conn, dirs, ODOO, extractor=_extractor(ext),
                   poster=lambda u, b, h: {'result': {}},
                   resolver=lambda remote: None)
    row = conn.execute("SELECT * FROM files WHERE id=?", (out['file_id'],)).fetchone()
    assert '2026XXXX_' in row['current_path'] and row['doc_date'] is None


def _held_env(conn, tmp_path, monkeypatch, ext_overrides=None):
    source = tmp_path / 'ScanSnap'
    source.mkdir(exist_ok=True)
    dirs = pipeline.dirs_for_source(source)
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    state.seed_suppliers(conn, {'reichurrasco': ('515553565', 'Rei do Churrasco')})
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [])
    src = source / 'scan01.pdf'
    src.write_bytes(b'%PDF norqr')
    out = pipeline.handle_new_file(src, conn, dirs, lambda p: ('reichurrasco', None))
    ext = dict(GOOD_EXT, supplier_nif='515553565', date='2026-05-17',
               **(ext_overrides or {}))
    pipeline.drain(conn, dirs, ODOO, extractor=_extractor(ext),
                   poster=lambda u, b, h: {'result': {}},
                   resolver=lambda remote: None)
    return dirs, out['file_id']


def test_approval_marker_releases_held_invoice(conn, tmp_path, monkeypatch):
    dirs, fid = _held_env(conn, tmp_path, monkeypatch)
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'needs_review'
    marker = dirs['approved'] / f"{row['md5']}.json"
    marker.write_text(json.dumps({
        'md5': row['md5'], 'pdf': 'INTEGRATED/20260517_Reichurrasco.pdf',
        'date': '2026-05-18', 'supplier': 'Reichurrasco',
        'approved_at': '2026-08-24T10:00:00'}))
    posts = []
    stats = pipeline.drain(conn, dirs, ODOO, extractor=None,
                           poster=lambda u, b, h: posts.append(b) or {'result': {'id': 5}},
                           resolver=lambda remote: 'DRIVEID')
    assert stats['approved'] == 1
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'sent'
    # human-corrected date wins: filing folder and payload
    assert '/INTEGRATED/2026/05/' in row['current_path']
    assert posts[0]['emission_date'] == '2026-05-18'
    assert not marker.exists()
    art = json.loads((dirs['extracted'] / '20260517_Reichurrasco.json').read_text())
    assert art['odoo']['status'] == 'sent'
    assert any('aprovada manualmente' in n for n in art['validation']['notes'])


def test_approval_marker_kept_on_transient_odoo_error(conn, tmp_path, monkeypatch):
    dirs, fid = _held_env(conn, tmp_path, monkeypatch)
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    marker = dirs['approved'] / f"{row['md5']}.json"
    marker.write_text(json.dumps({'md5': row['md5'],
                                  'pdf': 'INTEGRATED/20260517_Reichurrasco.pdf'}))
    def bad_poster(u, b, h):
        raise OSError('down')
    stats = pipeline.drain(conn, dirs, ODOO, extractor=None, poster=bad_poster,
                           resolver=lambda remote: None)
    assert stats['approved'] == 0
    assert marker.exists()   # retried on the next tick
    assert conn.execute("SELECT status FROM files WHERE id=?",
                        (fid,)).fetchone()[0] == 'needs_review'


def test_approval_marker_for_unknown_md5_is_dropped(conn, tmp_path, monkeypatch):
    dirs, fid = _held_env(conn, tmp_path, monkeypatch)
    marker = dirs['approved'] / 'ffff.json'
    marker.write_text(json.dumps({'md5': 'ffff', 'pdf': 'INTEGRATED/x.pdf'}))
    pipeline.drain(conn, dirs, ODOO, extractor=None,
                   poster=lambda u, b, h: {'result': {}},
                   resolver=lambda remote: None)
    assert not marker.exists()


def test_approval_marker_overrides_header_values(conn, tmp_path, monkeypatch):
    # extraction misread the IVA (held: iva_vs_qr); the human resolves the
    # correct values at approval time and they reach the Odoo payload
    dirs, fid = _held_env(conn, tmp_path, monkeypatch)
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    marker = dirs['approved'] / f"{row['md5']}.json"
    marker.write_text(json.dumps({
        'md5': row['md5'], 'pdf': 'INTEGRATED/20260517_Reichurrasco.pdf',
        'overrides': {'iva_cents': 1763, 'total_cents': 2061,
                      'invoice_ref': 'FT 101/00093037',
                      'lines': [{'not': 'supported yet'}]}}))
    posts = []
    stats = pipeline.drain(conn, dirs, ODOO, extractor=None,
                           poster=lambda u, b, h: posts.append(b) or {'result': {}},
                           resolver=lambda remote: None)
    assert stats['approved'] == 1
    assert posts[0]['vat_amount'] == 17.63
    assert posts[0]['total_amount'] == 20.61
    assert posts[0]['invoice_number'] == 'FT 101/00093037'
    art = json.loads((dirs['extracted'] / '20260517_Reichurrasco.json').read_text())
    notes = ' '.join(art['validation']['notes'])
    assert 'corrigidos na aprova' in notes and 'iva_cents' in notes
    # unsupported keys are ignored gracefully and reported, not fatal
    assert 'lines' in notes and 'não suportad' in notes
    assert art['extraction']['iva_cents'] == 1763


def test_nao_fatura_marker_retires_and_moves(conn, tmp_path, monkeypatch):
    # QA judged the document "not an invoice": the pipeline retires the held
    # row and moves the PDF out of the flow into ScanSnap/QA/Nao Fatura/
    dirs, fid = _held_env(conn, tmp_path, monkeypatch)
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    marker = dirs['approved'] / f"{row['md5']}.json"
    marker.write_text(json.dumps({
        'action': 'nao_fatura', 'md5': row['md5'],
        'pdf': 'INTEGRATED/20260517_Reichurrasco.pdf',
        'target': 'QA/Nao Fatura/20260517_Reichurrasco.pdf'}))
    posts = []
    stats = pipeline.drain(conn, dirs, ODOO, extractor=None,
                           poster=lambda u, b, h: posts.append(b) or {'result': {}},
                           resolver=lambda remote: None)
    assert stats['retired'] == 1 and not posts        # nothing goes to Odoo
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'nao_fatura'
    assert row['current_path'].endswith('/QA/Nao Fatura/20260517_Reichurrasco.pdf')
    source = tmp_path / 'ScanSnap'
    assert (source / 'QA' / 'Nao Fatura' / '20260517_Reichurrasco.pdf').exists()
    assert not (source / 'INTEGRATED' / '20260517_Reichurrasco.pdf').exists()
    assert not marker.exists()


def test_nao_fatura_marker_never_touches_sent_rows(env):
    conn, dirs, fid = env
    pipeline.drain(conn, dirs, ODOO, extractor=_extractor(GOOD_EXT),
                   poster=lambda u, b, h: {'result': {}},
                   resolver=lambda remote: None)
    row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    assert row['status'] == 'sent'
    marker = dirs['approved'] / f"{row['md5']}.json"
    marker.write_text(json.dumps({'action': 'nao_fatura', 'md5': row['md5'],
                                  'pdf': 'x', 'target': 'QA/Nao Fatura/x.pdf'}))
    stats = pipeline.drain(conn, dirs, ODOO, extractor=None,
                           poster=lambda u, b, h: {'result': {}},
                           resolver=lambda remote: None)
    assert stats['retired'] == 0
    assert conn.execute("SELECT status FROM files WHERE id=?",
                        (fid,)).fetchone()[0] == 'sent'
    assert not marker.exists()   # orphan dropped with a warning
