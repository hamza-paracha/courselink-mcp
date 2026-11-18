"""All courses, people, files and responses in this module are synthetic."""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from courselink_mcp.browser import BrowserSession
from courselink_mcp.config import Config
from courselink_mcp.server import API, create_app, register_tools
from courselink_mcp.service import Service
from courselink_mcp.submissions import lateness


class Browser:
    def __init__(self):
        self.versions = {'le': '1.99'}
        self.status = {'state': 'authenticated'}
        self.calls = []
        self.folders = [{'Id': 10, 'Name': 'Example assignment', 'DueDate': '2099-01-01T00:00:00Z',
                         'AllowResubmission': True, 'Availability': {}}]
        self.rows = [True]
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


async def test_history_cache_status_and_unknown_coverage(service):
    result = await API(service).call('submissions', {'course_id': 'example'})
    row = result['folders'][0]['submissions'][0]
    assert row['files'][0]['name'] == 'test_answer.zip'
    assert row['timeliness'] == 'on_time' and row['read_by_user'] is True
    assert row['submitted_at_utc'] == '2040-10-21T22:53:00Z'
    result['folders'].clear()  # Callers cannot mutate cache contents.
    results = await asyncio.gather(*(service.submissions.list('123', query='TEST_ANSWER.ZIP') for _ in range(5)))
    assert all(r['submitted'] for r in results) and len(service.browser.calls) == 2
    assert (await service.submissions.list('123', query='missing'))['submitted'] is False
    service.submissions.cache.clear()
    service.browser.fail = True
    result = await service.submissions.list('123', query='missing')
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


async def test_tools_and_rest_auth_dispatch(service):
    mcp = FastMCP('test')
    register_tools(mcp, API(service).call)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert tools['list_submissions'].annotations.readOnlyHint
    assert 'submit_to_dropbox' not in tools and 'submission_status' not in tools
    assert not hasattr(BrowserSession, 'upload_dropbox') and not hasattr(service.submissions, 'submit')
    body = {'course_id': 'example', 'folder_id': '10', 'files': ['/synthetic/answer.txt']}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service.config, service)),
                                 base_url='http://test') as client:
        assert (await client.post('/api/submit_to_dropbox', json=body)).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + service.config.token()
        denied = await client.post('/api/submit_to_dropbox', json=body)
        assert denied.status_code == 400 and denied.json()['error'] == 'Unknown operation.'
        assert not service.browser.calls
        assert (await client.get('/api/submissions?course_id=example')).json()['folders']
        match = await client.get('/api/submissions', params={'course_id': 'example', 'query': 'test_answer'})
        assert match.json()['submitted'] is True
        legacy = await client.get('/api/submission_status', params={'course_id': 'example', 'query': 'test_answer'})
        assert legacy.json()['latest_submission'] == match.json()['latest_submission']


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
async def test_unrecognized_history_never_claims_empty(service, mutation):
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
    result = await service.submissions.list('123', query='Example')
    assert result['submitted'] is None and not result['coverage']['complete']
    assert not service.submissions.cache


async def test_empty_history_status_and_latest(service):
    service.browser.rows.clear()
    result = await service.submissions.list('123', query='Example')
    assert result['submitted'] is False and result['latest_submission'] is None
    assert result['coverage']['complete']
    service.submissions.cache.clear()
    service.browser.get = AsyncMock(return_value=history_fixture().replace('41001_0', '10_0'))
    result = await service.submissions.list('123', query='Example')
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


async def test_paginated_history_is_partial(service):
    service.browser.get = AsyncMock(return_value=history_fixture('one_submission').replace(
        '41001_0', '10_0') + '<button title="Next Page">Next</button>')
    result = await service.submissions.list('123', query='Example')
    assert result['submitted'] is True and result['files']
    assert not result['coverage']['complete']
    assert not service.submissions.cache
    assert (await service.submissions.list('123', query='not present'))['submitted'] is None


def test_filename_is_not_a_pagination_control():
    from courselink_mcp.submissions import parse_history
    html = history_fixture('one_submission').replace('test_answer.zip', 'next_page.zip')
    assert parse_history(html, '41001', None)[1]['complete']
