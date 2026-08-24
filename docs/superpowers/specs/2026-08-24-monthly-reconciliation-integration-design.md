# Monthly Reconciliation ⇄ pipeline/QA_Faturas integration — proposal

Date: 2026-08-24
Status: **proposal — discussed, not yet approved** (phases below are staged so
each can be approved independently)

## Context (verified 2026-08-24)

Two systems ingest the same monthly e-Fatura CSV export and match it against
invoice records:

| | QA_Faturas (`efatura/` CSVs) | Odoo `hio_purchase_management` Monthly Reconciliation |
|---|---|---|
| Unit | `efatura` rows | `invoice.reconciliation.line` per month record |
| Matches against | the PDF archive (review flow, candidates NIF+date±2d) | `import.ocr.invoice` **and** manual `account.move` bills |
| Matching | exact NIF + date window, human picks candidate; new: auto-approve for pipeline-validated invoices | fuzzy: partner lookup, invoice-number containment, 2-of-3 amounts ±0.01, day/month-flip date heuristics, confidence 0–100, per-partner flags (`ignore_zero_vat`, `match_using_vat_only`, match-after-slash) |
| Role | archive completeness («Em falta», «pedida»), review assistance | **hard gate**: beverage/makro `action_process_invoice` requires a reconciliation link (or `allow_without_reconciliation` + manual tipification); also the **credit-note oracle** (`is_credit_note` ← line `Tipo == nota_credito`); auto-prematch cron each 6h |

### Findings

1. **Drift**: Odoo reconciliations stop at 2025-12; QA_Faturas carries the
   2026 CSVs. Two upload rituals, both half-maintained.
2. **Gate landmine**: everything the pipeline pushes is `invoice_type
   beverage`/`makro` — including the ~700 receipt/restaurant records from the
   QA/OK rollout. None can be processed to POs/bills until 2026 reconciliation
   months exist and match (or `allow_without_reconciliation` is bulk-set).
   The Q1 credit-note tipification question resolves the same way: NC typing
   comes from the reconciliation line.
3. **Unused shared key — ATCUD**: reconciliation lines store `at_code`
   (parsed from the CSV's «Nº Fatura / ATCUD»); the pipeline reads the same
   code from the fiscal QR (field `H`) on every document and keeps it in
   `state.db`/artifacts — but the webhook payload does not send it, so Odoo
   falls back to fuzzy matching built for unreliable OCR data. For
   QR-sourced records the match can be a deterministic equality join.

## Plan

### Phase 0 — no code (owner action)
Upload the 2026 monthly CSVs (Jan–Aug) into Monthly Reconciliation and run
`action_recheck_unmatched`. Un-gates the pushed backlog, fixes NC typing;
even the current fuzzy matcher should link most QR-exact records.

### Phase 1 — ATCUD everywhere (small, highest value)
- Pipeline: `odoo_send.build_payload` includes `atcud` (from the files row);
  webhook stores it on `import.ocr.invoice` (new field `at_code`).
- Odoo: `_find_reconciliation_match` (invoice side) and `find_invoice`
  (reconciliation side) try **exact `at_code` equality first**, confidence
  100, no heuristics; fuzzy logic remains only as fallback for ATCUD-less
  records (manual bills, pre-QR history).
- Effect: pipeline-sourced invoices — including restaurants and credit
  notes — reconcile deterministically; most per-partner matching quirks
  become legacy for QR-sourced data.

### Phase 2 — single ingestion point
CSV is uploaded once, in Odoo (it is already the pre-processing block).
QA_Faturas stops reading `efatura/` and pulls reconciliation lines at
startup into its existing `efatura` table (source swap only — candidates,
«Em falta», «pedida» UX unchanged). Transport: XML-RPC pull, or a Drive
snapshot dropped by the pipeline (consistent with the no-direct-network
Drive-channel architecture). Bonus: QA gains visibility of Odoo's match
status (e.g. "declared + matched to manual bill + no PDF" as an Em-falta
category).

### Phase 3 (optional) — decisions flow back
A QA human link (invoice ↔ e-Fatura entry) or a second approval confirms the
corresponding reconciliation line (`matched` / `manually_approved`) — one
click serves both systems. Candidate transport: extend the `APPROVED/`
marker with the linked `at_code`.

## Explicitly out / not recommended

- Retiring QA_Faturas' e-Fatura features in favour of Odoo views: the
  archive-completeness workflow is document-side, Odoo's `not_found` list is
  full of non-beverage noise, and the keyboard review flow is where the work
  happens.

## Open design tension

The reconciliation gate treats "no e-Fatura entry" as an error, but entries
lag the paper by up to ~6 weeks while the pipeline delivers day-of-scan.
Either the gate learns a "declared-pending" state for young invoices, or
final processing simply waits for the monthly CSV (arguably the point of a
pre-processing block). Decide during Phase 1 review.

## Decision log

- 2026-08-24: proposal written after code study (`invoice_reconciliation.py`,
  `import_ocr_invoice.py`, live data: 12 months of 2025 reconciliations,
  1738 lines, 795 matched / 892 not_found). Awaiting owner decision on
  Phase 1.
