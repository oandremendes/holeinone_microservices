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


def identify(pdf_path, conn, ocr_fallback, dry_run=False):
    hits = qr_decode(pdf_path)
    if hits:
        f = hits[0].fields          # primary = first fiscal QR
        nif = f.get('A')
        sup = state.supplier_for_nif(conn, nif) if nif else None
        if sup is None and nif:
            key = f'nif{nif}'
            if dry_run:
                logger.info('[DRY RUN] would register_supplier nif=%s key=%s', nif, key)
            else:
                state.register_supplier(conn, nif, key, f'NIF {nif}')
        else:
            key = sup['supplier_key'] if sup else 'unknown'
        # guard against a double "_nc": only append the credit-note suffix
        # when the (registered or auto-generated) key doesn't already carry it
        suffix = '' if key.endswith('_nc') else qr_mod.doc_suffix(f)
        return Identified(supplier_key=key + suffix,
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
    """Returns ('new', None) | ('duplicate', original_row) | ('supersede', original_row).

    The original row's own md5 tells the caller (handle_new_file) which
    ledger matched: original['md5'] == md5 means an exact-bytes match (the
    files.md5 UNIQUE constraint forbids inserting a second row for it);
    a mismatch means the match came from QR identity (nif+atcud/doc_ref)
    on a file with different bytes, whose md5 is free to insert.
    """
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
    """Full Phase A for one file. When dry_run=True this is a complete no-op
    on persistent state: no file is moved and no row is written/updated in
    any table (files, qr_codes, suppliers) -- only the would-be action dict
    is computed and returned (with file_id=None), and [DRY RUN] lines are
    logged instead of mutating. This lets the acceptance procedure run
    --dry-run first and then a live run over the same input files without
    the dry run polluting dedup state (e.g. spurious md5 rows that would
    make the live run see everything as a duplicate).
    """
    pdf_path = Path(pdf_path)
    md5 = file_md5(pdf_path)
    ident = identify(pdf_path, conn, ocr_fallback, dry_run=dry_run)
    verdict, original = check_duplicate(conn, md5, ident)

    if verdict == 'duplicate':
        dest = _unique_dest(dirs['duplicados'], pdf_path.stem)
        if dry_run:
            logger.info('[DRY RUN] would move %s -> %s (duplicate of file_id=%s)',
                        pdf_path, dest, original['id'])
            return {'action': 'DUPLICATE', 'new_name': dest.name, 'file_id': None}
        shutil.move(str(pdf_path), str(dest))
        if original['md5'] == md5:
            # exact-bytes duplicate: files.md5 is UNIQUE, so there is no
            # new row to insert -- just move the file and point at the original
            return {'action': 'DUPLICATE', 'new_name': dest.name,
                    'file_id': original['id']}
        # QR-identity duplicate: different bytes -> distinct md5, safe to insert
        fid = state.insert_file(conn, md5=md5, original_name=pdf_path.name,
                                current_path=str(dest), nif=ident.nif,
                                atcud=ident.atcud, doc_ref=ident.doc_ref,
                                id_source=ident.id_source, status='queued')
        state.mark_duplicate(conn, fid, original['id'])
        return {'action': 'DUPLICATE', 'new_name': dest.name, 'file_id': fid}

    # supersede: move the bad original out of the way first
    if verdict == 'supersede':
        if dry_run:
            logger.info('[DRY RUN] would supersede original file_id=%s', original['id'])
        else:
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

    if verdict == 'supersede':
        action = 'SUPERSEDE'

    if dry_run:
        logger.info('[DRY RUN] would move %s -> %s (%s)', pdf_path, dest, action)
        return {'action': action, 'new_name': dest.name, 'file_id': None}

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
    return {'action': action, 'new_name': dest.name, 'file_id': fid}


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
