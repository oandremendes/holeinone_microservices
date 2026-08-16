# Invoice Classification v2 — QR-first identification, Claude extraction, retry queue

**Status:** approved design, 2026-08-17
**Scope:** evolve `invoice_classification/` in place. Same VPS, same systemd timer + rclone mount, same discovery model.

## Goals

1. Identify supplier, date, and document type from the invoice's fiscal QR code (Portaria 195/2020) first; fall back to the existing Tesseract/NIF/keyword pipeline.
2. Replace Parseur/Docupipe with Claude line extraction (Parseur/Docupipe clients kept but neutralized behind a config flag).
3. Persist pipeline state in SQLite so failed extractions are visible and retried on later runs (capped), instead of being silently lost after the file move.
4. Deliver results to Odoo (gated on QR validation) and as JSON artifacts in a central Drive folder.
5. Detect duplicates by file hash **and** by fiscal document identity from the QR.

## Decisions log (agreed with owner)

| Decision | Choice |
|---|---|
| Extraction backend | Claude replaces Parseur **and** Docupipe. Old clients stay in repo, dispatch preserved behind `"legacy_apis": {"enabled": false}` in config. |
| Results destination | POST to Odoo webhook **and** JSON artifact per invoice in central `ScanSnap/EXTRACTED/` folder (not sidecars). |
| Pipeline state | New `state.db` (stdlib sqlite3, single file in app dir). No dependency on QA_Faturas' DB. |
| Retry policy | Capped at 5 attempts; attempts + last error stored; after cap → `failed`, visible, no silent retry-forever. Refusals (`stop_reason == "refusal"`) are permanent failures, not retried. |
| Unknown QR NIF | Auto-register supplier (NIF + OCR-derived name), proceed normally. |
| Odoo gate | Send only when Claude's extraction agrees with the QR (±2c, supplier quirks); mismatch → `needs_review`, held. |
| File moves | One move at classification (as today). Validated invoices additionally filed into `INTEGRATED/YYYY/MM/` before the Odoo POST; `needs_review`/`failed` stay flat in `INTEGRATED/` for visibility. |
| Duplicates | md5 ledger (exact rescan) + unique `(nif, atcud)` from QR (same paper, different scan). Supersede rule below. |
| Model | `claude-opus-5`, key in `config.json` under `"anthropic"`. |
| Test data | Invoices in the owner's QA folder are reserved for **manual** acceptance testing — the service must not process them automatically during development. |

## Architecture

Two phases per oneshot run (same timer, ~5 min):

```
Phase A — classify (per new PDF at top level of ScanSnap/ and ScanSnap/Receipts/):
  1. md5 → in ledger?                    → duplicate handling
  2. QR decode (all fiscal QRs in PDF)
  3. (nif, atcud) → in ledger?           → duplicate handling
  4. QR hit  → supplier by NIF (auto-register unknown), date from F, _nc from D
     QR miss → existing Tesseract pipeline unchanged (NIF substring, keywords,
               templates, date regexes)
  5. identified (QR or OCR) → rename YYYYMMDD_Supplier[_nc].pdf → move to
     INTEGRATED/ → files row status='queued' (QR payloads stored).
     unknown → move to REVIEW/ unrenamed → files row status='review_folder'
     (md5/QR recorded so a better rescan supersedes it). The MATCHED/ folder
     is retired for new files — with Claude as the extractor, "identified but
     no integration" no longer exists; the routing branch remains only for
     the neutralized legacy_apis path.

Phase B — drain queue (every run — this is what "send at a later date,
usually after a scan" means operationally):
  for each files row with status IN ('queued','retry') and attempts < 5:
    a. Claude extract (whole PDF base64, QA_Faturas prompt/schema/hints)
    b. validate vs QR + soma_linhas
    c. pass → move PDF to INTEGRATED/YYYY/MM/ → write EXTRACTED/<basename>.json
             → POST Odoo → status='sent'
       validation fail → JSON written with needs_review; status='needs_review'; no Odoo
       transient error → attempts+1, last_error, status='retry' (→ 'failed' at cap)
       refusal / corrupt PDF → status='failed' immediately
```

Discovery stays positional (`-maxdepth 1` on the scan folders); everything after the classification move is driven by the DB, never by folder position. Subfolders of `INTEGRATED/` are invisible to discovery, so year/month filing is safe.

## Modules

| File | Role |
|---|---|
| `qr.py` (new) | Render pages with PyMuPDF; decode with zxing-cpp fast path (216 dpi, first/last page); fallback ladder: PIL MaxFilter erosion (3/5) + OpenCV WeChat CNN at 300 dpi, first/last pages only. Collect **all** fiscal QRs per PDF (multi-document PDFs, e.g. Su Eletricidade). Parse `A:…*B:…` payload into a dict. |
| `state.py` (new) | SQLite schema + queue operations (enqueue, claim, record attempt, supersede, ledger lookups). WAL mode; single-process (oneshot) so no locking complexity. |
| `claude_extract.py` (new, ported from QA_Faturas) | Same Pydantic models (`Extraction`, `Line`, `TaxLine`, integer cents), same prompt + per-supplier hints, one retry on invalid JSON. `stop_reason == 'refusal'` raised as permanent. |
| `validation.py` (new) | QR↔extraction checks: total (±2c), IVA (±2c + supplier quirk table), ref (extracted ref vs QR `G`, and vs `G + " / " + H`), date; plus `soma_linhas` (Σ line totals == total). Returns status + per-check booleans for the JSON. |
| `odoo_send.py` (new, ported from QA_Faturas `odoo_export`) | `build_payload` (plain JSON + `api_key` in body, md5 included, EUR strings from cents), Drive preview link resolved via rclone **after** year/month filing, POST with timeout. |
| `artifacts.py` (new) | Atomic JSON writes (tmp + rename) to `EXTRACTED/`. |
| `classifier.py` (modified) | QR stage inserted at top of `classify()`; `process_and_move()` enqueues instead of uploading; Phase B drain called at end of `process` command. |
| `api_config.py` / clients (modified minimally) | `upload_to_api()` kept; called only when `config.legacy_apis.enabled` is true. |

### Known IVA quirks (from the 1181-invoice prototype, 2026-08-16)

- Supermarkets (Continente/Lidl/…): ±1–2c rounding → the ±2c tolerance.
- Teófilo `ZFV1`: QR field N reports exactly 2× the real IVA.
- Oriental/Semino restaurant software: field N contains the **base**, not the IVA.
- Stamp-duty documents (BCP): N > 0 while IVA is genuinely 0.
Quirks live in a small table in `validation.py` keyed by supplier; a quirk match counts as validation pass with a note in the JSON.

## Database schema (`state.db`)

```sql
CREATE TABLE files (
  id INTEGER PRIMARY KEY,
  md5 TEXT UNIQUE NOT NULL,
  nif TEXT,                -- from QR (or OCR match)
  atcud TEXT,              -- QR field H (document identity)
  doc_ref TEXT,            -- QR field G
  original_name TEXT NOT NULL,
  current_path TEXT NOT NULL,
  supplier_key TEXT,
  doc_date TEXT,           -- YYYY-MM-DD
  doc_type TEXT,           -- QR field D (FT/FS/NC/…)
  id_source TEXT NOT NULL, -- 'qr' | 'ocr' | 'none'
  status TEXT NOT NULL,    -- queued|retry|sent|needs_review|failed|duplicate|review_folder
  superseded_by INTEGER REFERENCES files(id),
  duplicate_of INTEGER REFERENCES files(id),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_doc_identity ON files(nif, atcud)
  WHERE atcud IS NOT NULL AND status != 'duplicate';

CREATE TABLE qr_codes (      -- all fiscal QRs found in the PDF
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id),
  page INTEGER, raw_payload TEXT NOT NULL, parsed_json TEXT NOT NULL,
  is_primary INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE extractions (
  file_id INTEGER PRIMARY KEY REFERENCES files(id),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  model TEXT, extracted_at TEXT,
  result_json TEXT,        -- full Claude extraction
  validation_json TEXT,    -- per-check booleans + notes
  odoo_sent_at TEXT, odoo_result TEXT
);

CREATE TABLE suppliers (   -- seeded from the 87 hardcoded profiles on first run
  nif TEXT PRIMARY KEY,
  supplier_key TEXT NOT NULL,
  display_name TEXT,
  auto_registered INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
```

## Duplicate handling

Checked in Phase A, before any Claude spend:

1. **md5 match** → duplicate of that row.
2. **QR identity match** `(nif, atcud)` (fallback `(nif, doc_ref)` when H absent) on any fiscal QR in the PDF → duplicate of that row.

Then, by the original's state:
- Original healthy (`queued`/`retry`/`sent`/`needs_review`): new file moved to `Duplicados/` (sibling of ScanSnap, as in QA_Faturas), row `status='duplicate'`, `duplicate_of` set.
- Original bad (`failed`, or `id_source='none'` stuck in REVIEW): **new scan supersedes** — old file moved to `Duplicados/`, old row marked `superseded_by`, new row queued. Rescanning a problem invoice is therefore the fix workflow.

No-QR PDFs get md5-only dedup.

## JSON artifact (`ScanSnap/EXTRACTED/<basename>.json`)

Same basename as the PDF (uniqueness via existing `_1`/`_2` suffixes). Written atomically after every extraction attempt that produced data; rewritten on re-extraction or Odoo status change. Shape:

```json
{
  "pdf": "INTEGRATED/2026/03/20260315_Novadis.pdf",
  "processed_at": "…", "model": "claude-opus-5", "attempts": 1,
  "identification": {"supplier_key": "novadis", "nif": "504350900",
                      "date": "2026-03-15", "doc_type": "FT", "source": "qr"},
  "qr": {"A": "…", "G": "…", "H": "…", "N": "…", "O": "…", "…": "all fields"},
  "extraction": {"supplier_name": "…", "invoice_ref": "…", "date": "…",
                  "base_cents": 0, "iva_cents": 0, "total_cents": 0,
                  "lines": [], "taxes": []},
  "validation": {"status": "ok|needs_review",
                  "checks": {"total_vs_qr": true, "iva_vs_qr": true,
                              "ref_vs_qr": true, "soma_linhas": true},
                  "notes": []},
  "odoo": {"sent_at": "…", "status": "sent|held"}
}
```

All money integer cents. The folder is the complete, externally-consumable record; `state.db` is the service's private ledger.

## Config additions (`config.example.json`)

```json
{
  "parseur":  {"api_key": "…"},
  "docupipe": {"api_key": "…"},
  "legacy_apis": {"enabled": false},
  "anthropic": {"api_key": "…", "model": "claude-opus-5"},
  "odoo": {"webhook_url": "…", "api_key": "…", "drive_remote": "gdrive:ScanSnap"}
}
```

## Deploy changes

- `requirements.txt`: + `pymupdf`, `zxing-cpp`, `opencv-contrib-python-headless` (replaces `opencv-python` — WeChat decoder needs contrib; headless for VPS), `anthropic`, `pydantic`.
- `deploy.sh`: no structural change; preserves `state.db` across deploys (same treatment as `config.json`); creates `EXTRACTED/` and `Duplicados/` if missing.
- Timer, rclone mount, systemd units unchanged.

## Testing

- Unit tests (pytest, new `tests/`): QR payload parsing; validation gate incl. quirk table and ±2c; date/identity extraction from QR fields; queue state machine with a **fake extractor** (as QA_Faturas does) covering retry cap, refusal short-circuit, supersede, duplicate paths; atomic JSON writer.
- Manual acceptance: owner runs the pipeline against the reserved QA-folder invoices with a `--dry-run` (no Odoo, no moves) then a live pass. The service must not pick these up on its own before that.

## Out of scope

- Removing Parseur/Docupipe code (kept, neutralized).
- Any change to QA_Faturas (it can consume `EXTRACTED/` later if wanted).
- Odoo-side duplicate rejection (payload carries md5; server action may use it later).
- e-Fatura CSV matching on the VPS (stays a QA_Faturas concern).
