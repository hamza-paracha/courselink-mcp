import base64
import hashlib
from pathlib import Path

import pytest

from courselink_mcp.computer import Computer
from courselink_mcp.config import Config


@pytest.fixture
def setup(tmp_path):
    config = Config(state=tmp_path, school=tmp_path / 'School', courses={'1': 'example-course'})
    item = {'id': '1:content:2', 'course_id': '1', 'kind': 'content'}
    data = b'lab instructions'
    version = {'id': 7, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
               'path': '/srv/downloads/example-course/CourseLink Downloads/content/1_content_2/lab.pdf'}

    async def call(operation, args):
        assert operation == 'read_file'
        return {'base64': base64.b64encode(data).decode(), 'offset': 0,
                'next_offset': len(data), 'eof': True}
    return Computer(config, call), item, version


async def test_verified_local_download_reuses_bytes_and_preserves_edits(setup):
    computer, item, version = setup
    first = await computer.save(item, version)
    path = Path(first['path'])
    assert path.read_bytes() == b'lab instructions'
    assert path.is_relative_to(computer.config.school / 'example-course' / 'CourseLink Downloads')
    assert not (await computer.save(item, version))['saved']
    path.write_text('my own work')
    second = await computer.save(item, version)
    assert second['saved'] and second['path'] != first['path']
    assert path.read_text() == 'my own work'


async def test_bad_transfer_not_published_and_partial_removed(setup):
    computer, item, version = setup
    version['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='mismatch'):
        await computer.save(item, version)
    assert not list(computer.config.school.rglob('*.partial'))
    assert not list(computer.config.school.rglob('*.pdf'))


async def test_symlink_escape_rejected(setup, tmp_path):
    computer, item, version = setup
    school = computer.config.school
    school.mkdir()
    (school / 'example-course').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        await computer.save(item, version)


async def test_sync_paginates_all_content_and_saves_metadata(setup):
    computer, item, version = setup
    original_call = computer.call
    item['download_path'] = '/content/lab.pdf'
    page = {'id': '1:content:3', 'course_id': '1', 'kind': 'content', 'description': 'Page instructions'}
    offsets = []

    async def call(operation, args):
        if operation == 'status':
            return {'session': {'state': 'authenticated'}, 'scanning': False,
                    'last_scan': {'complete': True}}
        if operation == 'items':
            offsets.append(args['offset'])
            return {'items': [item if args['offset'] == 0 else page], 'total': 2}
        if operation == 'versions':
            return {'versions': [version]}
        return await original_call(operation, args)
    computer.call = call
    result = await computer.sync(refresh=False)
    assert result['complete'] and result['saved'] == 1
    assert offsets == [0, 1]
    assert 'Page instructions' in (computer.root('1') / 'catalog.json').read_text()


async def test_missing_cached_file_is_reported(setup):
    computer, item, version = setup
    item['download_path'] = '/content/lab.pdf'

    async def call(operation, args):
        if operation == 'status':
            return {'session': {'state': 'login_required'}, 'last_scan': {'complete': True}}
        if operation == 'items':
            return {'items': [item], 'total': 1}
        if operation == 'versions':
            return {'versions': []}
    computer.call = call
    result = await computer.sync(refresh=False)
    assert not result['complete'] and len(result['errors']) == 2


async def test_remote_bridge_requires_own_connection_settings(monkeypatch):
    from courselink_mcp.computer import connect
    for variable in ('COURSELINK_REMOTE_HOST', 'COURSELINK_REMOTE_STATE_DIR', 'COURSELINK_REMOTE_EXECUTABLE'):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ValueError, match='COURSELINK_REMOTE_HOST'):
        async with connect():
            pytest.fail('A connection without explicit server settings must not be opened.')


async def test_remote_bridge_quotes_paths_and_uses_configured_host(monkeypatch):
    from contextlib import asynccontextmanager
    import shlex
    import courselink_mcp.computer as module
    captured = []
    monkeypatch.setenv('COURSELINK_REMOTE_HOST', 'example-host')
    monkeypatch.setenv('COURSELINK_REMOTE_STATE_DIR', '/srv/private state')
    monkeypatch.setenv('COURSELINK_REMOTE_EXECUTABLE', '/srv/my clone/.venv/bin/courselink')

    @asynccontextmanager
    async def fake_stdio(parameters):
        captured.append(parameters)
        yield None, None

    class FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

    monkeypatch.setattr(module, 'stdio_client', fake_stdio)
    monkeypatch.setattr(module, 'ClientSession', FakeSession)
    async with module.connect():
        pass
    assert captured[0].args[-2] == 'example-host'
    assert shlex.split(captured[0].args[-1]) == [
        'env', 'COURSELINK_STATE_DIR=/srv/private state',
        '/srv/my clone/.venv/bin/courselink', 'stdio']


@pytest.mark.parametrize('scan_complete', [True, False])
async def test_fresh_sync_reuses_scan_downloads_and_reports_partial_coverage(setup, monkeypatch, scan_complete):
    computer, item, version = setup
    original_call = computer.call
    item['download_path'] = '/content/lab.pdf'
    calls = []
    scanned = False

    async def call(operation, args):
        nonlocal scanned
        calls.append(operation)
        if operation == 'status':
            return {'session': {'state': 'authenticated'}, 'scanning': False,
                    'last_scan': {'complete': scan_complete if scanned else True,
                                  'finished_at': 'new' if scanned else 'old'}}
        if operation == 'scan':
            scanned = True
            return {'queued': True}
        if operation == 'items':
            return {'items': [item], 'total': 1}
        if operation == 'versions':
            return {'versions': [version]}
        return await original_call(operation, args)

    async def no_wait(seconds):
        pass

    monkeypatch.setattr('courselink_mcp.computer.asyncio.sleep', no_wait)
    computer.call = call
    result = await computer.sync()
    assert result['complete'] is scan_complete
    assert result['saved'] == 1
    assert calls.count('scan') == 1
    assert 'download' not in calls
    assert len(result['errors']) == (0 if scan_complete else 1)
    # A subsequent sync also avoids transferring unchanged bytes to this computer.
    calls.clear()
    result = await computer.sync(refresh=False)
    assert result['unchanged'] == 1
    assert 'read_file' not in calls and 'download' not in calls
