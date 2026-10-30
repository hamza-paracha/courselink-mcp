"""All courses, people, files and responses in this module are synthetic."""
import asyncio
import copy
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from courselink_mcp.browser import BrowserSession, LoginRequired
from courselink_mcp.config import Config
from courselink_mcp.server import API, create_app, register_tools
from courselink_mcp.service import Service
from courselink_mcp.submissions import lateness


@pytest.fixture(autouse=True)
async def executor_lifecycle():
    # Restricted offline sandboxes can drop asyncio's cross-thread socket wakeups.
    # Keep timers active through executor shutdown without mocking file validation.
    async def pulse():
        while True:
            await asyncio.sleep(0.01)

    task = asyncio.create_task(pulse())
    try:
        yield
    finally:
        try:
            await asyncio.get_running_loop().shutdown_default_executor()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class Browser:
    def __init__(self):
        self.versions = {'le': '1.99'}
        self.status = {'state': 'authenticated'}
        self.calls = []
        self.folders = [{'Id': 10, 'Name': 'Example assignment', 'DueDate': '2099-01-01T00:00:00Z',
                         'AllowResubmission': True, 'Availability': {}}]
        self.rows = [True]
        self.upload_dropbox = AsyncMock(return_value={'outcome': 'submitted'})
        self.fail = False

    def api(self, product, suffix):
        return f'/d2l/api/{product}/1.99/{suffix}'

    async def paged(self, path):
        self.calls.append(path)
        if path.endswith('/folders/'):
            return copy.deepcopy(self.folders)
        raise AssertionError(path)

    async def get(self, path, *, response_type):
        self.calls.append(path)
        assert path == '/d2l/lms/dropbox/user/folders_history.d2l?db=10&grpid=0&ou=123'
        assert response_type == 'html'
        if self.fail:
            raise RuntimeError('Sensitive upstream text must not escape')
        fixture = 'one_submission' if self.rows else 'empty'
        return (Path(__file__).parent / 'fixtures' / f'dropbox_history_{fixture}.html').read_text().replace('41001_0', '10_0')



@pytest.fixture
def service(tmp_path):
    school = tmp_path / 'school'
    school.mkdir()
    service = Service(Config(state=tmp_path, school=school, courses={'123': 'example'}))
    service.browser = Browser()
    yield service
    service.store.close()


@pytest.fixture
def files(service):
    path = service.config.school / 'answer.txt'
    path.write_text('Synthetic assignment')
    return [str(path)]


async def test_history_cache_status_and_unknown_coverage(service):
    result = await API(service).call('submissions', {'course_id': 'example'})
    row = result['folders'][0]['submissions'][0]
    assert row['files'][0]['name'] == 'test_answer.zip'
    assert row['timeliness'] == 'on_time' and row['read_by_user'] is True
    assert row['submitted_at_utc'] == '2040-10-21T22:53:00Z'
    result['folders'].clear()  # Callers cannot mutate cache contents.
    results = await asyncio.gather(*(service.submissions.status('123', 'TEST_ANSWER.ZIP') for _ in range(5)))
    assert all(r['submitted'] for r in results) and len(service.browser.calls) == 2
    assert (await service.submissions.status('123', 'missing'))['submitted'] is False
    service.submissions.cache.clear()
    service.browser.fail = True
    result = await service.submissions.status('123', 'missing')
    assert result['submitted'] is None and not result['coverage']['complete']
    assert 'Sensitive' not in json.dumps(result)
    assert not service.submissions.cache


@pytest.mark.parametrize('submitted,due,expected', [
    ('2026-01-01T12:00:00Z', '2026-01-01T07:00:00-05:00', False),
    ('2026-01-01T12:00:01Z', '2026-01-01T12:00:00Z', True),
    (None, '2026-01-01T12:00:00Z', None), ('bad', 'bad', None),
    ('2026-01-01T12:00:00', '2026-01-01T12:00:00Z', None),
])
def test_lateness(submitted, due, expected):
    assert lateness(submitted, due) is expected


async def test_preview_confirm_single_use_and_audit(service, files):
    preview = await service.submissions.submit('123', '10', files)
    assert preview['dry_run'] and len(preview['files'][0]['sha256']) == 64
    service.browser.upload_dropbox.assert_not_awaited()
    result = await service.submissions.submit('123', '10', files, True, preview['preview_token'])
    assert result['outcome'] == 'submitted' and not result['dry_run']
    args = service.browser.upload_dropbox.call_args.args
    assert args[:2] == ('123', '10') and args[2][0]['buffer'] == b'Synthetic assignment'
    with pytest.raises(ValueError, match='preview token'):
        await service.submissions.submit('123', '10', files, True, preview['preview_token'])
    audit = service.config.state / 'submission-audit.jsonl'
    rows = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [r['event'] for r in rows] == ['attempt', 'preview', 'attempt', 'upload_started', 'upload_result', 'attempt', 'failed']
    assert audit.stat().st_mode & 0o777 == 0o600
    assert preview['preview_token'] not in audit.read_text()
    assert str(service.config.school) not in audit.read_text()
    assert not service.submissions.cache


@pytest.mark.parametrize('mutation', ['bytes', 'due', 'history', 'expired', 'folder'])
async def test_confirmation_binds_files_folder_history_and_expiry(service, files, mutation):
    preview = await service.submissions.submit('123', '10', files)
    token = preview['preview_token']
    if mutation == 'bytes':
        from pathlib import Path
        Path(files[0]).write_text('Different assignment')
    elif mutation == 'due':
        service.browser.folders[0]['DueDate'] = '2098-01-01T00:00:00Z'
    elif mutation == 'history':
        service.browser.rows.clear()
    elif mutation == 'expired':
        service.submissions.previews[token] = (time.monotonic() - 1, 'old')
    else:
        service.browser.folders[0]['Name'] = 'Changed folder'
    with pytest.raises(ValueError):
        await service.submissions.submit('123', '10', files, True, token)
    service.browser.upload_dropbox.assert_not_awaited()


async def test_late_requires_both_confirmation_and_opt_in(service, files):
    service.browser.folders[0]['DueDate'] = '2000-01-01T00:00:00Z'
    preview = await service.submissions.submit('123', '10', files, allow_late=True)
    assert preview['would_be_late']
    with pytest.raises(ValueError, match='allow_late'):
        await service.submissions.submit('123', '10', files, True, preview['preview_token'])
    preview = await service.submissions.submit('123', '10', files)
    assert (await service.submissions.submit('123', '10', files, True, preview['preview_token'], True))['outcome'] == 'submitted'


@pytest.mark.parametrize('policy', [False, None])
async def test_resubmission_unknown_or_denied(service, files, policy):
    service.browser.folders[0]['AllowResubmission'] = policy
    preview = await service.submissions.submit('123', '10', files)
    assert preview['allows_resubmission'] is policy
    with pytest.raises(ValueError, match='resubmi'):
        await service.submissions.submit('123', '10', files, True, preview['preview_token'])
    service.browser.upload_dropbox.assert_not_awaited()


async def test_unknown_resubmission_needs_explicit_override_and_denied_never_overridden(service, files):
    service.browser.folders[0]['AllowResubmission'] = False
    preview = await service.submissions.submit('123', '10', files)
    with pytest.raises(ValueError, match='not allowed'):
        await service.submissions.submit('123', '10', files, True, preview['preview_token'], allow_resubmit=True)
    service.browser.upload_dropbox.assert_not_awaited()
    service.browser.folders[0]['AllowResubmission'] = None
    preview = await service.submissions.submit('123', '10', files)
    with pytest.raises(ValueError, match='allow_resubmit=true'):
        await service.submissions.submit('123', '10', files, True, preview['preview_token'])
    preview = await service.submissions.submit('123', '10', files)
    result = await service.submissions.submit('123', '10', files, True, preview['preview_token'], allow_resubmit=True)
    assert result['outcome'] == 'submitted'
    service.browser.upload_dropbox.assert_awaited_once()


@pytest.mark.parametrize('kind', ['empty', 'oversized', 'executable', 'extension', 'symlink', 'directory', 'outside', 'duplicate', 'fifo'])
async def test_odd_files_are_rejected_and_audited(service, files, kind):
    from pathlib import Path
    path = Path(files[0])
    if kind == 'empty':
        path.write_bytes(b'')
    elif kind == 'oversized':
        service.config.max_file_bytes = 1
    elif kind == 'executable':
        path.chmod(0o700)
    elif kind == 'extension':
        path = path.with_suffix('.exe')
        path.write_bytes(b'bad')
    elif kind == 'symlink':
        link = path.with_name('link.txt')
        link.symlink_to(path)
        path = link
    elif kind == 'directory':
        path.unlink()
        path.mkdir()
    elif kind == 'outside':
        path = service.config.state / 'outside.txt'
        path.write_text('Private')
    elif kind == 'fifo':
        path.unlink()
        os.mkfifo(path)
    paths = [str(path)] * (2 if kind == 'duplicate' else 1)
    with pytest.raises(ValueError):
        await service.submissions.submit('123', '10', paths)
    service.browser.upload_dropbox.assert_not_awaited()
    audit = [json.loads(line) for line in (service.config.state / 'submission-audit.jsonl').read_text().splitlines()]
    assert [entry['event'] for entry in audit] == ['attempt', 'failed']
    assert audit[-1]['error_type'] == 'ValueError'
    assert audit[0]['attempt'] == audit[-1]['attempt']


async def test_missing_files_closed_folder_and_invalid_input(service, files):
    with pytest.raises(ValueError, match='File not found on the service host'):
        await service.submissions.submit('123', '10', [str(service.config.school / 'missing.txt')])
    with pytest.raises(ValueError, match='not under the school directory'):
        await service.submissions.submit('123', '10', ['/workspace/elsewhere/Lab3.zip'])
    with pytest.raises(ValueError, match='must be absolute'):
        await service.submissions.submit('123', '10', ['relative/Lab3.zip'])
    with pytest.raises(ValueError):
        await service.submissions.submit('123', None, files)
    with pytest.raises(ValueError):
        await service.submissions.submit('123', '10', files, confirm='false')
    with pytest.raises(ValueError):
        await service.submissions.list('../123')
    service.browser.folders[0]['Availability'] = {'EndDate': '2000-01-01T00:00:00Z'}
    preview = await service.submissions.submit('123', '10', files)
    with pytest.raises(ValueError, match='not currently open'):
        await service.submissions.submit('123', '10', files, True, preview['preview_token'])
    service.browser.status['state'] = 'login_required'
    with pytest.raises(LoginRequired):
        await service.submissions.list('123')
    service.browser.upload_dropbox.assert_not_awaited()


async def test_concurrent_confirmation_uploads_once(service, files):
    preview = await service.submissions.submit('123', '10', files)
    results = await asyncio.gather(*(service.submissions.submit('123', '10', files, True,
        preview['preview_token']) for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(r, ValueError) for r in results) == 1
    service.browser.upload_dropbox.assert_awaited_once()


async def test_audit_failure_prevents_upload(service, files, monkeypatch):
    preview = await service.submissions.submit('123', '10', files)
    def fail(*a, **kw):
        raise OSError('Disk full')
    monkeypatch.setattr(service.submissions, 'audit', fail)
    with pytest.raises(OSError):
        await service.submissions.submit('123', '10', files, True, preview['preview_token'])
    service.browser.upload_dropbox.assert_not_awaited()


@pytest.mark.parametrize('entrypoint', ['submission', 'browser'])
async def test_all_monitor_workers_and_children_refuse_uploads(service, files, monkeypatch, entrypoint):
    preview = await service.submissions.submit('123', '10', files)
    browser = BrowserSession(service.config)
    checked = []

    async def attempt():
        with pytest.raises(ValueError, match='Background monitor'):
            if entrypoint == 'submission':
                await service.submissions.submit('123', '10', files, True, preview['preview_token'])
            else:
                await browser.upload_dropbox('123', '10', [], preflight=lambda: None)

    def worker(name):
        async def run():
            await attempt()
            await asyncio.create_task(attempt())  # Child tasks inherit the prohibition.
            checked.append(name)
        return run

    for name in ('session_loop', 'scan_loop', 'index_loop'):
        monkeypatch.setattr(service, name, worker(name))
    await service.start()
    await asyncio.gather(*service.tasks)
    assert set(checked) == {'session_loop', 'scan_loop', 'index_loop'}
    service.browser.upload_dropbox.assert_not_awaited()
    # The explicit request task retains its privileges and the unused token.
    result = await service.submissions.submit('123', '10', files, True, preview['preview_token'])
    assert result['outcome'] == 'submitted'


async def test_tools_and_rest_auth_dispatch(service, files):
    mcp = FastMCP('test')
    register_tools(mcp, API(service).call)
    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools['list_submissions'].annotations.readOnlyHint
    assert tools['submission_status'].annotations.readOnlyHint
    assert tools['submit_to_dropbox'].annotations.destructiveHint
    schema = tools['submit_to_dropbox'].inputSchema
    assert schema['required'] == ['course_id', 'folder_id', 'files']
    assert schema['properties']['confirm']['default'] is False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service.config, service)), base_url='http://test') as client:
        assert (await client.post('/api/submit_to_dropbox', json={})).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + service.config.token()
        assert (await client.get('/api/submit_to_dropbox')).status_code == 405
        result = await client.post('/api/submit_to_dropbox', json={'course_id': 'example', 'folder_id': '10', 'files': files})
        assert result.status_code == 200 and result.json()['dry_run']
        assert (await client.get('/api/submissions?course_id=example')).json()['folders']


@pytest.mark.parametrize('mode', ['success', 'redirect', 'controls', 'unknown'])
async def test_browser_upload_uses_existing_context_and_never_retries(service, mode):
    browser = BrowserSession(service.config)
    browser.closed = False
    browser.status = {'state': 'authenticated'}
    control = SimpleNamespace(count=AsyncMock(return_value=0 if mode == 'controls' else 1),
                              is_enabled=AsyncMock(return_value=True), set_input_files=AsyncMock(), click=AsyncMock())
    receipt = SimpleNamespace(wait_for=AsyncMock(side_effect=TimeoutError() if mode == 'unknown' else None))
    page = SimpleNamespace(url='', locator=lambda _: control, get_by_role=lambda *a, **kw: control,
                           get_by_text=lambda *a, **kw: receipt, close=AsyncMock())
    async def goto(url, **kw):
        page.url = 'https://example.invalid/login' if mode == 'redirect' else url
    page.goto = goto
    browser.context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    payload = [{'name': 'answer.txt', 'mimeType': 'text/plain', 'buffer': b'Example'}]
    if mode in ('redirect', 'controls'):
        with pytest.raises(ValueError):
            await browser.upload_dropbox('123', '10', payload, preflight=lambda: None)
        control.set_input_files.assert_not_awaited()
    else:
        result = await browser.upload_dropbox('123', '10', payload, preflight=lambda: None)
        assert result['outcome'] == ('submitted' if mode == 'success' else 'unknown')
        control.set_input_files.assert_awaited_once_with(payload)
        control.click.assert_awaited_once()
    page.close.assert_awaited_once()


def history_fixture(kind='two_submissions'):
    return (Path(__file__).parent / 'fixtures' / f'dropbox_history_{kind}.html').read_text()


@pytest.mark.parametrize('evidence', ['both', 'marker', 'table'])
def test_real_empty_history(evidence):
    from courselink_mcp.submissions import parse_history
    html = history_fixture('empty_state')
    if evidence == 'marker':
        html = html.replace('d_gempty', '')
    elif evidence == 'table':
        html = html.replace('No files have been submitted to this dropbox folder', 'Unknown')
    assert parse_history(html, '41001', None) == ([], {'complete': True, 'errors': []})


def test_real_empty_history_mismatched_folder():
    from courselink_mcp.submissions import parse_history
    with pytest.raises(ValueError, match='selected folder does not match'):
        parse_history(history_fixture('empty_state'), '41002', None)


@pytest.mark.parametrize('mutation', ['no_evidence', 'wrong_summary', 'row'])
def test_real_empty_history_requires_recognized_evidence(mutation):
    from courselink_mcp.submissions import parse_history
    html = history_fixture('empty_state')
    if mutation == 'row':
        html = html.replace('no-column-border=""></table>',
                            'no-column-border=""><tr><td>Unknown</td></tr></table>')
    else:
        html = html.replace('No files have been submitted to this dropbox folder', 'Unknown')
        if mutation == 'no_evidence':
            html = html.replace('d_gempty', '')
        else:
            html = html.replace('List of file submissions for the selected dropbox folders', 'Unknown')
    with pytest.raises(ValueError, match='headers are unrecognized'):
        parse_history(html, '41001', None)


def test_history_fixture_fields_and_deadline():
    from courselink_mcp.submissions import parse_history
    rows, coverage = parse_history(history_fixture(), '41001', '2040-10-21T22:53:00Z')
    assert coverage == {'complete': True, 'errors': []}
    assert [r['submission_id'] for r in rows] == ['1000001', '1000002']
    deleted, live = rows
    assert deleted['files'] == [{'name': 'test_answer.tar.gz', 'size_display': None,
                                'size_bytes': None, 'deleted': True, 'url': None}]
    assert deleted['read_by_user'] is False
    assert deleted['submitted_at_utc'] == '2040-10-18T16:22:00Z'
    assert live['submitted_at_display'] == 'Oct 21, 2040 6:53 PM'
    assert live['submitted_at_utc'] == '2040-10-21T22:53:00Z'
    assert live['on_time'] is True and live['late'] is False
    assert live['files'][0]['size_bytes'] == round(3.99 * 1024**2)
    assert live['files'][0]['size_display'] == '3.99 MB'
    assert live['files'][0]['url'].startswith('/d2l/common/viewFile.d2lfile/')
    assert live['files'][0]['deleted'] is False
    assert parse_history(history_fixture(), '41001', '2040-10-20T00:00:00Z')[0][-1]['late'] is True
    assert parse_history(history_fixture(), '41001', None)[0][-1]['on_time'] is None


@pytest.mark.parametrize('size,expected', [('512 KB', 512 * 1024), ('1.2 GB', round(1.2 * 1024**3))])
def test_multiple_files_and_sizes(size, expected):
    from courselink_mcp.submissions import parse_history
    html = history_fixture('one_submission').replace('</a><span>&nbsp;(3.99 MB)</span></span>',
        '</a><span>&nbsp;(3.99 MB)</span></span>'
        f'<span class="dfl"><a href="/d2l/file/second"><span>second.zip</span></a><span> ({size})</span></span>')
    rows, coverage = parse_history(html, '41001', None)
    assert coverage['complete'] and len(rows[0]['files']) == 2
    assert rows[0]['files'][1]['size_bytes'] == expected
    assert not rows[0]['files'][1]['deleted']


@pytest.mark.parametrize('controls,complete', [
    ('<button title="Next Page">Next</button>', False),
    ('<a rel="next" href="#">More</a>', False),
    ('<button disabled title="Next Page">Next</button>', True),
    ('<select aria-label="Page size"><option selected value="1">1</option></select>', False),
    ('<select aria-label="Page size"><option selected value="20">20</option></select>', True),
    ('<select aria-label="Page"><option selected>1</option><option>2</option></select>', False),
])
def test_history_pagination(controls, complete):
    from courselink_mcp.submissions import parse_history
    rows, coverage = parse_history(history_fixture('one_submission') + controls, '41001', None)
    assert len(rows) == 1
    assert coverage['complete'] is complete
    assert bool(coverage['errors']) is not complete


@pytest.mark.parametrize('mutation', ['mismatch', 'table', 'headers', 'login', 'row'])
async def test_unrecognized_history_never_claims_empty_or_allows_upload(service, files, mutation):
    html = history_fixture('empty').replace('41001_0', '10_0')
    if mutation == 'mismatch':
        html = html.replace('10_0', '999_0')
    elif mutation == 'table':
        html = html.replace('id="z_e"', 'id="other"')
    elif mutation == 'headers':
        html = html.replace('Date Submitted', 'Unknown')
    elif mutation == 'row':
        html = html.replace('</table></d2l-table-wrapper>', '<tr><td>Unknown row</td></tr></table></d2l-table-wrapper>')
    else:
        html = '<html>Sign in</html>'
    service.browser.get = AsyncMock(return_value=html)
    result = await service.submissions.status('123', 'Example')
    assert result['submitted'] is None and not result['coverage']['complete']
    assert not service.submissions.cache
    with pytest.raises(ValueError, match='Cannot verify existing submissions'):
        await service.submissions.submit('123', '10', files)
    service.browser.upload_dropbox.assert_not_awaited()


async def test_empty_history_status_and_latest(service):
    service.browser.rows.clear()
    result = await service.submissions.status('123', 'Example')
    assert result['submitted'] is False and result['latest_submission'] is None
    assert result['coverage']['complete']
    service.submissions.cache.clear()
    service.browser.get = AsyncMock(return_value=history_fixture().replace('41001_0', '10_0'))
    result = await service.submissions.status('123', 'Example')
    assert result['submitted'] is True
    assert result['latest_submission']['submission_id'] == '1000002'
    assert result['files'][0]['name'] == 'test_answer.zip'
    assert 'America/Toronto' in result['note']


async def test_history_workers_are_bounded_and_overlap(service):
    from urllib.parse import parse_qs, urlsplit
    service.config.api_concurrency = 2
    service.browser.folders = [{**service.browser.folders[0], 'Id': i} for i in range(10, 16)]
    active = peak = 0
    async def get(path, *, response_type):
        nonlocal active, peak
        assert response_type == 'html'
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(0.01)
            fid = parse_qs(urlsplit(path).query)['db'][0]
            return history_fixture('empty').replace('41001_0', fid + '_0')
        finally:
            active -= 1
    service.browser.get = get
    result = await service.submissions.list('123')
    assert result['coverage']['complete'] and len(result['folders']) == 6
    assert peak == 2


def test_history_winter_timezone():
    from courselink_mcp.submissions import history_datetime
    assert history_datetime('Jan 21, 2026 6:53 PM') == '2026-01-21T23:53:00Z'
    assert history_datetime('invalid') is None


async def test_paginated_history_is_partial_and_blocks_upload(service, files):
    service.browser.get = AsyncMock(return_value=history_fixture('one_submission').replace(
        '41001_0', '10_0') + '<button title="Next Page">Next</button>')
    result = await service.submissions.status('123', 'Example')
    assert result['submitted'] is True and result['files']
    assert not result['coverage']['complete']
    assert not service.submissions.cache
    assert (await service.submissions.status('123', 'not present'))['submitted'] is None
    with pytest.raises(ValueError, match='Cannot verify existing submissions'):
        await service.submissions.submit('123', '10', files)
    service.browser.upload_dropbox.assert_not_awaited()


def test_filename_is_not_a_pagination_control():
    from courselink_mcp.submissions import parse_history
    html = history_fixture('one_submission').replace('test_answer.zip', 'next_page.zip')
    assert parse_history(html, '41001', None)[1]['complete']
