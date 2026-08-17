import pytest
import pipeline
import state
from qr import QrHit, parse_payload

PAYLOAD = ('A:504350900*B:508179947*C:PT*D:FT*E:N*F:20260315*'
           'G:FT FT100/011485*H:JFZ4PTHN-011485*N:72.35*O:446.93')


@pytest.fixture
def dirs(tmp_path):
    d = {'integrated': tmp_path / 'INTEGRATED', 'review': tmp_path / 'REVIEW',
         'duplicados': tmp_path / 'Duplicados'}
    for p in d.values():
        p.mkdir()
    return d


def _hit(payload=PAYLOAD):
    return QrHit(page=1, raw=payload, fields=parse_payload(payload))


def _pdf(tmp_path, name='scan001.pdf', content=b'%PDF x'):
    p = tmp_path / name
    p.write_bytes(content)
    return p


def test_qr_identified_moves_and_queues(conn, dirs, tmp_path, monkeypatch):
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit()])
    pdf = _pdf(tmp_path)
    out = pipeline.handle_new_file(pdf, conn, dirs, ocr_fallback=None)
    assert out['action'] == 'INTEGRATED'
    assert out['new_name'] == '20260315_Novadis.pdf'
    assert (dirs['integrated'] / '20260315_Novadis.pdf').exists()
    row = state.find_by_md5(conn, pipeline.file_md5(dirs['integrated'] / '20260315_Novadis.pdf'))
    assert row['status'] == 'queued' and row['id_source'] == 'qr'
    assert row['atcud'] == 'JFZ4PTHN-011485' and row['doc_type'] == 'FT'


def test_auto_register_unknown_nif(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit()])
    out = pipeline.handle_new_file(_pdf(tmp_path), conn, dirs, ocr_fallback=None)
    assert out['action'] == 'INTEGRATED'
    assert state.supplier_for_nif(conn, '504350900')['auto_registered'] == 1


def test_ocr_fallback_on_qr_miss(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [])
    out = pipeline.handle_new_file(_pdf(tmp_path), conn, dirs,
                                   ocr_fallback=lambda p: ('teofilo', '20260120'))
    assert out['action'] == 'INTEGRATED'
    assert out['new_name'] == '20260120_Teofilo.pdf'
    row = state.find_by_md5(conn, pipeline.file_md5(dirs['integrated'] / out['new_name']))
    assert row['id_source'] == 'ocr' and row['status'] == 'queued'


def test_unknown_goes_to_review(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [])
    out = pipeline.handle_new_file(_pdf(tmp_path), conn, dirs,
                                   ocr_fallback=lambda p: ('unknown', None))
    assert out['action'] == 'REVIEW'
    assert (dirs['review'] / 'scan001.pdf').exists()
    row = state.find_by_md5(conn, pipeline.file_md5(dirs['review'] / 'scan001.pdf'))
    assert row['status'] == 'review_folder' and row['id_source'] == 'none'


def test_md5_duplicate_moves_to_duplicados(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit()])
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    pipeline.handle_new_file(_pdf(tmp_path, 'a.pdf'), conn, dirs, None)
    out = pipeline.handle_new_file(_pdf(tmp_path, 'b.pdf'), conn, dirs, None)
    assert out['action'] == 'DUPLICATE'
    assert (dirs['duplicados'] / 'b.pdf').exists()


def test_qr_identity_duplicate_different_bytes(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit()])
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    pipeline.handle_new_file(_pdf(tmp_path, 'a.pdf', b'%PDF one'), conn, dirs, None)
    out = pipeline.handle_new_file(_pdf(tmp_path, 'b.pdf', b'%PDF two'), conn, dirs, None)
    assert out['action'] == 'DUPLICATE'


def test_rescan_supersedes_failed_original(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit()])
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    first = pipeline.handle_new_file(_pdf(tmp_path, 'a.pdf', b'%PDF one'), conn, dirs, None)
    state.record_error(conn, first['file_id'], 'x', permanent=True)   # original bad
    out = pipeline.handle_new_file(_pdf(tmp_path, 'b.pdf', b'%PDF two'), conn, dirs, None)
    assert out['action'] == 'SUPERSEDE'
    old = conn.execute("SELECT * FROM files WHERE id=?", (first['file_id'],)).fetchone()
    assert old['status'] == 'superseded' and old['superseded_by'] == out['file_id']
    assert (dirs['duplicados'] / '20260315_Novadis.pdf').exists()  # old file moved out
    new_row = conn.execute("SELECT * FROM files WHERE id=?", (out['file_id'],)).fetchone()
    assert new_row['status'] == 'queued'
