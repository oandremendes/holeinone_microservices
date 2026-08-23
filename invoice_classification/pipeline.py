"""v2 pipeline: Phase A (identify/dedup/enqueue) and Phase B (drain queue)."""
import hashlib
import json
import logging
import re
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
    try:
        hits = qr_decode(pdf_path)
    except Exception as e:
        # corrupt/unreadable PDF: fall through to the OCR fallback (which
        # itself returns 'unknown' on unreadable input) so the file lands in
        # REVIEW with a files row instead of erroring on every run
        logger.warning('QR decode failed for %s: %s', pdf_path, e)
        hits = []
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
    if verdict == 'supersede' and original['md5'] == md5:
        # exact same bytes rescanned over a bad original: files.md5 is UNIQUE,
        # so there is no new row to insert -- reuse the original row (its old
        # file is already in Duplicados) and let it re-enter the queue
        fid = original['id']
        state.reset_for_retry(conn, fid, current_path=str(dest), status=status,
                              nif=ident.nif, atcud=ident.atcud,
                              doc_ref=ident.doc_ref,
                              supplier_key=ident.supplier_key or None,
                              doc_date=ident.doc_date, doc_type=ident.doc_type,
                              id_source=ident.id_source)
        stored = [r['raw_payload'] for r in conn.execute(
            "SELECT raw_payload FROM qr_codes WHERE file_id=? ORDER BY id",
            (fid,)).fetchall()]
        if [h.raw for h in ident.qr_hits] != stored:
            state.delete_qrs(conn, fid)
            for i, hit in enumerate(ident.qr_hits):
                state.add_qr(conn, fid, hit.page, hit.raw, hit.fields,
                             is_primary=(i == 0))
        return {'action': action, 'new_name': dest.name, 'file_id': fid}

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


def dirs_for_source(source_dir):
    """Build the v2 pipeline dirs layout for a given ScanSnap source folder."""
    source_dir = Path(source_dir)
    if source_dir.name == 'ScanSnap':
        scansnap_root = source_dir
        duplicados = source_dir.parent / 'Duplicados'
    else:
        # e.g. .../ScanSnap/Receipts -> ScanSnap root is the parent
        scansnap_root = source_dir.parent
        duplicados = source_dir / 'Duplicados'
    return {
        'integrated': source_dir / 'INTEGRATED',
        'review': source_dir / 'REVIEW',
        'duplicados': duplicados,
        'extracted': scansnap_root / 'EXTRACTED',
        'approved': scansnap_root / 'APPROVED',
    }


def _in_month_dir(path):
    """True when path is already filed under an INTEGRATED YYYY/MM subdir."""
    p = Path(path)
    return bool(re.fullmatch(r'\d{2}', p.parent.name)
                and re.fullmatch(r'\d{4}', p.parent.parent.name))


def _dirs_for_row(current_path):
    """Derive the folder layout for one queued row from its own path, so a
    drain started from any folder files each row into its own source root
    (state.pending is global). None when the layout can't be derived."""
    p = Path(current_path)
    integrated = p.parent.parent.parent if _in_month_dir(p) else p.parent
    if integrated.name != 'INTEGRATED':
        return None
    return dirs_for_source(integrated.parent)


def _primary_qr(conn, file_id):
    row = conn.execute("SELECT parsed_json FROM qr_codes WHERE file_id=? AND"
                       " is_primary=1", (file_id,)).fetchone()
    return json.loads(row['parsed_json']) if row else None


def _default_extractor(pdf_path, supplier):
    import api_config
    import claude_extract
    cfg = api_config.get_anthropic()
    return claude_extract.extract(pdf_path, supplier, api_key=cfg['api_key'])


# Campos de cabeçalho que uma aprovação humana pode corrigir; alargar aqui
# à medida que aparecerem casos novos (linhas ficam para uma versão futura).
_OVERRIDABLE_FIELDS = {'invoice_ref', 'total_cents', 'iva_cents', 'base_cents',
                       'total_document_cents', 'total_vasilhame_cents',
                       'due_date', 'customer_nif'}


def _process_approvals(conn, dirs, odoo_cfg, poster, resolver, stats):
    """Segunda aprovação vinda do QA_Faturas: consome marcadores
    ScanSnap/APPROVED/<md5>.json escritos quando um humano marca OK uma
    fatura retida. O gate é dispensado (a aprovação humana É a segunda
    validação): a extração já guardada segue para o Odoo com as correções
    do marcador (data/fornecedor), o PDF é arquivado em YYYY/MM e o
    artefacto atualizado. Falha transitória mantém o marcador para o
    próximo tick; marcadores órfãos são removidos com aviso."""
    import json as _json

    import artifacts
    import claude_extract
    import odoo_send
    import validation as _validation  # noqa: F401 (parity of imports)

    approved_dir = (dirs or {}).get('approved')
    if not approved_dir or not Path(approved_dir).is_dir():
        return
    for marker in sorted(Path(approved_dir).glob('*.json')):
        try:
            mk = _json.loads(marker.read_text())
        except (OSError, ValueError):
            logger.warning('marcador de aprovação ilegível: %s', marker)
            continue
        row = state.find_by_md5(conn, mk.get('md5') or '')
        if mk.get('action') == 'nao_fatura':
            _retire_nao_fatura(conn, dirs, row, mk, marker, stats)
            continue
        ext_row = row and conn.execute(
            "SELECT * FROM extractions WHERE file_id=?", (row['id'],)).fetchone()
        if (row is None or row['status'] not in ('needs_review', 'failed')
                or not ext_row or not ext_row['result_json']):
            logger.warning('marcador de aprovação sem fatura retida '
                           'correspondente (md5=%s); removido', mk.get('md5'))
            marker.unlink(missing_ok=True)
            continue
        row_dirs = _dirs_for_row(row['current_path']) or dirs
        result = claude_extract.Extraction.model_validate_json(ext_row['result_json'])
        extraction = result.model_dump()
        served_model = ext_row['model'] or claude_extract.MODEL
        if mk.get('date'):
            extraction['date'] = mk['date']
        # Correções decididas pelo humano na aprovação. Contrato aberto:
        # chaves desconhecidas (ex.: futuras correções de linhas) são
        # ignoradas com aviso, para versões antigas do serviço não
        # rebentarem com marcadores novos.
        applied, unsupported = [], []
        for k, v in (mk.get('overrides') or {}).items():
            if k in _OVERRIDABLE_FIELDS:
                applied.append(f'{k} {extraction.get(k)}→{v}')
                extraction[k] = v
            else:
                unsupported.append(k)
        supplier_key = (mk.get('supplier') or row['supplier_key'] or '').lower()             or row['supplier_key']
        # o QA pode ter renomeado o ficheiro no Drive
        scansnap_root = Path(row_dirs['extracted']).parent
        if mk.get('pdf') and (scansnap_root / mk['pdf']).exists():
            new_path = scansnap_root / mk['pdf']
            if str(new_path) != row['current_path']:
                state.update_path(conn, row['id'], new_path)
                row = state.find_by_md5(conn, mk['md5'])
        verdict = {'status': 'ok', 'checks': {}, 'notes': []}
        old_vj = ext_row['validation_json']
        if old_vj:
            verdict = _json.loads(old_vj)
            verdict['status'] = 'ok'
        verdict.setdefault('notes', []).append(
            f"aprovada manualmente (QA) em {mk.get('approved_at') or ''}".strip())
        if applied:
            verdict['notes'].append('valores corrigidos na aprovação: '
                                    + ', '.join(applied))
        if unsupported:
            logger.warning('aprovação %s: overrides não suportados ignorados: %s',
                           mk.get('md5'), unsupported)
            verdict['notes'].append('overrides não suportados nesta versão '
                                    '(ignorados): ' + ', '.join(unsupported))
        try:
            doc_date = mk.get('date') or row['doc_date'] or extraction.get('date')
            if _in_month_dir(row['current_path']):
                new_path = Path(row['current_path'])
            else:
                new_path = file_into_month(row['current_path'],
                                           row_dirs['integrated'], doc_date)
                state.update_path(conn, row['id'], new_path)
                row = state.find_by_md5(conn, mk['md5'])
            basename = new_path.stem
            marker_rel = str(new_path)
            m = '/ScanSnap/'
            rel = marker_rel.split(m, 1)[1] if m in marker_rel else new_path.name
            drive_id = resolver(f"{odoo_cfg['drive_remote']}/{rel}")
            url = odoo_send.drive_preview_url(drive_id) if drive_id else None
            payload_row = dict(row)
            payload_row['supplier_key'] = supplier_key
            payload = odoo_send.build_payload(payload_row, extraction,
                                              document_url=url)
            odoo_result = odoo_send.send(payload, odoo_cfg['webhook_url'],
                                         odoo_cfg['api_key'], poster=poster)
        except Exception as e:
            logger.warning('aprovação %s: envio falhou (%s); marcador mantido '
                           'para o próximo tick', mk.get('md5'), e)
            continue
        state.save_extraction(conn, row['id'], served_model,
                              ext_row['result_json'], _json.dumps(verdict))
        state.mark_sent(conn, row['id'], _json.dumps(odoo_result))
        sent_row = conn.execute("SELECT odoo_sent_at FROM extractions WHERE"
                                " file_id=?", (row['id'],)).fetchone()
        art = artifacts.build_artifact(dict(row), _primary_qr(conn, row['id']),
                                       extraction, verdict,
                                       {'sent_at': sent_row['odoo_sent_at'],
                                        'status': 'sent'},
                                       served_model, ext_row['attempts'])
        artifacts.write_json(row_dirs['extracted'], basename, art)
        marker.unlink(missing_ok=True)
        stats['approved'] += 1


def _retire_nao_fatura(conn, dirs, row, mk, marker, stats):
    """QA julgou o documento "não é fatura": retira a row do fluxo (estado
    terminal `nao_fatura`, nunca enviada ao Odoo) e move o PDF para fora,
    por omissão para ScanSnap/QA/Nao Fatura/. Rows já enviadas nunca são
    tocadas — o marcador órfão é removido com aviso."""
    retirable = ('needs_review', 'failed', 'review_folder', 'queued', 'retry')
    if row is None or row['status'] not in retirable:
        logger.warning('marcador nao_fatura sem row retirável (md5=%s, '
                       'status=%s); removido', mk.get('md5'),
                       row['status'] if row else None)
        marker.unlink(missing_ok=True)
        return
    row_dirs = _dirs_for_row(row['current_path']) or dirs
    scansnap_root = Path(row_dirs['extracted']).parent
    target_rel = mk.get('target') or f"QA/Nao Fatura/{Path(row['current_path']).name}"
    target_dir = scansnap_root / Path(target_rel).parent
    target_dir.mkdir(parents=True, exist_ok=True)
    src = Path(row['current_path'])
    if src.exists():
        dest = scansnap_root / target_rel
        if dest.exists():
            dest = _unique_dest(target_dir, dest.stem)
        shutil.move(str(src), str(dest))
    else:
        # o QA (ou um humano) pode já ter movido o ficheiro
        dest = scansnap_root / target_rel
    conn.execute("UPDATE files SET status='nao_fatura', current_path=?"
                 " WHERE id=?", (str(dest), row['id']))
    conn.commit()
    marker.unlink(missing_ok=True)
    stats['retired'] += 1


def drain(conn, dirs, odoo_cfg, extractor=None, poster=None, resolver=None,
          cap=5, dry_run=False):
    import claude_extract
    import artifacts
    import odoo_send
    import validation

    extractor = extractor or _default_extractor
    resolver = resolver or odoo_send.resolve_drive_id
    stats = {'extracted': 0, 'sent': 0, 'needs_review': 0, 'retried': 0,
             'failed': 0, 'approved': 0, 'retired': 0}

    if not dry_run:
        _process_approvals(conn, dirs, odoo_cfg, poster, resolver, stats)

    for row in state.pending(conn, cap=cap):
        fid = row['id']
        attempts = row['attempts'] + 1
        # dirs are derived per row from its own path: state.pending is global,
        # so a drain started from one folder may pick up rows queued under
        # another source root (the caller's dirs are only a fallback)
        row_dirs = _dirs_for_row(row['current_path']) or dirs
        if row_dirs is None:
            logger.warning("cannot derive folders for %s; skipping",
                           row['current_path'])
            continue
        if dry_run:
            logger.info(f"[DRY RUN] would extract {row['current_path']}")
            continue
        if row['result_json']:
            # odoo/filing retry: the extraction already succeeded on a
            # previous attempt -- reuse it instead of re-running Claude
            result = claude_extract.Extraction.model_validate_json(row['result_json'])
            raw_json = row['result_json']
            served_model = row['model'] or claude_extract.MODEL
        else:
            try:
                result, raw_json, served_model = extractor(
                    row['current_path'], row['supplier_key'])
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
        state.save_extraction(conn, fid, served_model, raw_json,
                              json.dumps(verdict))
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        basename = Path(row['current_path']).stem

        if verdict['status'] != 'ok':
            state.set_status(conn, fid, 'needs_review')
            # Sem QR o documento fica retido para segunda aprovação manual;
            # se a extração leu a data, prefixa-a no nome (2026XXXX_ ->
            # YYYYMMDD_) e guarda doc_date, para a fila de QA ficar
            # organizada por data. Retenções COM QR nunca são renomeadas.
            ext_date = extraction.get('date')
            if (qr_fields is None and not row['doc_date'] and ext_date
                    and re.fullmatch(r'\d{4}-\d{2}-\d{2}', ext_date)
                    and re.match(r'\d{4}XXXX_', basename)):
                old_path = Path(row['current_path'])
                stem = ext_date.replace('-', '') + basename[8:]
                dest = _unique_dest(old_path.parent, stem)
                shutil.move(str(old_path), str(dest))
                conn.execute("UPDATE files SET current_path=?, doc_date=?"
                             " WHERE id=?", (str(dest), ext_date, fid))
                conn.commit()
                row = conn.execute("SELECT * FROM files WHERE id=?",
                                   (fid,)).fetchone()
                basename = dest.stem
            stats['needs_review'] += 1
            art = artifacts.build_artifact(dict(row), qr_fields, extraction, verdict,
                                           {'sent_at': None, 'status': 'held'},
                                           served_model, attempts)
            artifacts.write_json(row_dirs['extracted'], basename, art)
            continue

        # validated: file into YYYY/MM before resolving the Drive link
        try:
            doc_date = row['doc_date'] or extraction.get('date')
            if _in_month_dir(row['current_path']):
                # already filed by a previous attempt -- no _N rename cascade
                new_path = Path(row['current_path'])
            else:
                new_path = file_into_month(row['current_path'],
                                           row_dirs['integrated'], doc_date)
                state.update_path(conn, fid, new_path)
                row = conn.execute("SELECT * FROM files WHERE id=?",
                                   (fid,)).fetchone()
            # the filed name may carry a _N suffix from _unique_dest, so the
            # artifact basename must follow the POST-filing path
            basename = new_path.stem
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
            new_status = conn.execute("SELECT status FROM files WHERE id=?",
                                      (fid,)).fetchone()[0]
            stats['retried' if new_status == 'retry' else 'failed'] += 1
            art = artifacts.build_artifact(dict(row), qr_fields, extraction, verdict,
                                           {'sent_at': None, 'status': None},
                                           served_model, attempts)
            artifacts.write_json(row_dirs['extracted'], basename, art)
            continue

        state.mark_sent(conn, fid, json.dumps(odoo_result))
        sent_row = conn.execute("SELECT odoo_sent_at FROM extractions WHERE"
                                " file_id=?", (fid,)).fetchone()
        art = artifacts.build_artifact(dict(row), qr_fields, extraction, verdict,
                                       {'sent_at': sent_row['odoo_sent_at'],
                                        'status': 'sent'},
                                       served_model, attempts)
        artifacts.write_json(row_dirs['extracted'], basename, art)
        stats['sent'] += 1
    return stats
