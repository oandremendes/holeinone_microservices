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


def reset_for_retry(conn, file_id, *, current_path, status='queued', nif=None,
                    atcud=None, doc_ref=None, supplier_key=None, doc_date=None,
                    doc_type=None, id_source='none'):
    """Reuse an existing row for an exact-bytes rescan (files.md5 is UNIQUE,
    so no new row can be inserted): repoint the path, refresh identification,
    reset the queue status and clear extraction attempts/last_error."""
    conn.execute("UPDATE files SET current_path=?, status=?, nif=?, atcud=?,"
                 " doc_ref=?, supplier_key=?, doc_date=?, doc_type=?,"
                 " id_source=? WHERE id=?",
                 (str(current_path), status, nif, atcud, doc_ref, supplier_key,
                  doc_date, doc_type, id_source, file_id))
    conn.execute("UPDATE extractions SET attempts=0, last_error=NULL"
                 " WHERE file_id=?", (file_id,))
    conn.commit()


def delete_qrs(conn, file_id):
    conn.execute("DELETE FROM qr_codes WHERE file_id=?", (file_id,))
    conn.commit()


def pending(conn, cap=5):
    return conn.execute(
        "SELECT f.*, COALESCE(e.attempts, 0) AS attempts,"
        " e.result_json AS result_json, e.model AS model FROM files f"
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
