# Product matching: automation + verification — design

Date: 2026-08-24
Status: **proposal — awaiting approval** (phases independently approvable)
Repos touched: `addons-hio/hio_purchase_management` (main), `invoice_classification` + `QA_Faturas` (phase 3 only)

## Problem (owner's words)

The biggest pain is the lack of automation and finding out whether everything
is mapped correctly. New products needing manual mapping is accepted — but
matching must run right after import, and checking should mean reviewing
exceptions, not clicking through invoices.

## Current state (verified 2026-08-24)

- Matching cascade lives twice (drifted copies in `action_process_invoice`
  and `action_auto_prematch`): exact normalized designation (per partner) →
  pg_trgm fuzzy `%` `limit=1` → product-name search → error.
- Automation = a 6-hour cron (`action_auto_prematch_cron`, draft +
  beverage/makro only). An unmatched product sets `status='error'` mixed
  with genuine errors; there is no per-invoice "products pending" signal and
  no global worklist.
- A mapping learned in the wizard does **not** re-apply to other pending
  invoices with the same product.
- `supplier_code` is on 5 581/7 227 invoice lines and is used **nowhere**
  (not matched on, not shown in the wizard, not stored in the match table).
- `import.ocr.match.product`: 761 entries, keyed (partner, designation);
  extra per-product price flags (`unitpriceincorrect`, `usediscount`).
- Products `Despesa Refeição 6%/13%/23%` exist (1240/1140/1139) — the
  name-exact fallback will auto-learn them per partner on first receipt.

## Phase 1 — match on import + exceptions surface (the core ask)

All in `hio_purchase_management`:

1. **One cascade.** Extract the product-matching logic into a single helper
   (`import.ocr.invoice._match_line_products()`), used by webhook, prematch
   and process — the two existing copies have already drifted.
2. **Match at import time.** The Claude webhook controller calls the
   partner+product prematch right after creating the lines (same
   transaction, guarded so a matching failure can never break ingestion).
   The webhook **response** gains `products_unknown: <n>`. The 6h cron stays
   as a safety net / retro pass.
3. **Status semantics.** Unmatched products stop masquerading as
   `status='error'`. New stored fields on `import.ocr.invoice`:
   - `unmatched_product_count` (lines without `product_id`)
   - `product_match_state`: `matched` / `pending_products` / `no_lines`
   recomputed from `invoice_line_ids.product_id`. Prematch sets
   `prematched` on partner/reconciliation success regardless of product
   gaps; only `action_process_invoice` (which truly cannot build the PO)
   keeps erroring.
4. **The verification view.** List filter + decoration «Produtos por
   mapear» on invoices, and a menu opening `import.ocr.invoiceline`
   filtered `product_id = False` + invoice in draft/prematched/error,
   grouped by designation — each row opens the existing wizard. "Is
   everything mapped?" becomes one glance at one list.

## Phase 2 — supplier_code first + learn-once-cascades

1. `import.ocr.match.product` gains `supplier_code` (indexed). Cascade
   order becomes: **(partner, supplier_code) exact** → (partner,
   designation_normalized) exact → fuzzy fallback → name-exact learn →
   unmatched placeholder (now storing the code too).
2. Wizard shows the line's `supplier_code` (readonly) and saves it on the
   match row.
3. **Cascade-on-learn:** creating/updating a match row with a product
   re-matches every product-less line of pending invoices for that partner
   with the same code or normalized designation, and recomputes the
   invoice states — map once, every pending invoice clears.
4. **Backfill action (one-time):** stamp `supplier_code` onto existing
   entries from historical lines where (partner, normalized designation,
   product) agree on an unambiguous code.

## Phase 3 — visibility inside the daily QA flow (small, two repos)

- `invoice_classification`: `odoo_send.send`'s stored result already flows
  into the artifact — pass `products_unknown` through into the artifact's
  `odoo` block. README updated.
- `QA_Faturas`: badge on `ok_sent` rows gains «N produtos por mapear».
  README updated.
- Odoo remains the owner of the catalog and the mapping action; the
  standalone only surfaces the count (per the 2026-08-24 discussion:
  matching QA stays in Odoo).

## Phase 4 (optional, later)

- Fuzzy guardrail: replace bare `%`/`limit=1` with SQL `similarity()` +
  threshold; below-threshold → unmatched (worklist) instead of a silent
  possibly-wrong match.
- Price arithmetic: for `origin=claude` records, verify
  `unit_price × qty × (1−disc) ≈ line_total` per line and choose the price
  interpretation automatically, retiring the per-product
  `unitpriceincorrect`/`usediscount` flags for pipeline-sourced invoices.

## Testing & deployment

- Extend `hio_purchase_management/tests/` (cascade order, code-first hit,
  cascade-on-learn, state fields, webhook response) — run against a
  throwaway DB: `odoo-bin -d test_hio --test-tags hio_purchase_management
  --stop-after-init` on the VPS; never against `holeinone`.
- Deploy: commit to `addons-hio` (git, origin `oandremendes/addons-hio`) →
  pull on VPS → `odoo-bin -u hio_purchase_management -d holeinone
  --stop-after-init` + service restart (≈1 minute of downtime — needs an
  agreed moment). Rollback = git revert + re-upgrade.
- READMEs updated in the same commits as the code they describe:
  `hio_purchase_management/README.md` (import flow, pre-matching, new
  states/worklist, supplier-code matching), plus the phase-3 repos.

## Behaviour changes to sign off explicitly

1. Prematch no longer marks invoices `error` for unmatched products — those
   move to `pending_products` + the worklist. (Anyone relying on the error
   list to find them switches to the new filter.)
2. Matching now runs at webhook time — records appear already
   partner/product-resolved seconds after scanning.
3. Phase 4's fuzzy threshold trades a few silent auto-matches for worklist
   entries — fewer wrong POs, slightly more mapping clicks.

## Decision log

- 2026-08-24: plan written after code+data study; recommendation: implement
  **Phases 1+2 together** (one module upgrade), then 3; hold 4.
