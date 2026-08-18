from validation import validate

QR = {'A': '504350900', 'G': 'FT FT100/011485', 'H': 'JFZ4PTHN-011485',
      'F': '20260315', 'N': '72.35', 'O': '446.93'}
EXT = {'invoice_ref': 'FT FT100/011485', 'date': '2026-03-15',
       'iva_cents': 7235, 'total_cents': 44693,
       'lines': [{'line_total_cents': 44693}], 'taxes': []}


def test_all_ok():
    v = validate(EXT, QR, 'novadis')
    assert v['status'] == 'ok'
    assert all(v['checks'].values())


def test_tolerance_2c():
    ext = dict(EXT, iva_cents=7236)
    assert validate(ext, QR, 'novadis')['status'] == 'ok'
    ext = dict(EXT, iva_cents=7240)
    v = validate(ext, QR, 'novadis')
    assert v['status'] == 'needs_review' and not v['checks']['iva_vs_qr']


def test_total_mismatch_blocks():
    v = validate(dict(EXT, total_cents=52284), QR, 'novadis')
    assert v['status'] == 'needs_review' and not v['checks']['total_vs_qr']


def test_ref_matches_with_or_without_atcud():
    assert validate(dict(EXT, invoice_ref='FT FT100/011485 / JFZ4PTHN-011485'),
                    QR, None)['checks']['ref_vs_qr']
    assert not validate(dict(EXT, invoice_ref='FT FT100/999999'),
                        QR, None)['checks']['ref_vs_qr']


def test_quirk_iva_doubled():
    q = dict(QR, N='144.70')  # QR reports 2x the real IVA
    v = validate(EXT, q, 'teofilo')
    assert v['checks']['iva_vs_qr'] and 'iva_doubled' in ' '.join(v['notes'])
    assert validate(EXT, q, 'novadis')['status'] == 'needs_review'


def test_quirk_base_in_n():
    q = dict(QR, N='374.58')  # QR N holds the base, not the IVA
    assert validate(EXT, q, 'orientalshopping')['checks']['iva_vs_qr']


def test_quirk_stamp_duty():
    # BCP-like stamp-duty document: QR M (imposto do selo) > 0, N > 0,
    # but the document genuinely carries no IVA -- supplier-independent
    q = dict(QR, M='17.60', N='17.60')
    v = validate(dict(EXT, iva_cents=0), q, 'bcp')
    assert v['checks']['iva_vs_qr'] and 'stamp_duty' in ' '.join(v['notes'])
    assert validate(dict(EXT, iva_cents=None), q, None)['checks']['iva_vs_qr']
    # without M, IVA 0 vs N>0 still fails
    assert not validate(dict(EXT, iva_cents=0), QR, None)['checks']['iva_vs_qr']


def test_vasilhame_total_document():
    # Novadis-like: vasilhame/tara lines at 0% raise the payable total above
    # the merchandise total; QR O carries the payable (document) total
    q = dict(QR, O='451.93')
    ext = dict(EXT, total_document_cents=45193,
               lines=[{'line_total_cents': 44693}, {'line_total_cents': 500}])
    v = validate(ext, q, 'novadis')
    assert v['status'] == 'ok'
    assert v['checks']['total_vs_qr'] and v['checks']['soma_linhas']
    assert 'total_document_cents' in ' '.join(v['notes'])
    # when QR O carries the merchandise total instead, total_cents matches
    v2 = validate(ext, QR, 'novadis')
    assert v2['checks']['total_vs_qr']
    assert 'total_cents' in ' '.join(v2['notes'])
    # neither total near QR O -> fail
    v3 = validate(dict(ext, total_cents=99999, total_document_cents=88888),
                  q, 'novadis')
    assert not v3['checks']['total_vs_qr']


def test_soma_linhas():
    ext = dict(EXT, lines=[{'line_total_cents': 100}])
    v = validate(ext, QR, None)
    assert not v['checks']['soma_linhas'] and v['status'] == 'needs_review'


def test_no_qr_holds():
    v = validate(EXT, None, 'novadis')
    assert v['status'] == 'needs_review'
    assert 'sem QR' in ' '.join(v['notes'])
