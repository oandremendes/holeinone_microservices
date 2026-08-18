import classifier as clf
import pipeline
import state
import api_config


class FakeClassifier:
    def classify(self, pdf_path):
        from classifier import ClassificationResult
        return ClassificationResult(supplier='teofilo', confidence=0.95,
                                    method='ocr', details={},
                                    invoice_date='20260120')


def test_process_and_move_enqueues_via_pipeline(conn, tmp_path, monkeypatch):
    src = tmp_path / 'ScanSnap'
    src.mkdir()
    (src / 'scan.pdf').write_bytes(b'%PDF x')
    dirs = {'integrated': src / 'INTEGRATED', 'review': src / 'REVIEW',
            'duplicados': tmp_path / 'Duplicados', 'extracted': src / 'EXTRACTED'}
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [])
    stats = clf.process_and_move(FakeClassifier(), src, src / 'MATCHED',
                                 dirs['review'], dirs['integrated'],
                                 conn=conn, dirs_extra=dirs)
    assert stats['integrated'] == 1
    assert (dirs['integrated'] / '20260120_Teofilo.pdf').exists()
    assert len(state.pending(conn)) == 1


def test_legacy_upload_not_called_by_default(conn, tmp_path, monkeypatch):
    src = tmp_path / 'ScanSnap'
    src.mkdir()
    (src / 'scan.pdf').write_bytes(b'%PDF x')
    called = []
    monkeypatch.setattr(clf, 'upload_to_api', lambda *a: called.append(a) or
                        {'success': True})
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [])
    dirs = {'integrated': src / 'INTEGRATED', 'review': src / 'REVIEW',
            'duplicados': tmp_path / 'Duplicados', 'extracted': src / 'EXTRACTED'}
    clf.process_and_move(FakeClassifier(), src, src / 'MATCHED', dirs['review'],
                         dirs['integrated'], upload=True, conn=conn, dirs_extra=dirs)
    assert called == []   # neutralized: legacy_apis_enabled() is False by default


def test_run_drain_invokes_pipeline_drain(tmp_path, monkeypatch):
    """C3: the standalone drain codepath (classifier.py drain) calls
    pipeline.drain once with the configured Odoo settings."""
    calls = []
    monkeypatch.setattr(pipeline, 'drain',
                        lambda conn, dirs, cfg, **kw: calls.append((dirs, cfg, kw))
                        or {'sent': 0})
    real_connect = state.connect
    monkeypatch.setattr(state, 'connect',
                        lambda p: real_connect(tmp_path / 'state.db'))
    odoo = {'webhook_url': 'u', 'api_key': 'k', 'drive_remote': 'g:ScanSnap'}
    monkeypatch.setattr(api_config, 'get_odoo', lambda *a, **k: odoo)
    stats = clf.run_drain(dry_run=True)
    assert stats == {'sent': 0}
    assert len(calls) == 1
    assert calls[0][1] is odoo
    assert calls[0][2].get('dry_run') is True


def test_legacy_upload_uses_real_supplier_key(conn, tmp_path, monkeypatch):
    src = tmp_path / 'ScanSnap'
    src.mkdir()
    (src / 'scan.pdf').write_bytes(b'%PDF x')
    called = []
    monkeypatch.setattr(clf, 'upload_to_api', lambda *a: called.append(a) or
                        {'success': True})
    monkeypatch.setattr(pipeline, 'qr_decode', lambda p: [])
    monkeypatch.setattr(api_config, 'legacy_apis_enabled', lambda *a, **k: True)
    dirs = {'integrated': src / 'INTEGRATED', 'review': src / 'REVIEW',
            'duplicados': tmp_path / 'Duplicados', 'extracted': src / 'EXTRACTED'}
    clf.process_and_move(FakeClassifier(), src, src / 'MATCHED', dirs['review'],
                         dirs['integrated'], upload=True, conn=conn, dirs_extra=dirs)
    assert len(called) == 1
    assert called[0][1] == 'teofilo'
