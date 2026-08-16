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
