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


def test_quirk_base_in_n_goldenmarina():
    # GoldenMarina POS puts the base in QR N (total 20.61, IVA 2.98, N=17.63)
    q = dict(QR, N='17.63', O='20.61')
    ext = dict(EXT, iva_cents=298, total_cents=2061,
               lines=[{'line_total_cents': 2061}])
    assert validate(ext, q, 'goldenmarina')['checks']['iva_vs_qr']


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


def test_soma_linhas_tolerates_per_line_rounding():
    # Real Soares invoice: net lines 17.50 + 9.95 = 27.45 = base; the gross
    # per line rounds to 21.53 + 12.24 = 33.77 while the document total is
    # 33.76 (QR O). Per-line VAT rounding must not hold a validated invoice.
    q = dict(QR, N='6.31', O='33.76')
    ext = dict(EXT, iva_cents=631, total_cents=3376, base_cents=2745,
               lines=[{'line_net_cents': 1750, 'line_total_cents': 2153,
                       'iva_rate_pct': 23.0},
                      {'line_net_cents': 995, 'line_total_cents': 1224,
                       'iva_rate_pct': 23.0}])
    v = validate(ext, q, 'soares')
    assert v['checks']['soma_linhas'] and v['status'] == 'ok'
    # a missing line (whole-line amounts) still fails
    v2 = validate(dict(ext, lines=ext['lines'][:1]), q, 'soares')
    assert not v2['checks']['soma_linhas']


def test_soma_linhas_net_sum_with_vasilhame():
    # Teofilo: merchandise base 231.39 + tara 48.00 (0%, net == gross);
    # gross line sum 331.05 vs document total 331.06 (rounding)
    q = dict(QR, N='51.67', O='331.06')
    ext = dict(EXT, iva_cents=5167, total_cents=28306, base_cents=23139,
               total_document_cents=33106, total_vasilhame_cents=4800,
               lines=[{'line_net_cents': 23139, 'line_total_cents': 28305,
                       'iva_rate_pct': 23.0},
                      {'line_net_cents': 4800, 'line_total_cents': 4800,
                       'iva_rate_pct': None}])
    v = validate(ext, q, 'teofilo')
    assert v['checks']['soma_linhas'] and v['status'] == 'ok'


def test_ref_vs_qr_token_order_insensitive():
    # Garcias prints "FT FAL 5307/2026" while the QR G carries
    # "FT FAL.2026/5307": same tokens, different order/punctuation
    q = dict(QR, G='FT FAL.2026/5307', H='J6T227PR-5307')
    ok = validate(dict(EXT, invoice_ref='FT FAL 5307/2026'), q, 'garcias')
    assert ok['checks']['ref_vs_qr']
    # different number -> still a mismatch
    bad = validate(dict(EXT, invoice_ref='FT FAL 5308/2026'), q, 'garcias')
    assert not bad['checks']['ref_vs_qr']
    # leading zeros in numeric tokens are ignored, letters are not
    assert validate(dict(EXT, invoice_ref='FT FT100/11485'), QR,
                    None)['checks']['ref_vs_qr']


def test_ref_vs_qr_missing_prefix_token_ok_but_wrong_token_not():
    # Garcias sometimes prints "FAL 4138/2026" for QR G "FT FAL.2026/4138"
    q = dict(QR, G='FT FAL.2026/4138', H='J6T227PR-4138')
    assert validate(dict(EXT, invoice_ref='FAL 4138/2026'), q, 'garcias')['checks']['ref_vs_qr']
    # a different letter token (NC vs FT) or number is still a mismatch
    assert not validate(dict(EXT, invoice_ref='NC FAL 4138/2026'), q, 'garcias')['checks']['ref_vs_qr']
    assert not validate(dict(EXT, invoice_ref='FAL 4138/2025'), q, 'garcias')['checks']['ref_vs_qr']
    assert not validate(dict(EXT, invoice_ref='4138'), q, 'garcias')['checks']['ref_vs_qr']


def test_base_mais_iva_consistency():
    # Real Garcias case: total 146.12 / IVA 27.32 match the QR, lines sum to
    # the total, but Claude read base 114.69 (should be 118.80): base+IVA
    # must equal the total (or the document total for vasilhame invoices)
    q = dict(QR, N='27.32', O='146.12', G='FT FAL.2026/4138', H='J6T227PR-4138')
    ext = dict(EXT, invoice_ref='FT FAL.2026/4138', iva_cents=2732,
               total_cents=14612, base_cents=11469,
               lines=[{'line_total_cents': 14612, 'line_net_cents': 11880}])
    v = validate(ext, q, 'garcias')
    assert not v['checks']['base_mais_iva'] and v['status'] == 'needs_review'
    assert validate(dict(ext, base_cents=11880), q, 'garcias')['status'] == 'ok'
    # Novadis-style: base includes 0% vasilhame, so base+IVA = document total
    q2 = dict(QR, N='80.94', O='522.84')
    ext2 = dict(EXT, iva_cents=8094, total_cents=43284, base_cents=44190,
                total_document_cents=52284, total_vasilhame_cents=9000,
                lines=[{'line_total_cents': 52284, 'line_net_cents': 44190}])
    assert validate(ext2, q2, 'novadis')['checks']['base_mais_iva']
    # no base extracted -> check is not applicable (passes)
    assert validate(dict(EXT, base_cents=None), QR, 'novadis')['status'] == 'ok'


def test_ref_vs_qr_more_issuer_formats():
    # JMV prints "Factura 1412129305" for QR G "ZF2 B123/1412129305"
    q = dict(QR, G='ZF2 B123/1412129305', H='JFT2HWK7-1412129305')
    assert validate(dict(EXT, invoice_ref='Factura 1412129305'), q, 'jmv')['checks']['ref_vs_qr']
    assert not validate(dict(EXT, invoice_ref='Factura 1412129306'), q, 'jmv')['checks']['ref_vs_qr']
    # Garcias without the dot: "FT FAL2026/3986" for "FT FAL.2026/3986"
    q = dict(QR, G='FT FAL.2026/3986', H='J6T227PR-3986')
    assert validate(dict(EXT, invoice_ref='FT FAL2026/3986'), q, 'garcias')['checks']['ref_vs_qr']
    # Teofilo credit note: extraction prefixes the QR doc type (D=NC)
    q = dict(QR, D='NC', G='C CABA/020980', H='JFVM9MJT-020980')
    assert validate(dict(EXT, invoice_ref='NC C CABA/020980'), q, 'teofilo_nc')['checks']['ref_vs_qr']
    # a wrong doc-type prefix is no longer a token match, but with the
    # ATCUD confirming the number and all amounts passing it is accepted
    # through the near-miss path; without amount confirmation it stays out
    v = validate(dict(EXT, invoice_ref='FT C CABA/020980'), q, 'teofilo_nc')
    assert v['checks']['ref_vs_qr'] and any('ATCUD' in n for n in v['notes'])
    assert not validate(dict(EXT, invoice_ref='FT C CABA/020980',
                             total_cents=1), q, 'teofilo_nc')['checks']['ref_vs_qr']


def test_ref_near_miss_accepted_when_atcud_confirms():
    # Receipt serials get one char misread (Continente: printed CNJ003, read
    # CNJO03) while the ATCUD H carries the same document number and every
    # amount matches the QR: accept, per the QR-is-authoritative policy.
    q = dict(QR, G='FS CNJ003/734718', H='JF53DDGR-734718')
    ext = dict(EXT, invoice_ref='FS CNJO03/734718')
    v = validate(ext, q, 'continente')
    assert v['checks']['ref_vs_qr'] and v['status'] == 'ok'
    assert any('ATCUD' in n for n in v['notes'])
    # a different document number is NOT accepted even with high similarity
    assert not validate(dict(EXT, invoice_ref='FS CNJ003/734719'), q,
                        'continente')['checks']['ref_vs_qr']
    # nor is a near-miss when another check already failed (no cover for
    # a bad extraction)
    bad = validate(dict(ext, total_cents=99999), q, 'continente')
    assert not bad['checks']['ref_vs_qr']
    # a reference whose trailing number contradicts the ATCUD stays held
    assert not validate(dict(EXT, invoice_ref='XY 999/734719'), q,
                        'continente')['checks']['ref_vs_qr']
    # issuer-format differences with the ATCUD number confirmed are accepted
    # (Mscar prints '2026/11361356' for QR 'COM_FAC_NOVOS_F12_AT 001/11361356')
    q2 = dict(QR, G='COM_FAC_NOVOS_F12_AT 001/11361356', H='JF58YBPF-11361356')
    assert validate(dict(EXT, invoice_ref='2026/11361356'), q2,
                    'mscar')['checks']['ref_vs_qr']
