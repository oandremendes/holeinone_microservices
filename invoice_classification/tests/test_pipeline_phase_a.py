from pathlib import Path

import pytest
import pipeline
import state
from qr import QrHit, parse_payload

PAYLOAD = ('A:504350900*B:508179947*C:PT*D:FT*E:N*F:20260315*'
           'G:FT FT100/011485*H:JFZ4PTHN-011485*N:72.35*O:446.93')

NC_PAYLOAD = ('A:504350900*B:508179947*C:PT*D:NC*E:N*F:20260315*'
              'G:NC NC100/000045*H:JFZ4PTHN-000045*N:72.35*O:446.93')


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


def test_same_md5_supersede_reuses_row(conn, dirs, tmp_path, monkeypatch):
    """Exact same bytes rescanned over a bad original: files.md5 is UNIQUE,
    so the original row must be reused (repointed + requeued), not inserted."""
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit()])
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    first = pipeline.handle_new_file(_pdf(tmp_path, 'a.pdf'), conn, dirs, None)
    state.record_error(conn, first['file_id'], 'x', permanent=True)   # original bad
    out = pipeline.handle_new_file(_pdf(tmp_path, 'b.pdf'), conn, dirs, None)
    assert out['action'] == 'SUPERSEDE'
    assert out['file_id'] == first['file_id']          # row reused, no new insert
    row = conn.execute("SELECT * FROM files WHERE id=?", (first['file_id'],)).fetchone()
    assert row['status'] == 'queued'
    assert row['current_path'] == str(dirs['integrated'] / out['new_name'])
    assert (dirs['integrated'] / out['new_name']).exists()
    assert (dirs['duplicados'] / '20260315_Novadis.pdf').exists()      # old file moved out
    ext = conn.execute("SELECT attempts, last_error FROM extractions WHERE file_id=?",
                       (first['file_id'],)).fetchone()
    assert ext['attempts'] == 0 and ext['last_error'] is None
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM qr_codes WHERE file_id=?",
                        (first['file_id'],)).fetchone()[0] == 1


def test_corrupt_pdf_goes_to_review_and_dedups(conn, dirs, tmp_path):
    """Unreadable PDFs must not error-loop: QR decode failure falls through to
    the OCR fallback, the file lands in REVIEW with a (supersede-able) row."""
    blob = _pdf(tmp_path, 'garbage.pdf', b'this is not a pdf at all')
    out = pipeline.handle_new_file(blob, conn, dirs,
                                   ocr_fallback=lambda p: ('unknown', None))
    assert out['action'] == 'REVIEW'
    assert (dirs['review'] / 'garbage.pdf').exists()
    row = state.find_by_md5(conn, pipeline.file_md5(dirs['review'] / 'garbage.pdf'))
    assert row['status'] == 'review_folder' and row['id_source'] == 'none'
    # same bytes again: md5 dedup finds the row -- no crash, no second row
    blob2 = _pdf(tmp_path, 'garbage2.pdf', b'this is not a pdf at all')
    pipeline.handle_new_file(blob2, conn, dirs,
                             ocr_fallback=lambda p: ('unknown', None))
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM files").fetchone()[0] == 'review_folder'


def test_dry_run_is_read_only(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit()])
    pdf = _pdf(tmp_path)
    out = pipeline.handle_new_file(pdf, conn, dirs, ocr_fallback=None, dry_run=True)
    assert out['action'] == 'INTEGRATED'
    assert out['file_id'] is None
    assert pdf.exists()                     # source file left in place
    assert not list(dirs['integrated'].iterdir())   # nothing moved into place
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 0


def test_dry_run_duplicate_is_read_only(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit()])
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    pipeline.handle_new_file(_pdf(tmp_path, 'a.pdf'), conn, dirs, None)
    dup = _pdf(tmp_path, 'b.pdf')
    out = pipeline.handle_new_file(dup, conn, dirs, None, dry_run=True)
    assert out['action'] == 'DUPLICATE' and out['file_id'] is None
    assert dup.exists()                             # source file left in place
    assert not list(dirs['duplicados'].iterdir())   # nothing moved
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_dry_run_supersede_is_read_only(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit()])
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    first = pipeline.handle_new_file(_pdf(tmp_path, 'a.pdf'), conn, dirs, None)
    state.record_error(conn, first['file_id'], 'x', permanent=True)
    orig_path = conn.execute("SELECT current_path FROM files WHERE id=?",
                             (first['file_id'],)).fetchone()[0]
    dup = _pdf(tmp_path, 'b.pdf', b'%PDF two')
    out = pipeline.handle_new_file(dup, conn, dirs, None, dry_run=True)
    assert out['action'] == 'SUPERSEDE' and out['file_id'] is None
    assert dup.exists()
    assert Path(orig_path).exists()                 # bad original not moved
    assert not list(dirs['duplicados'].iterdir())
    row = conn.execute("SELECT * FROM files WHERE id=?", (first['file_id'],)).fetchone()
    assert row['status'] == 'failed' and row['current_path'] == orig_path
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_dry_run_review_is_read_only(conn, dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [])
    pdf = _pdf(tmp_path)
    out = pipeline.handle_new_file(pdf, conn, dirs,
                                   ocr_fallback=lambda p: ('unknown', None),
                                   dry_run=True)
    assert out['action'] == 'REVIEW' and out['file_id'] is None
    assert pdf.exists()
    assert not list(dirs['review'].iterdir())
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_nc_suffix_not_doubled_when_key_already_ends_nc(conn, dirs, tmp_path, monkeypatch):
    state.seed_suppliers(conn, {'novadis_nc': ('504350900', 'Novadis NC')})
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [_hit(NC_PAYLOAD)])
    out = pipeline.handle_new_file(_pdf(tmp_path), conn, dirs, ocr_fallback=None)
    assert out['action'] == 'INTEGRATED'
    assert out['new_name'].count('_nc') == 1
    assert out['new_name'] == '20260315_Novadis_nc.pdf'
