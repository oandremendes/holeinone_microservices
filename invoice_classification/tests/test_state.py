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
