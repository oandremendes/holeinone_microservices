"""Fiscal QR (Portaria 195/2020) decoding and parsing."""
import re
from dataclasses import dataclass

import numpy as np
import pymupdf
import zxingcpp
from PIL import Image, ImageFilter

_wechat = None  # lazy: loading the CNN costs ~0.5s


def _get_wechat():
    global _wechat
    if _wechat is None:
        import cv2
        _wechat = cv2.wechat_qrcode_WeChatQRCode()
    return _wechat


@dataclass
class QrHit:
    page: int          # 1-based
    raw: str
    fields: dict


def parse_payload(text):
    fields = {}
    for part in text.split('*'):
        if ':' in part:
            k, v = part.split(':', 1)
            fields[k] = v
    return fields


def is_fiscal(text):
    return bool(text) and text.startswith('A:') and '*' in text


def qr_date(fields):
    f = fields.get('F', '')
    if re.fullmatch(r'\d{8}', f):
        return f'{f[0:4]}-{f[4:6]}-{f[6:8]}'
    return None


def qr_cents(value):
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


def doc_suffix(fields):
    return '_nc' if fields.get('D') == 'NC' else ''


def _render(page, zoom):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY)
    return Image.frombytes('L', (pix.width, pix.height), pix.samples)


def _zxing(img):
    res = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode)
    return [r.text for r in res if r.valid]


def _wechat_texts(img):
    texts, _ = _get_wechat().detectAndDecode(np.array(img))
    return list(texts)


def _page_fiscal_texts(page, deep):
    """All fiscal payloads on a page. Fast: zxing@216dpi. Deep adds erosion
    (thermal-ink bleed) and the WeChat CNN @300dpi."""
    found = [t for t in _zxing(_render(page, 3.0)) if is_fiscal(t)]
    if found or not deep:
        return found
    img = _render(page, 4.2)
    e3 = img.filter(ImageFilter.MaxFilter(3))
    for texts in (_zxing(img), _zxing(e3),
                  _zxing(img.filter(ImageFilter.MaxFilter(5))),
                  _wechat_texts(img), _wechat_texts(e3)):
        found = [t for t in texts if is_fiscal(t)]
        if found:
            return found
    return []


def decode_pdf(path, deep=True):
    """All fiscal QRs in the document, page order, deduped by raw payload."""
    doc = pymupdf.open(path)
    try:
        hits, seen = [], set()
        for pno in range(len(doc)):
            page_deep = deep and pno in (0, len(doc) - 1)  # CNN only on end pages
            for raw in _page_fiscal_texts(doc[pno], page_deep):
                if raw not in seen:
                    seen.add(raw)
                    hits.append(QrHit(page=pno + 1, raw=raw, fields=parse_payload(raw)))
        return hits
    finally:
        doc.close()
