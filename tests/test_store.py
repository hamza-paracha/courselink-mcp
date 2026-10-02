from pathlib import Path

from courselink_mcp.store import Store, safe_name


def content(title='Lab 1', **kw):
    return {'id': '1:content:2', 'course_id': '1', 'kind': 'content', 'title': title, **kw}


def test_change_cursor_reappearance_and_no_duplicates(tmp_path):
    store = Store(tmp_path / 'db')
    store.reconcile('1', 'content', [content()])
    store.reconcile('1', 'content', [content()])
    assert len(store.changes()['changes']) == 1
    store.reconcile('1', 'content', [content(due_date='2026-10-03')])
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
