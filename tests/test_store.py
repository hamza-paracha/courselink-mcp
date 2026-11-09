from pathlib import Path

from courselink_mcp.store import Store, safe_name


def content(title='Lab 1', **kw):
    return {'id': '1:content:2', 'course_id': '1', 'kind': 'content', 'title': title, **kw}


def test_change_cursor_reappearance_and_no_duplicates(tmp_path):
    store = Store(tmp_path / 'db')
    store.reconcile('1', 'content', [content()])
    store.reconcile('1', 'content', [content()])
    assert len(store.changes()['changes']) == 1
    store.reconcile('1', 'content', [content(due_date='2040-10-03')])
    assert store.changes(after=1)['changes'][0]['details']['changed_fields'] == ['due_date']
    store.reconcile('1', 'content', [])
    assert not store.get('1:content:2')['available']
    store.reconcile('1', 'content', [content()])
    assert store.get('1:content:2')['available']
    assert [c['event'] for c in store.changes()['changes']] == [
        'discovered', 'updated', 'no_longer_visible', 'available_again']
    assert store.changes(after=999)['next_cursor'] == 999


def test_versions_preserve_replacements_and_reverts(tmp_path):
    store = Store(tmp_path / 'db')
    row = content()
    store.reconcile('1', 'content', [row])
    for index, digest in enumerate(['aaa', 'aaa', 'bbb', 'aaa']):
        temp = tmp_path / f'tmp{index}'
        temp.write_text(digest)
        _, changed = store.record_file(row, temp, tmp_path / f'version{index}', digest, 3)
        assert changed == (index != 1)
    versions = store.versions(row['id'])
    assert [v['sha256'] for v in versions] == ['aaa', 'bbb', 'aaa']
    assert all(Path(v['path']).exists() for v in versions)
    store.file_checked(row)
    assert not store.needs_download(row, 3600)
    store.reconcile('1', 'content', [content(title='Changed')])
    assert store.needs_download(row, 3600)


def test_search_pagination_and_filename_safety(tmp_path):
    store = Store(tmp_path / 'db')
    rows = [dict(content(), id=f'1:content:{i}', title=f'Lab {i}') for i in range(4)]
    store.reconcile('1', 'content', rows)
    result = store.items(query='lab', limit=2, offset=2)
    assert result['total'] == 4
    assert len(result['items']) == 2
    assert store.items(query="' OR 1=1--")['total'] == 0
    assert '/' not in safe_name('../../../../file.pdf')


def test_link_updates_preserve_other_links_until_full_reconciliation(tmp_path):
    store = Store(tmp_path / 'db')
    first = dict(content(), kind='linked_file', id='1:linked:first')
    second = dict(first, id='1:linked:second')
    store.upsert_link(first)
    store.upsert_link(second)
    store.upsert_link(dict(first, title='Updated'))
    assert store.get(second['id'])['available']
    assert len(store.changes()['changes']) == 3
    store.reconcile('1', 'linked_file', [first])
    assert not store.get(second['id'])['available']
    store.close()


def test_listing_preserves_item_details_without_per_item_queries(tmp_path):
    store = Store(tmp_path / 'db')
    rows = [dict(content(), id=f'1:content:{i}', title=f'Lab {i:03d}', category='lab',
                 instructions={'Html': '<p>Read me</p>'}) for i in range(150)]
    store.reconcile('1', 'content', rows)
    expected = [store.get(row['id']) for row in rows[20:120]]
    queries = []
    store.db.set_trace_callback(queries.append)
    result = store.items(course_id='1', kind='content', category='lab', limit=100, offset=20)
    assert result == {'total': 150, 'items': expected, 'offset': 20}
    assert len([query for query in queries if query.startswith('SELECT')]) == 2
    store.close()


def test_latest_version_query_is_bounded(tmp_path):
    store = Store(tmp_path / 'db')
    row = content()
    store.reconcile('1', 'content', [row])
    for index in range(20):
        file = tmp_path / f'input{index}'
        file.write_text(str(index))
        store.record_file(row, file, tmp_path / f'saved{index}', str(index), len(str(index)))
    queries = []
    store.db.set_trace_callback(queries.append)
    assert store.latest_version(row['id'])['sha256'] == '19'
    assert 'LIMIT 1' in queries[-1]
    store.file_checked(row)
    assert not store.needs_download(row, 3600)
    assert any('LIMIT 1' in q for q in queries)
    store.close()


def test_legacy_versions_migrate_without_losing_files(tmp_path):
    import sqlite3
    db = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('''CREATE TABLE versions(id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,sha256 TEXT NOT NULL,path TEXT NOT NULL,
            size INTEGER NOT NULL,downloaded_at TEXT NOT NULL)''')
        connection.execute("INSERT INTO versions VALUES(1,'1:content:1','digest','saved.pdf',4,'2026-11-02')")
    for _ in range(2):  # Migration is idempotent across restarts.
        store = Store(db)
        row = store.latest_version('1:content:1')
        assert row['path'] == 'saved.pdf' and row['sha256'] == 'digest'
        assert row['etag'] is None and row['last_modified'] is None
        store.close()


def test_search_indexes_track_updates_replacements_and_rebuild(tmp_path):
    store = Store(tmp_path / 'search.sqlite3')
    row = {'id': '1:content:1', 'course_id': '1', 'kind': 'content', 'title': 'Lab instructions'}
    store.reconcile('1', 'content', [row])
    source = tmp_path / 'temporary'
    source.write_text('example')
    version, _ = store.record_file(row, source, tmp_path / 'saved', 'digest', 7)
    fts_enabled = store.fts
    text = 'Lab instruction: "quoted" phrase, multiple\nlines and café.'
    store.save_document(version['id'], {'text': text})
    for query in ('instruction', 'Lab', 'ab', '"quoted"', 'multiple\nlines', 'café', 'missing', 'on: "'):
        indexed = store.search_documents(query)['matches']
        store.fts = False
        fallback = store.search_documents(query)['matches']
        store.fts = fts_enabled
        assert indexed == fallback
    store.save_document(version['id'], {'text': 'Replacement instructions'})
    assert not store.search_documents('quoted')['matches']
    assert store.search_documents('Replacement')['matches']
    store.reconcile('1', 'content', [{**row, 'title': 'New title'}])
    assert not store.items(query='Lab')['items']
    assert store.items(query='New title')['items']
    if store.fts:
        store.db.execute('DROP TABLE documents_fts')
        store.db.execute('DROP TABLE items_fts')
    store.close()
    store = Store(tmp_path / 'search.sqlite3')
    assert store.search_documents('Replacement')['matches']
    assert store.items(query='New title')['items']
    store.close()
