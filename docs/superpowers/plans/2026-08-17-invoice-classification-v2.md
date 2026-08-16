# Invoice Classification v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** QR-first supplier/date identification, Claude line extraction with a SQLite retry queue, QR-gated Odoo delivery, and JSON artifacts — evolving `invoice_classification/` in place.

**Architecture:** Two phases per oneshot run. Phase A (classify): md5 + QR-identity dedup → QR identification (fallback: existing Tesseract pipeline) → rename/move → enqueue in `state.db`. Phase B (drain): for each queued row, Claude-extract → validate against the QR → file into `INTEGRATED/YYYY/MM/` → write `EXTRACTED/<basename>.json` → POST Odoo. Failures accumulate capped attempts and retry on later runs.

**Tech Stack:** Python 3.10+, sqlite3 (stdlib), PyMuPDF, zxing-cpp, opencv-contrib-python-headless (WeChat QR), Pillow, anthropic SDK (`claude-opus-5`), pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-08-17-invoice-classification-v2-design.md`

## Global Constraints

- All work happens in `invoice_classification/`; tests in `invoice_classification/tests/`.
- Run tests from `invoice_classification/`: `python -m pytest tests/ -v` (pytest picks up modules via rootdir; add `tests/__init__.py` never — use plain dir).
- Money is always **integer cents**. Dates in DB/JSON are `YYYY-MM-DD`; filenames use `YYYYMMDD`.
- Model ID is exactly `claude-opus-5`; API key read from `config.json` `"anthropic": {"api_key": ...}`.
- Never call the real Anthropic/Odoo APIs or move real Drive files in tests — every external effect is injectable and faked in tests.
- The owner's Drive `QA/` folders are reserved for manual acceptance testing — nothing in this plan may process them.
- Existing behavior of the Tesseract fallback pipeline (classify_by_nif/keywords/template, date extraction, REVIEW routing) must not change.
- Commit after every task with the message given in its final step.
- QA_Faturas reference code (for ported modules) lives at `/home/jolteon/github/QA_Faturas/` — copy indicated blocks verbatim from there where instructed.

## File Structure

| File | Responsibility |
|---|---|
| `qr.py` (new) | Render PDF pages, decode fiscal QRs (fast + deep ladder), parse Portaria payloads |
| `state.py` (new) | `state.db` schema + all queue/ledger/supplier operations |
| `claude_extract.py` (new) | Pydantic schema, prompt + supplier hints, Claude call (injectable client) |
| `validation.py` (new) | QR↔extraction checks, ±2c tolerance, supplier quirk table |
| `odoo_send.py` (new) | Payload build + webhook POST (injectable poster), Drive id/preview |
| `artifacts.py` (new) | Build artifact dict, atomic JSON write |
| `pipeline.py` (new) | Phase A per-file identify/dedup/enqueue; Phase B drain loop |
| `classifier.py` (modify) | Wire pipeline into `process_and_move()` + `process` command; legacy upload behind flag |
| `api_config.py` (modify) | Load new config sections (`anthropic`, `odoo`, `legacy_apis`) |
| `config.example.json`, `requirements.txt`, `deploy.sh` (modify) | New deps/config/dirs; preserve `state.db` |

---

### Task 1: `state.py` — schema, ledger, queue

**Files:**
- Create: `invoice_classification/state.py`
- Create: `invoice_classification/tests/conftest.py`
- Test: `invoice_classification/tests/test_state.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces (used by every later task):
  - `connect(db_path: Path|str) -> sqlite3.Connection` — WAL, `row_factory=sqlite3.Row`, creates schema idempotently.
  - `insert_file(conn, *, md5, original_name, current_path, nif=None, atcud=None, doc_ref=None, supplier_key=None, doc_date=None, doc_type=None, id_source='none', status='queued') -> int`
  - `find_by_md5(conn, md5) -> Row|None`; `find_by_identity(conn, nif, atcud=None, doc_ref=None) -> Row|None` (matches on atcud when given, else doc_ref; ignores rows with status `duplicate`)
  - `add_qr(conn, file_id, page, raw_payload, parsed: dict, is_primary: bool)`
  - `mark_duplicate(conn, file_id, of_id)`; `supersede(conn, old_id, new_id)` (old → status `superseded`, `superseded_by=new_id`)
  - `pending(conn, cap=5) -> list[Row]` — status IN ('queued','retry') AND attempts < cap, joined with `extractions.attempts`
  - `record_error(conn, file_id, error: str, cap=5, permanent=False)` — attempts+1, last_error; status→`failed` if permanent or attempts≥cap else `retry`
  - `save_extraction(conn, file_id, model, result_json: str, validation_json: str)`
  - `set_status(conn, file_id, status)`; `update_path(conn, file_id, new_path)`
  - `mark_sent(conn, file_id, odoo_result_json: str)` — sets extractions.odoo_sent_at + files.status='sent'
  - `seed_suppliers(conn, entries: dict[str, tuple[str, str]])` — `{key: (nif, display_name)}`, INSERT OR IGNORE
  - `supplier_for_nif(conn, nif) -> Row|None`; `register_supplier(conn, nif, key, display_name) `(auto_registered=1)

- [ ] **Step 1: conftest with a tmp DB fixture**

```python
# tests/conftest.py
import pytest
import state


@pytest.fixture
def conn(tmp_path):
    c = state.connect(tmp_path / 'state.db')
    yield c
    c.close()
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_state.py
import state


def test_insert_and_md5_lookup(conn):
    fid = state.insert_file(conn, md5='abc', original_name='x.pdf',
                            current_path='/tmp/x.pdf')
    row = state.find_by_md5(conn, 'abc')
    assert row['id'] == fid and row['status'] == 'queued'
    assert state.find_by_md5(conn, 'zzz') is None


def test_identity_lookup_prefers_atcud_and_skips_duplicates(conn):
    a = state.insert_file(conn, md5='1', original_name='a.pdf', current_path='/a',
                          nif='504350900', atcud='JFZ4-011485', doc_ref='FT FT100/011485')
    assert state.find_by_identity(conn, '504350900', atcud='JFZ4-011485')['id'] == a
    assert state.find_by_identity(conn, '504350900', doc_ref='FT FT100/011485')['id'] == a
    assert state.find_by_identity(conn, '504350900', atcud='OTHER') is None
    b = state.insert_file(conn, md5='2', original_name='b.pdf', current_path='/b',
                          nif='504350900', atcud='X-1')
    state.mark_duplicate(conn, b, a)
    assert state.find_by_identity(conn, '504350900', atcud='X-1') is None


def test_queue_retry_cap_and_permanent(conn):
    fid = state.insert_file(conn, md5='q', original_name='q.pdf', current_path='/q')
    assert [r['id'] for r in state.pending(conn)] == [fid]
    for i in range(4):
        state.record_error(conn, fid, f'boom {i}')
        assert conn.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()[0] == 'retry'
    state.record_error(conn, fid, 'boom 5')  # attempt 5 == cap
    assert conn.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()[0] == 'failed'
    assert state.pending(conn) == []
    p = state.insert_file(conn, md5='p', original_name='p.pdf', current_path='/p')
    state.record_error(conn, p, 'refusal', permanent=True)
    assert conn.execute("SELECT status FROM files WHERE id=?", (p,)).fetchone()[0] == 'failed'


def test_supersede_and_sent(conn):
    old = state.insert_file(conn, md5='o', original_name='o.pdf', current_path='/o')
    state.record_error(conn, old, 'x', permanent=True)
    new = state.insert_file(conn, md5='n', original_name='n.pdf', current_path='/n')
    state.supersede(conn, old, new)
    row = conn.execute("SELECT status, superseded_by FROM files WHERE id=?", (old,)).fetchone()
    assert row['status'] == 'superseded' and row['superseded_by'] == new
    state.save_extraction(conn, new, 'claude-opus-5', '{}', '{"status":"ok"}')
    state.mark_sent(conn, new, '{"id": 7}')
    assert conn.execute("SELECT status FROM files WHERE id=?", (new,)).fetchone()[0] == 'sent'


def test_suppliers_seed_and_register(conn):
    state.seed_suppliers(conn, {'novadis': ('504350900', 'Novadis')})
    assert state.supplier_for_nif(conn, '504350900')['supplier_key'] == 'novadis'
    assert state.supplier_for_nif(conn, '999999999') is None
    state.register_supplier(conn, '999999999', 'nif999999999', 'NIF 999999999')
    row = state.supplier_for_nif(conn, '999999999')
    assert row['auto_registered'] == 1
    state.seed_suppliers(conn, {'other': ('504350900', 'X')})  # no clobber
    assert state.supplier_for_nif(conn, '504350900')['supplier_key'] == 'novadis'
```

- [ ] **Step 3: Run to verify failure** — `python -m pytest tests/test_state.py -v` → FAIL (`ModuleNotFoundError: state`).

- [ ] **Step 4: Implement `state.py`**

```python
"""SQLite state store: file ledger, extraction queue, supplier registry."""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  md5 TEXT UNIQUE NOT NULL,
  nif TEXT, atcud TEXT, doc_ref TEXT,
  original_name TEXT NOT NULL,
  current_path TEXT NOT NULL,
  supplier_key TEXT, doc_date TEXT, doc_type TEXT,
  id_source TEXT NOT NULL DEFAULT 'none',
  status TEXT NOT NULL DEFAULT 'queued',
  superseded_by INTEGER REFERENCES files(id),
  duplicate_of INTEGER REFERENCES files(id),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qr_codes (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id),
  page INTEGER, raw_payload TEXT NOT NULL, parsed_json TEXT NOT NULL,
  is_primary INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS extractions (
  file_id INTEGER PRIMARY KEY REFERENCES files(id),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, model TEXT, extracted_at TEXT,
  result_json TEXT, validation_json TEXT,
  odoo_sent_at TEXT, odoo_result TEXT
);
CREATE TABLE IF NOT EXISTS suppliers (
  nif TEXT PRIMARY KEY,
  supplier_key TEXT NOT NULL,
  display_name TEXT,
  auto_registered INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    return conn


def insert_file(conn, *, md5, original_name, current_path, nif=None, atcud=None,
                doc_ref=None, supplier_key=None, doc_date=None, doc_type=None,
                id_source='none', status='queued'):
    cur = conn.execute(
        "INSERT INTO files (md5, nif, atcud, doc_ref, original_name, current_path,"
        " supplier_key, doc_date, doc_type, id_source, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (md5, nif, atcud, doc_ref, original_name, current_path, supplier_key,
         doc_date, doc_type, id_source, status, _now()))
    conn.commit()
    return cur.lastrowid


def find_by_md5(conn, md5):
    return conn.execute("SELECT * FROM files WHERE md5 = ?", (md5,)).fetchone()


def find_by_identity(conn, nif, atcud=None, doc_ref=None):
    base = "SELECT * FROM files WHERE nif = ? AND status NOT IN ('duplicate','superseded') AND "
    if atcud:
        return conn.execute(base + "atcud = ?", (nif, atcud)).fetchone()
    if doc_ref:
        return conn.execute(base + "doc_ref = ?", (nif, doc_ref)).fetchone()
    return None


def add_qr(conn, file_id, page, raw_payload, parsed, is_primary):
    import json
    conn.execute("INSERT INTO qr_codes (file_id, page, raw_payload, parsed_json,"
                 " is_primary) VALUES (?,?,?,?,?)",
                 (file_id, page, raw_payload, json.dumps(parsed), int(is_primary)))
    conn.commit()


def mark_duplicate(conn, file_id, of_id):
    conn.execute("UPDATE files SET status='duplicate', duplicate_of=? WHERE id=?",
                 (of_id, file_id))
    conn.commit()


def supersede(conn, old_id, new_id):
    conn.execute("UPDATE files SET status='superseded', superseded_by=? WHERE id=?",
                 (new_id, old_id))
    conn.commit()


def pending(conn, cap=5):
    return conn.execute(
        "SELECT f.*, COALESCE(e.attempts, 0) AS attempts FROM files f"
        " LEFT JOIN extractions e ON e.file_id = f.id"
        " WHERE f.status IN ('queued','retry') AND COALESCE(e.attempts, 0) < ?"
        " ORDER BY f.id", (cap,)).fetchall()


def _ensure_extraction_row(conn, file_id):
    conn.execute("INSERT OR IGNORE INTO extractions (file_id) VALUES (?)", (file_id,))


def record_error(conn, file_id, error, cap=5, permanent=False):
    _ensure_extraction_row(conn, file_id)
    conn.execute("UPDATE extractions SET attempts = attempts + 1, last_error = ?"
                 " WHERE file_id = ?", (error, file_id))
    attempts = conn.execute("SELECT attempts FROM extractions WHERE file_id = ?",
                            (file_id,)).fetchone()[0]
    status = 'failed' if permanent or attempts >= cap else 'retry'
    conn.execute("UPDATE files SET status = ? WHERE id = ?", (status, file_id))
    conn.commit()


def save_extraction(conn, file_id, model, result_json, validation_json):
    _ensure_extraction_row(conn, file_id)
    conn.execute("UPDATE extractions SET model=?, extracted_at=?, result_json=?,"
                 " validation_json=? WHERE file_id=?",
                 (model, _now(), result_json, validation_json, file_id))
    conn.commit()


def set_status(conn, file_id, status):
    conn.execute("UPDATE files SET status=? WHERE id=?", (status, file_id))
    conn.commit()


def update_path(conn, file_id, new_path):
    conn.execute("UPDATE files SET current_path=? WHERE id=?", (str(new_path), file_id))
    conn.commit()


def mark_sent(conn, file_id, odoo_result_json):
    conn.execute("UPDATE extractions SET odoo_sent_at=?, odoo_result=? WHERE file_id=?",
                 (_now(), odoo_result_json, file_id))
    conn.execute("UPDATE files SET status='sent' WHERE id=?", (file_id,))
    conn.commit()


def seed_suppliers(conn, entries):
    for key, (nif, display_name) in entries.items():
        if nif and len(nif) == 9 and nif != '000000000':
            conn.execute("INSERT OR IGNORE INTO suppliers (nif, supplier_key,"
                         " display_name, auto_registered, created_at) VALUES (?,?,?,0,?)",
                         (nif, key, display_name, _now()))
    conn.commit()


def supplier_for_nif(conn, nif):
    return conn.execute("SELECT * FROM suppliers WHERE nif = ?", (nif,)).fetchone()


def register_supplier(conn, nif, key, display_name):
    conn.execute("INSERT OR IGNORE INTO suppliers (nif, supplier_key, display_name,"
                 " auto_registered, created_at) VALUES (?,?,?,1,?)",
                 (nif, key, display_name, _now()))
    conn.commit()
```

- [ ] **Step 5: Run tests** — `python -m pytest tests/test_state.py -v` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add invoice_classification/state.py invoice_classification/tests/
git commit -m "[ADD] invoice_classification: state.py SQLite ledger + retry queue"
```

---

### Task 2: `qr.py` — payload parsing and PDF decoding

**Files:**
- Create: `invoice_classification/qr.py`
- Test: `invoice_classification/tests/test_qr.py`
- Modify: `invoice_classification/requirements.txt` (this task needs the new deps)

**Interfaces:**
- Consumes: nothing internal.
- Produces:
  - `parse_payload(text: str) -> dict` — `'A:1*B:2'` → `{'A':'1','B':'2'}`
  - `is_fiscal(text: str|None) -> bool` — starts with `'A:'` and contains `'*'`
  - `qr_date(fields: dict) -> str|None` — field F `YYYYMMDD` → `'YYYY-MM-DD'`
  - `qr_cents(value: str|None) -> int|None` — `'446.93'` → `44693`
  - `doc_suffix(fields: dict) -> str` — `'_nc'` when field D == `'NC'`, else `''`
  - `@dataclass QrHit: page: int; raw: str; fields: dict`
  - `decode_pdf(path, deep=True) -> list[QrHit]` — ALL fiscal QRs, first/last-page fast path then deep ladder

- [ ] **Step 1: Update requirements.txt** — replace the line `opencv-python>=4.8.0` with `opencv-contrib-python-headless>=4.8.0` and append:

```
# QR decoding (v2)
pymupdf>=1.24.0
zxing-cpp>=2.2.0

# Claude extraction (v2)
anthropic>=0.100.0
pydantic>=2.0.0

# Tests
pytest>=8.0.0
```

Then `pip install -r requirements.txt` in the dev venv. Note: `opencv-python` must be uninstalled first (`pip uninstall -y opencv-python`) — contrib replaces it.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_qr.py
import pymupdf
import zxingcpp
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
        img = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, text, 400, 400)
        import PIL.Image, io
        buf = io.BytesIO()
        PIL.Image.fromarray(img).save(buf, format='PNG')
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
```

- [ ] **Step 3: Run to verify failure** — `python -m pytest tests/test_qr.py -v` → FAIL.

- [ ] **Step 4: Implement `qr.py`**

```python
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
```

- [ ] **Step 5: Run tests** — `python -m pytest tests/test_qr.py -v` → PASS.
- [ ] **Step 6: Commit** — `git add -A invoice_classification && git commit -m "[ADD] invoice_classification: qr.py fiscal QR decode + parse; v2 deps"`

---

### Task 3: `claude_extract.py` — schema, prompt, injectable Claude call

**Files:**
- Create: `invoice_classification/claude_extract.py`
- Test: `invoice_classification/tests/test_claude_extract.py`

**Interfaces:**
- Consumes: nothing internal.
- Produces:
  - Pydantic models `Line`, `TaxLine`, `Extraction` — **copied verbatim** from `/home/jolteon/github/QA_Faturas/claude_extract.py:70-101`.
  - `PROMPT`, `SUPPLIER_HINTS`, `MODEL = 'claude-opus-5'` — copied verbatim from the same file (lines 14-67).
  - `PermanentExtractionError(RuntimeError)` — refusal/corrupt input; never retried.
  - `extract(pdf_path, supplier=None, api_key=None, client=None) -> tuple[Extraction, str]` — `client` injectable for tests; one re-ask on invalid JSON (transient `RuntimeError` after that).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_claude_extract.py
import json
import pytest
import claude_extract as ce


class FakeBlock:
    type = 'text'
    def __init__(self, text): self.text = text


class FakeResponse:
    def __init__(self, text, stop_reason='end_turn'):
        self.stop_reason = stop_reason
        self.content = [FakeBlock(text)]


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = self
    def create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


GOOD = json.dumps({'supplier_name': 'Novadis', 'supplier_nif': '504350900',
                   'customer_nif': None, 'invoice_ref': 'FT FT100/011485',
                   'date': '2026-03-15', 'due_date': None, 'base_cents': 37458,
                   'iva_cents': 7235, 'total_cents': 44693,
                   'total_vasilhame_cents': None, 'total_document_cents': None,
                   'lines': [{'description': 'Barril', 'supplier_code': None,
                              'quantity': 2, 'unit_price_eur': None,
                              'discount_pct': None, 'iva_rate_pct': 23.0,
                              'line_net_cents': 37458, 'line_total_cents': 44693}],
                   'taxes': []})


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / 'x.pdf'
    p.write_bytes(b'%PDF-1.4 fake')
    return p


def test_extract_ok(pdf):
    fake = FakeClient([FakeResponse(f'aqui está:\n{GOOD}\nfim')])
    result, raw = ce.extract(pdf, supplier='novadis', client=fake)
    assert result.total_cents == 44693
    assert result.lines[0].description == 'Barril'
    assert 'Notas específicas' in fake.calls[0]['messages'][0]['content'][1]['text']
    assert fake.calls[0]['model'] == 'claude-opus-5'


def test_extract_retries_bad_json_once(pdf):
    fake = FakeClient([FakeResponse('not json at all'), FakeResponse(GOOD)])
    result, _ = ce.extract(pdf, client=fake)
    assert result.total_cents == 44693
    assert len(fake.calls) == 2


def test_extract_gives_up_after_two_bad(pdf):
    fake = FakeClient([FakeResponse('junk'), FakeResponse('junk')])
    with pytest.raises(RuntimeError) as e:
        ce.extract(pdf, client=fake)
    assert not isinstance(e.value, ce.PermanentExtractionError)


def test_refusal_is_permanent(pdf):
    fake = FakeClient([FakeResponse('', stop_reason='refusal')])
    with pytest.raises(ce.PermanentExtractionError):
        ce.extract(pdf, client=fake)
```

- [ ] **Step 2: Run to verify failure** — FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement.** Copy from `/home/jolteon/github/QA_Faturas/claude_extract.py` **verbatim**: the module constants `MODEL`, `PROMPT`, `SUPPLIER_HINTS` (lines 14-67), the three Pydantic models (lines 70-101), and the helpers `build_content` (116-131) and `_parse_json_response` (134-140). Then replace `_api_key`/`extract` with:

```python
class PermanentExtractionError(RuntimeError):
    """Failure that must not be retried (refusal, unreadable input)."""


def extract(pdf_path, supplier=None, api_key=None, client=None):
    """PDF -> (Extraction, raw_json_str). `client` injectable for tests.

    Invalid-JSON responses get one re-ask; still invalid -> RuntimeError
    (transient, retried by the queue). refusal -> PermanentExtractionError.
    """
    import pydantic
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    content = build_content(pdf_path, supplier)
    messages = [{'role': 'user', 'content': content}]
    last_err = None
    for _ in range(2):
        response = client.messages.create(model=MODEL, max_tokens=16000,
                                          messages=messages)
        if response.stop_reason == 'refusal':
            raise PermanentExtractionError('Claude recusou (stop_reason=refusal)')
        text = next(b.text for b in response.content if b.type == 'text')
        try:
            result = Extraction.model_validate(_parse_json_response(text))
            return result, result.model_dump_json()
        except (ValueError, pydantic.ValidationError) as e:
            last_err = e
            messages = [{'role': 'user', 'content': content},
                        {'role': 'user', 'content':
                         f'A resposta anterior era inválida ({e}). Responde de '
                         f'novo, apenas com o objeto JSON válido.'}]
    raise RuntimeError(f'Resposta JSON inválida após repetição: {last_err}')
```

Do not copy the QA-specific `main()`/`extract_dict`/db imports.

- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -am "[ADD] invoice_classification: claude_extract.py (ported from QA_Faturas, injectable client)"`

---

### Task 4: `validation.py` — QR gate with quirk table

**Files:**
- Create: `invoice_classification/validation.py`
- Test: `invoice_classification/tests/test_validation.py`

**Interfaces:**
- Consumes: `qr.qr_cents` semantics (euros-string → cents) — reimplement locally is fine but import from `qr` to stay DRY.
- Produces:
  - `validate(extraction: dict, qr_fields: dict|None, supplier_key: str|None) -> dict` shaped `{'status': 'ok'|'needs_review', 'checks': {...}, 'notes': [...]}`. Checks: `total_vs_qr`, `iva_vs_qr`, `ref_vs_qr`, `date_vs_qr`, `soma_linhas`. With `qr_fields=None` only `soma_linhas` runs and status is `needs_review` (no QR → cannot gate → hold).
  - `IVA_QUIRKS: dict[str, str]` — `{'teofilo': 'iva_doubled', 'orientalshopping': 'base_in_n', 'seminoshopping': 'base_in_n'}`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_validation.py
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


def test_soma_linhas():
    ext = dict(EXT, lines=[{'line_total_cents': 100}])
    v = validate(ext, QR, None)
    assert not v['checks']['soma_linhas'] and v['status'] == 'needs_review'


def test_no_qr_holds():
    v = validate(EXT, None, 'novadis')
    assert v['status'] == 'needs_review'
    assert 'sem QR' in ' '.join(v['notes'])
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement `validation.py`**

```python
"""QR <-> Claude extraction validation gate."""
import re

from qr import qr_cents

TOL = 2  # cents

IVA_QUIRKS = {
    'teofilo': 'iva_doubled',        # QR N = 2x real IVA (ZFV1 series)
    'orientalshopping': 'base_in_n', # issuer software puts the base in N
    'seminoshopping': 'base_in_n',
}


def _norm_ref(s):
    if not s:
        return ''
    s = re.sub(r'\s+', ' ', s.strip().upper())
    m = re.match(r'^(.*?)(\d+)$', s)
    return (m.group(1) + str(int(m.group(2)))) if m else s


def _close(a, b):
    return a is not None and b is not None and abs(a - b) <= TOL


def validate(extraction, qr_fields, supplier_key):
    checks, notes = {}, []
    lines = extraction.get('lines') or []
    line_sum = sum(ln.get('line_total_cents') or 0 for ln in lines)
    checks['soma_linhas'] = line_sum == (extraction.get('total_cents') or 0)

    if not qr_fields:
        notes.append('sem QR fiscal — validação impossível, retido')
        return {'status': 'needs_review', 'checks': checks, 'notes': notes}

    base_key = (supplier_key or '').removesuffix('_nc')
    q_total = qr_cents(qr_fields.get('O'))
    q_iva = qr_cents(qr_fields.get('N'))
    e_total, e_iva = extraction.get('total_cents'), extraction.get('iva_cents')

    checks['total_vs_qr'] = _close(e_total, q_total)

    quirk = IVA_QUIRKS.get(base_key)
    if _close(e_iva, q_iva):
        checks['iva_vs_qr'] = True
    elif quirk == 'iva_doubled' and e_iva is not None and _close(e_iva * 2, q_iva):
        checks['iva_vs_qr'] = True
        notes.append('quirk iva_doubled aplicado (QR N = 2x IVA)')
    elif (quirk == 'base_in_n' and e_iva is not None and e_total is not None
          and _close(e_total - e_iva, q_iva)):
        checks['iva_vs_qr'] = True
        notes.append('quirk base_in_n aplicado (QR N contém a base)')
    else:
        checks['iva_vs_qr'] = False

    g, h = qr_fields.get('G'), qr_fields.get('H')
    full = f'{g} / {h}' if h else g
    ref = _norm_ref(extraction.get('invoice_ref'))
    checks['ref_vs_qr'] = ref in (_norm_ref(g), _norm_ref(full)) and bool(ref)

    from qr import qr_date
    checks['date_vs_qr'] = (extraction.get('date') == qr_date(qr_fields)
                            and extraction.get('date') is not None)

    status = 'ok' if all(checks.values()) else 'needs_review'
    return {'status': status, 'checks': checks, 'notes': notes}
```

- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -am "[ADD] invoice_classification: validation.py QR gate + IVA quirk table"`

---

### Task 5: `odoo_send.py` and `artifacts.py`

**Files:**
- Create: `invoice_classification/odoo_send.py`
- Create: `invoice_classification/artifacts.py`
- Test: `invoice_classification/tests/test_odoo_send.py`, `invoice_classification/tests/test_artifacts.py`

**Interfaces:**
- Consumes: `Extraction.model_dump()` dict shape from Task 3; `files` Row fields from Task 1.
- Produces:
  - `odoo_send.build_payload(file_row, extraction: dict, document_url=None) -> dict` — like QA_Faturas but sourced from a `files` row: `vendor_name` from `file_row['supplier_key']` (strip `_nc`, `.capitalize()`), `document_name` from `Path(file_row['current_path']).name`, `external_document_id` = `str(file_row['id'])`, `md5` from row.
  - `odoo_send.send(payload, webhook_url, api_key, poster=None) -> dict` — POST `{**payload, 'api_key': api_key}`; raise `RuntimeError` on JSON-RPC error envelope.
  - `odoo_send.resolve_drive_id(remote_path, run=None) -> str|None`; `odoo_send.drive_preview_url(drive_id) -> str` — copied verbatim from `/home/jolteon/github/QA_Faturas/odoo_export.py:79-96`.
  - `artifacts.build_artifact(file_row, qr_fields, extraction, validation, odoo, model, attempts) -> dict` (spec JSON shape; `odoo` is `{'sent_at':…,'status':'sent'|'held'|None}`)
  - `artifacts.write_json(dir_path: Path, basename: str, data: dict) -> Path` — atomic (`.tmp` + `os.replace`), `ensure_ascii=False`, `indent=1`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_odoo_send.py
import pytest
import odoo_send

FILE_ROW = {'id': 7, 'md5': 'abc', 'supplier_key': 'teofilo_nc',
            'current_path': '/x/INTEGRATED/2026/03/20260315_Teofilo_nc.pdf'}
EXT = {'invoice_ref': 'C CAAU/022263', 'date': '2026-07-21', 'due_date': None,
       'customer_nif': '508179947', 'base_cents': 42000, 'iva_cents': 0,
       'total_cents': 42000, 'total_vasilhame_cents': None,
       'total_document_cents': None,
       'lines': [{'description': 'Barril', 'supplier_code': 'B1',
                  'quantity': 2.0, 'unit_price_eur': 10.5, 'discount_pct': None,
                  'iva_rate_pct': 0.0, 'line_net_cents': 42000,
                  'line_total_cents': 42000}],
       'taxes': [{'rate_pct': 0.0, 'base_cents': 42000, 'value_cents': 0}]}


def test_build_payload():
    p = odoo_send.build_payload(FILE_ROW, EXT, document_url='https://d/p')
    assert p['vendor_name'] == 'Teofilo'          # _nc stripped, capitalized
    assert p['invoice_number'] == 'C CAAU/022263'
    assert p['total_amount'] == 420.0 and p['vat_amount'] == 0.0
    assert p['document_name'] == '20260315_Teofilo_nc.pdf'
    assert p['external_document_id'] == '7' and p['md5'] == 'abc'
    assert p['items'][0]['designation'] == 'Barril'
    assert p['document_url'] == 'https://d/p'


def test_build_payload_requires_ref():
    with pytest.raises(ValueError):
        odoo_send.build_payload(FILE_ROW, dict(EXT, invoice_ref=None))


def test_send_ok_and_error():
    seen = {}
    def poster(url, body, headers):
        seen.update(url=url, body=body)
        return {'result': {'ok': 1}}
    r = odoo_send.send({'a': 1}, 'https://odoo.example', 'KEY', poster=poster)
    assert r == {'ok': 1}
    assert seen['url'].endswith('/webhook/claude/invoice')
    assert seen['body']['api_key'] == 'KEY'
    with pytest.raises(RuntimeError):
        odoo_send.send({}, 'https://odoo.example', 'KEY',
                       poster=lambda *a: {'error': {'message': 'nope'}})
```

```python
# tests/test_artifacts.py
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
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement.** `odoo_send.py`: copy `_eur`, `_line_net_cents`, `resolve_drive_id`, `drive_preview_url`, `_post_json` verbatim from `/home/jolteon/github/QA_Faturas/odoo_export.py`; adapt `build_payload` to take `(file_row, extraction, document_url=None)` — body identical to QA's (`odoo_export.py:26-76`) except: `vendor` from `file_row['supplier_key']` (raise `ValueError` if falsy; `.removesuffix('_nc').capitalize()`), `document_name` = `Path(file_row['current_path']).name`, `external_document_id` = `str(file_row['id'])`, `md5` = `file_row['md5']`, and `lines`/`taxes` read from `extraction['lines']`/`extraction['taxes']`. Adapt `send(payload, webhook_url, api_key, poster=None)` to append `/webhook/claude/invoice` to `webhook_url` (QA's `send`, `odoo_export.py:108-123`, unchanged otherwise).

`artifacts.py`:

```python
"""JSON artifacts for ScanSnap/EXTRACTED/ — atomic writes."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def build_artifact(file_row, qr_fields, extraction, validation, odoo, model, attempts):
    path = file_row['current_path']
    marker = '/ScanSnap/'
    rel = path.split(marker, 1)[1] if marker in path else path
    return {
        'pdf': rel,
        'processed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'model': model,
        'attempts': attempts,
        'identification': {
            'supplier_key': file_row['supplier_key'], 'nif': file_row['nif'],
            'date': file_row['doc_date'], 'doc_type': file_row['doc_type'],
            'source': file_row['id_source'],
        },
        'qr': qr_fields,
        'extraction': extraction,
        'validation': validation,
        'odoo': odoo,
    }


def write_json(dir_path, basename, data):
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    final = dir_path / f'{basename}.json'
    tmp = dir_path / f'{basename}.json.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, final)
    return final
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/test_odoo_send.py tests/test_artifacts.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "[ADD] invoice_classification: odoo_send.py + artifacts.py"`

---

### Task 6: config plumbing in `api_config.py`

**Files:**
- Modify: `invoice_classification/api_config.py` (append functions; do not touch `SUPPLIER_ROUTES`/`get_route`)
- Modify: `invoice_classification/config.example.json`
- Test: `invoice_classification/tests/test_api_config.py`

**Interfaces:**
- Consumes: existing `load_config()` in `api_config.py` (reads `config.json` next to the module).
- Produces:
  - `get_anthropic() -> dict` — `{'api_key': …, 'model': 'claude-opus-5'}` (model defaulted when absent)
  - `get_odoo() -> dict` — `{'webhook_url': …, 'api_key': …, 'drive_remote': 'gdrive:ScanSnap'}` (drive_remote defaulted)
  - `legacy_apis_enabled() -> bool` — default **False** when section absent
  - All three accept an optional `config: dict` parameter for tests (bypassing file load).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_api_config.py
import api_config


def test_defaults_from_empty_config():
    assert api_config.legacy_apis_enabled(config={}) is False
    a = api_config.get_anthropic(config={'anthropic': {'api_key': 'k'}})
    assert a == {'api_key': 'k', 'model': 'claude-opus-5'}
    o = api_config.get_odoo(config={'odoo': {'webhook_url': 'u', 'api_key': 'k'}})
    assert o['drive_remote'] == 'gdrive:ScanSnap'


def test_legacy_flag_reads_config():
    assert api_config.legacy_apis_enabled(
        config={'legacy_apis': {'enabled': True}}) is True
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** — append to `api_config.py`:

```python
def get_anthropic(config=None):
    cfg = (config if config is not None else load_config()).get('anthropic', {})
    return {'api_key': cfg.get('api_key'),
            'model': cfg.get('model', 'claude-opus-5')}


def get_odoo(config=None):
    cfg = (config if config is not None else load_config()).get('odoo', {})
    return {'webhook_url': cfg.get('webhook_url'),
            'api_key': cfg.get('api_key'),
            'drive_remote': cfg.get('drive_remote', 'gdrive:ScanSnap')}


def legacy_apis_enabled(config=None):
    cfg = config if config is not None else load_config()
    return bool(cfg.get('legacy_apis', {}).get('enabled', False))
```

(If `load_config` has a different name in `api_config.py`, use the existing loader function — check `api_config.py:14-30`.) Update `config.example.json` to:

```json
{
  "parseur":  {"api_key": "YOUR_PARSEUR_KEY"},
  "docupipe": {"api_key": "YOUR_DOCUPIPE_KEY"},
  "legacy_apis": {"enabled": false},
  "anthropic": {"api_key": "YOUR_ANTHROPIC_KEY", "model": "claude-opus-5"},
  "odoo": {"webhook_url": "https://YOUR_ODOO_HOST", "api_key": "YOUR_ODOO_WEBHOOK_KEY",
           "drive_remote": "gdrive:ScanSnap"}
}
```

- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -am "[ADD] invoice_classification: config sections for anthropic/odoo/legacy_apis"`

---

### Task 7: `pipeline.py` Phase A — identify, dedup, enqueue

**Files:**
- Create: `invoice_classification/pipeline.py`
- Test: `invoice_classification/tests/test_pipeline_phase_a.py`

**Interfaces:**
- Consumes: `state.*` (Task 1), `qr.decode_pdf/qr_date/doc_suffix/QrHit` (Task 2).
- Produces:
  - `file_md5(path: Path) -> str`
  - `@dataclass Identified: supplier_key, doc_date, doc_type, nif, atcud, doc_ref, id_source, qr_hits: list` — `doc_date` is `YYYY-MM-DD` or None.
  - `identify(pdf_path, conn, ocr_fallback) -> Identified` — QR-first; `ocr_fallback(pdf_path)` is called only on QR miss and must return `(supplier_key_or_'unknown', date_yyyymmdd_or_None)` (adapter around `InvoiceClassifier.classify` is wired in Task 9).
  - `check_duplicate(conn, md5, ident) -> tuple[str, Row|None]` — returns `('new', None)`, `('duplicate', original_row)` or `('supersede', original_row)`. Supersede when original status in `('failed',)` or original `id_source == 'none'` and status == 'review_folder'.
  - `handle_new_file(pdf_path, conn, dirs, ocr_fallback, dry_run=False) -> dict` — full Phase A for one file: dedup → identify → rename/move to `dirs['integrated']` or `dirs['review']` (or `dirs['duplicados']`) → DB rows. Returns `{'action': 'INTEGRATED'|'REVIEW'|'DUPLICATE'|'SUPERSEDE', 'new_name': …, 'file_id': …}`. Filename: `{YYYYMMDD or f'{year}XXXX'}_{Supplier.capitalize()}{suffix}.pdf` with `_1`,`_2` collision counters (same convention as today).

- [ ] **Step 1: Write failing tests** (fake QR decode by monkeypatching `pipeline.qr_decode`, which the module exposes as an indirection over `qr.decode_pdf`):

```python
# tests/test_pipeline_phase_a.py
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
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement `pipeline.py` (Phase A half)**

```python
"""v2 pipeline: Phase A (identify/dedup/enqueue) and Phase B (drain queue)."""
import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import qr as qr_mod
import state

logger = logging.getLogger('invoice_classifier')

# indirection so tests can monkeypatch cheaply
def qr_decode(pdf_path):
    return qr_mod.decode_pdf(pdf_path)


def file_md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


@dataclass
class Identified:
    supplier_key: str = 'unknown'
    doc_date: str | None = None      # YYYY-MM-DD
    doc_type: str | None = None
    nif: str | None = None
    atcud: str | None = None
    doc_ref: str | None = None
    id_source: str = 'none'
    qr_hits: list = field(default_factory=list)


def identify(pdf_path, conn, ocr_fallback):
    hits = qr_decode(pdf_path)
    if hits:
        f = hits[0].fields          # primary = first fiscal QR
        nif = f.get('A')
        sup = state.supplier_for_nif(conn, nif) if nif else None
        if sup is None and nif:
            key = f'nif{nif}'
            state.register_supplier(conn, nif, key, f'NIF {nif}')
        else:
            key = sup['supplier_key'] if sup else 'unknown'
        return Identified(supplier_key=key + qr_mod.doc_suffix(f)
                          if not key.endswith('_nc') else key,
                          doc_date=qr_mod.qr_date(f), doc_type=f.get('D'),
                          nif=nif, atcud=f.get('H'), doc_ref=f.get('G'),
                          id_source='qr', qr_hits=hits)
    if ocr_fallback is not None:
        key, yyyymmdd = ocr_fallback(pdf_path)
        date = (f'{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}'
                if yyyymmdd else None)
        if key != 'unknown':
            return Identified(supplier_key=key, doc_date=date, id_source='ocr')
    return Identified()


def check_duplicate(conn, md5, ident):
    row = state.find_by_md5(conn, md5)
    if row is None:
        for hit in ident.qr_hits:
            row = state.find_by_identity(conn, hit.fields.get('A'),
                                         atcud=hit.fields.get('H'),
                                         doc_ref=hit.fields.get('G'))
            if row:
                break
    if row is None:
        return 'new', None
    bad = row['status'] == 'failed' or (row['status'] == 'review_folder'
                                        and row['id_source'] == 'none')
    return ('supersede' if bad else 'duplicate'), row


def _unique_dest(target_dir, stem, ext='.pdf'):
    dest = target_dir / f'{stem}{ext}'
    counter = 1
    while dest.exists():
        dest = target_dir / f'{stem}_{counter}{ext}'
        counter += 1
    return dest


def handle_new_file(pdf_path, conn, dirs, ocr_fallback, dry_run=False):
    pdf_path = Path(pdf_path)
    md5 = file_md5(pdf_path)
    ident = identify(pdf_path, conn, ocr_fallback)
    verdict, original = check_duplicate(conn, md5, ident)

    if verdict == 'duplicate':
        dest = _unique_dest(dirs['duplicados'], pdf_path.stem)
        if not dry_run:
            shutil.move(str(pdf_path), str(dest))
        fid = state.insert_file(conn, md5=md5, original_name=pdf_path.name,
                                current_path=str(dest), nif=ident.nif,
                                atcud=ident.atcud, doc_ref=ident.doc_ref,
                                id_source=ident.id_source, status='queued')
        state.mark_duplicate(conn, fid, original['id'])
        return {'action': 'DUPLICATE', 'new_name': dest.name, 'file_id': fid}

    # supersede: move the bad original out of the way first
    if verdict == 'supersede' and not dry_run:
        old_path = Path(original['current_path'])
        if old_path.exists():
            old_dest = _unique_dest(dirs['duplicados'], old_path.stem)
            shutil.move(str(old_path), str(old_dest))
            state.update_path(conn, original['id'], old_dest)

    if ident.supplier_key != 'unknown':
        date_part = (ident.doc_date.replace('-', '') if ident.doc_date
                     else f'{datetime.now().year}XXXX')
        stem = f'{date_part}_{ident.supplier_key.capitalize()}'
        dest = _unique_dest(dirs['integrated'], stem)
        action, status = 'INTEGRATED', 'queued'
    else:
        dest = _unique_dest(dirs['review'], pdf_path.stem)
        action, status = 'REVIEW', 'review_folder'

    if not dry_run:
        shutil.move(str(pdf_path), str(dest))
    fid = state.insert_file(conn, md5=md5, original_name=pdf_path.name,
                            current_path=str(dest), nif=ident.nif,
                            atcud=ident.atcud, doc_ref=ident.doc_ref,
                            supplier_key=ident.supplier_key or None,
                            doc_date=ident.doc_date, doc_type=ident.doc_type,
                            id_source=ident.id_source, status=status)
    for i, hit in enumerate(ident.qr_hits):
        state.add_qr(conn, fid, hit.page, hit.raw, hit.fields, is_primary=(i == 0))
    if verdict == 'supersede':
        state.supersede(conn, original['id'], fid)
        action = 'SUPERSEDE'
    return {'action': action, 'new_name': dest.name, 'file_id': fid}
```

Note the `_nc` composition: `identify` appends `qr.doc_suffix` to the base key (guard against double `_nc` when the registered key already ends with it).

- [ ] **Step 4: Run tests** — `python -m pytest tests/test_pipeline_phase_a.py -v` → PASS (fix until green).
- [ ] **Step 5: Commit** — `git commit -am "[ADD] invoice_classification: pipeline.py Phase A (QR identify, dedup, enqueue)"`

---

### Task 8: `pipeline.py` Phase B — drain: extract, gate, file, send

**Files:**
- Modify: `invoice_classification/pipeline.py` (append)
- Test: `invoice_classification/tests/test_pipeline_phase_b.py`

**Interfaces:**
- Consumes: `state.pending/record_error/save_extraction/set_status/update_path/mark_sent`, `validation.validate`, `artifacts.build_artifact/write_json`, `odoo_send.build_payload/send/resolve_drive_id/drive_preview_url`, `claude_extract.extract/PermanentExtractionError/MODEL`.
- Produces:
  - `file_into_month(path: Path, integrated_dir: Path, doc_date: str) -> Path` — moves to `integrated_dir/YYYY/MM/<name>` (mkdir -p), returns new path.
  - `drain(conn, dirs, odoo_cfg, extractor=None, poster=None, resolver=None, cap=5, dry_run=False) -> dict` — stats `{'extracted','sent','needs_review','retried','failed'}`. `extractor(pdf_path, supplier)` defaults to a wrapper over `claude_extract.extract`; `poster`/`resolver` pass through to `odoo_send.send`/`resolve_drive_id`. `dirs` additionally carries `'extracted'` (the EXTRACTED/ folder). `odoo_cfg` is `api_config.get_odoo()`'s dict.
  - Primary QR fields for validation come from `qr_codes` where `is_primary=1` (parsed_json).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_pipeline_phase_b.py
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
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement — append to `pipeline.py`**

```python
def file_into_month(path, integrated_dir, doc_date):
    year, month = doc_date[:4], doc_date[5:7]
    target = Path(integrated_dir) / year / month
    target.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(target, Path(path).stem)
    shutil.move(str(path), str(dest))
    return dest


def _primary_qr(conn, file_id):
    row = conn.execute("SELECT parsed_json FROM qr_codes WHERE file_id=? AND"
                       " is_primary=1", (file_id,)).fetchone()
    return json.loads(row['parsed_json']) if row else None


def _default_extractor(pdf_path, supplier):
    import api_config
    import claude_extract
    cfg = api_config.get_anthropic()
    return claude_extract.extract(pdf_path, supplier, api_key=cfg['api_key'])


def drain(conn, dirs, odoo_cfg, extractor=None, poster=None, resolver=None,
          cap=5, dry_run=False):
    import claude_extract
    import artifacts
    import odoo_send
    import validation

    extractor = extractor or _default_extractor
    resolver = resolver or odoo_send.resolve_drive_id
    stats = {'extracted': 0, 'sent': 0, 'needs_review': 0, 'retried': 0, 'failed': 0}

    for row in state.pending(conn, cap=cap):
        fid = row['id']
        attempts = row['attempts'] + 1
        if dry_run:
            logger.info(f"[DRY RUN] would extract {row['current_path']}")
            continue
        try:
            result, raw_json = extractor(row['current_path'], row['supplier_key'])
        except claude_extract.PermanentExtractionError as e:
            state.record_error(conn, fid, str(e), cap=cap, permanent=True)
            stats['failed'] += 1
            continue
        except Exception as e:
            state.record_error(conn, fid, str(e), cap=cap)
            new_status = conn.execute("SELECT status FROM files WHERE id=?",
                                      (fid,)).fetchone()[0]
            stats['retried' if new_status == 'retry' else 'failed'] += 1
            continue

        stats['extracted'] += 1
        extraction = result.model_dump()
        qr_fields = _primary_qr(conn, fid)
        verdict = validation.validate(extraction, qr_fields, row['supplier_key'])
        state.save_extraction(conn, fid, claude_extract.MODEL, raw_json,
                              json.dumps(verdict))
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        basename = Path(row['current_path']).stem

        if verdict['status'] != 'ok':
            state.set_status(conn, fid, 'needs_review')
            stats['needs_review'] += 1
            art = artifacts.build_artifact(dict(row), qr_fields, extraction, verdict,
                                           {'sent_at': None, 'status': 'held'},
                                           claude_extract.MODEL, attempts)
            artifacts.write_json(dirs['extracted'], basename, art)
            continue

        # validated: file into YYYY/MM before resolving the Drive link
        try:
            doc_date = row['doc_date'] or extraction.get('date')
            new_path = file_into_month(row['current_path'], dirs['integrated'],
                                       doc_date)
            state.update_path(conn, fid, new_path)
            row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            marker = '/ScanSnap/'
            rel = (str(new_path).split(marker, 1)[1] if marker in str(new_path)
                   else Path(new_path).name)
            drive_id = resolver(f"{odoo_cfg['drive_remote']}/{rel}")
            url = odoo_send.drive_preview_url(drive_id) if drive_id else None
            payload = odoo_send.build_payload(dict(row), extraction, document_url=url)
            odoo_result = odoo_send.send(payload, odoo_cfg['webhook_url'],
                                         odoo_cfg['api_key'], poster=poster)
        except Exception as e:
            state.record_error(conn, fid, f'odoo/filing: {e}', cap=cap)
            stats['retried'] += 1
            art = artifacts.build_artifact(dict(row), qr_fields, extraction, verdict,
                                           {'sent_at': None, 'status': None},
                                           claude_extract.MODEL, attempts)
            artifacts.write_json(dirs['extracted'], basename, art)
            continue

        state.mark_sent(conn, fid, json.dumps(odoo_result))
        sent_row = conn.execute("SELECT odoo_sent_at FROM extractions WHERE"
                                " file_id=?", (fid,)).fetchone()
        art = artifacts.build_artifact(dict(row), qr_fields, extraction, verdict,
                                       {'sent_at': sent_row['odoo_sent_at'],
                                        'status': 'sent'},
                                       claude_extract.MODEL, attempts)
        artifacts.write_json(dirs['extracted'], basename, art)
        stats['sent'] += 1
    return stats
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/ -v` → all PASS.
- [ ] **Step 5: Commit** — `git commit -am "[ADD] invoice_classification: pipeline.py Phase B drain (extract, gate, file, send)"`

---

### Task 9: wire into `classifier.py` + neutralize legacy APIs

**Files:**
- Modify: `invoice_classification/classifier.py` — `process_and_move()` (lines ~1387-1534) and the `__main__` `process` command (~1588-1680)
- Test: `invoice_classification/tests/test_classifier_wiring.py`

**Interfaces:**
- Consumes: `pipeline.handle_new_file/drain`, `state.connect/seed_suppliers`, `api_config.legacy_apis_enabled/get_odoo`, `InvoiceClassifier.SUPPLIERS`.
- Produces:
  - `process_and_move(classifier, source_dir, matched_dir, review_dir, integrated_dir, dry_run=False, upload=False, conn=None, dirs_extra=None)` — same signature plus optional `conn`/`dirs_extra` (tests inject; production builds them). Per file: calls `pipeline.handle_new_file` with an `ocr_fallback` adapter; legacy `upload_to_api` call only when `api_config.legacy_apis_enabled()`.
  - `ocr_fallback` adapter: `lambda p: _ocr_classify(classifier, p)` where `_ocr_classify` runs `classifier.classify(pdf_path)` and returns `(result.supplier, result.invoice_date)` (supplier already carries `_nc` suffix from `_detect_document_type_suffix`).
  - `run_process(source_dirs: list[Path], db_path, dry_run, drain_queue=True) -> None` — new top-level: opens DB once, seeds suppliers from `InvoiceClassifier.SUPPLIERS` (`{key: (p.nif, p.display_name)}`), Phase A per folder, then one `pipeline.drain`. The `__main__` `process` branch calls this.
  - `dirs` layout per source folder: `integrated=source/INTEGRATED`, `review=source/REVIEW`, `duplicados=source.parent/'Duplicados'` if source name is `ScanSnap` else `source/'Duplicados'`; `extracted` is always `<ScanSnap root>/EXTRACTED`. `state.db` lives next to `classifier.py`.

- [ ] **Step 1: Write failing tests** (monkeypatch a fake classifier + fake pipeline internals; assert wiring only)

```python
# tests/test_classifier_wiring.py
import classifier as clf
import pipeline
import state


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
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Modify `classifier.py`.** Inside `process_and_move`, replace the whole per-file body (the classify/rename/move/upload block, lines ~1438-1523) with:

```python
        try:
            out = pipeline.handle_new_file(
                pdf_path, conn, dirs_extra,
                ocr_fallback=lambda p: _ocr_classify(classifier, p),
                dry_run=dry_run)
            action = out['action']
            key = {'INTEGRATED': 'integrated', 'SUPERSEDE': 'integrated',
                   'REVIEW': 'review', 'DUPLICATE': 'duplicate'}[action]
            stats[key] = stats.get(key, 0) + 1
            stats['files'].append({'original': pdf_path.name,
                                   'new_name': out['new_name'], 'action': action})
            logger.info(f"{pdf_path.name} -> {action}/{out['new_name']}")
            if upload and action in ('INTEGRATED', 'SUPERSEDE') and not dry_run:
                import api_config
                if api_config.legacy_apis_enabled():
                    dest = dirs_extra['integrated'] / out['new_name']
                    upload_result = upload_to_api(dest, 'unknown')
                    ...  # existing success/failure logging block, unchanged
        except Exception as e:
            stats['errors'] += 1
            logger.error(f"Error processing {pdf_path.name}: {e}")
            stats['files'].append({'original': pdf_path.name, 'error': str(e),
                                   'action': 'ERROR'})
```

with, above `process_and_move`:

```python
import pipeline
import state


def _ocr_classify(classifier, pdf_path):
    """Adapter: Tesseract pipeline -> (supplier_key_or_'unknown', YYYYMMDD|None)."""
    result = classifier.classify(Path(pdf_path))
    return result.supplier, result.invoice_date
```

`process_and_move` gains keyword args `conn=None, dirs_extra=None`; when `conn is None` it opens `state.connect(Path(__file__).parent / 'state.db')` and seeds suppliers (`state.seed_suppliers(conn, {k: (p.nif, p.display_name) for k, p in InvoiceClassifier.SUPPLIERS.items()})`); when `dirs_extra is None` it builds the dict per the dirs layout in Interfaces. Keep the `matched_dir` parameter (callers pass it) but new files never route there.

In `__main__`'s `process` branch, after the per-folder `process_and_move` loop, add the Phase B drain:

```python
        import api_config
        conn = state.connect(Path(__file__).parent / 'state.db')
        dirs_extra = ...  # same builder as above, for the ScanSnap root
        drain_stats = pipeline.drain(conn, dirs_extra, api_config.get_odoo(),
                                     dry_run=args.dry_run)
        logger.info(f"Drain: {drain_stats}")
```

(Match the actual argparse variable names at `classifier.py:1622-1672` when editing.)

- [ ] **Step 4: Run the full suite** — `python -m pytest tests/ -v` → all PASS.
- [ ] **Step 5: Commit** — `git commit -am "[MOD] invoice_classification: wire QR pipeline + drain into classifier; neutralize Parseur/Docupipe behind legacy_apis flag"`

---

### Task 10: deploy.sh, README, acceptance instructions

**Files:**
- Modify: `invoice_classification/deploy.sh`
- Modify: `invoice_classification/README.md`

**Interfaces:** consumes everything; produces the deployable service.

- [ ] **Step 1: deploy.sh.** (a) In the rsync/app-sync step, add `--exclude='state.db*'` so deploys never clobber the ledger (same treatment as `config.json` — find the existing exclude list around the `rsync`/copy block, near `deploy.sh:154`). (b) After the mount setup, create the new folders idempotently as the service user: `sudo -u "$SERVICE_USER" mkdir -p "$GDRIVE_MOUNT/ScanSnap/EXTRACTED" "$GDRIVE_MOUNT/Duplicados"` (place next to where INTEGRATED dirs are handled). (c) No opencv special-casing changes needed beyond requirements.txt (already headless).

- [ ] **Step 2: README.** Update the Features section: QR-first identification, Claude extraction (`claude-opus-5`), SQLite retry queue (cap 5), QR-gated Odoo delivery, `EXTRACTED/` JSON artifacts, `INTEGRATED/YYYY/MM/` filing, md5+QR dedup with supersede-on-rescan. Mark Parseur/Docupipe as neutralized (`legacy_apis.enabled`). Add an **Acceptance test** section:

```markdown
## Acceptance test (manual)

1. Copy 3-5 PDFs from the reserved QA test set into a scratch folder.
2. `python classifier.py process /path/to/scratch --dry-run` — verify intended
   actions in the log (no moves, no API calls).
3. `python classifier.py process /path/to/scratch` — verify: files land in
   INTEGRATED/YYYY/MM/, EXTRACTED/*.json written, Odoo drafts created,
   `sqlite3 state.db "SELECT id,status,supplier_key FROM files"` shows 'sent'.
4. Re-copy one of the same PDFs and re-run: it must land in Duplicados/ with
   status 'duplicate'.
```

- [ ] **Step 3: Run full suite one last time** — `python -m pytest tests/ -v` → PASS.
- [ ] **Step 4: Commit** — `git commit -am "[MOD] invoice_classification: deploy + README for v2 (QR, Claude, retry queue)"`

---

## Self-Review Notes

- Spec coverage: QR-first id (T2/T7), Tesseract fallback unchanged (T7/T9 adapter), Claude replaces APIs (T3/T9), capped retries + permanent refusal (T1/T8), auto-register (T7), Odoo gate (T4/T8), central JSON (T5/T8), year/month filing pre-Odoo (T8), md5+QR dedup with supersede (T7), legacy flag (T6/T9), deploy/state.db preservation + EXTRACTED/Duplicados dirs (T10), MATCHED retirement (T9 — param kept, unused for new files), QA folder untouched (acceptance is manual, T10).
- Type consistency: `files` Row keys used in T5/T7/T8 match T1's schema; `Identified`/`QrHit`/`Extraction` names consistent across tasks; `dirs` dict keys (`integrated`, `review`, `duplicados`, `extracted`) consistent in T7/T8/T9.
- Known judgment call for the implementer: in T7 `identify`, guard the `_nc` suffix composition so an auto-registered key never gets a double suffix.
