import io
import numpy as np
import pymupdf
import zxingcpp
from PIL import Image
import qr

PAYLOAD = ('A:504350900*B:508179947*C:PT*D:FT*E:N*F:20260315*'
           'G:FT FT100/011485*H:JFZ4PTHN-011485*I1:PT*I7:374.58*I8:72.35*'
           'N:72.35*O:446.93*Q:abCD*R:2071')


def test_parse_payload():
    f = qr.parse_payload(PAYLOAD)
    assert f['A'] == '504350900'
    assert f['G'] == 'FT FT100/011485'
    assert f['O'] == '446.93'


def test_is_fiscal():
    assert qr.is_fiscal(PAYLOAD)
    assert not qr.is_fiscal('https://example.com/survey')
    assert not qr.is_fiscal(None)


def test_helpers():
    f = qr.parse_payload(PAYLOAD)
    assert qr.qr_date(f) == '2026-03-15'
    assert qr.qr_cents(f['O']) == 44693
    assert qr.qr_cents(None) is None
    assert qr.doc_suffix(f) == ''
    assert qr.doc_suffix({'D': 'NC'}) == '_nc'
    assert qr.qr_date({'F': 'garbage'}) is None


def _make_pdf(tmp_path, payloads):
    """PDF with one QR image per payload, one per page."""
    doc = pymupdf.open()
    for text in payloads:
        barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
        img_zx = zxingcpp.write_barcode_to_image(barcode, scale=14)
        img_array = np.array(img_zx)
        buf = io.BytesIO()
        Image.fromarray(img_array, mode='L').save(buf, format='PNG')
        page = doc.new_page(width=595, height=842)
        page.insert_image(pymupdf.Rect(100, 100, 400, 400), stream=buf.getvalue())
    out = tmp_path / 'test.pdf'
    doc.save(out)
    doc.close()
    return out


def test_decode_pdf_finds_all_fiscal_qrs(tmp_path):
    other = PAYLOAD.replace('011485', '022263').replace('D:FT', 'D:NC')
    pdf = _make_pdf(tmp_path, [PAYLOAD, 'https://not-fiscal.example', other])
    hits = qr.decode_pdf(pdf)
    assert [h.page for h in hits] == [1, 3]
    assert hits[0].fields['G'] == 'FT FT100/011485'
    assert hits[1].fields['D'] == 'NC'


def test_decode_pdf_none(tmp_path):
    pdf = _make_pdf(tmp_path, ['https://nothing.fiscal'])
    assert qr.decode_pdf(pdf) == []
