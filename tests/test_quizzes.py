"""Synthetic quiz fixtures only; no live CourseLink calls or personal data."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from courselink_mcp.browser import EndpointUnavailable, LoginRequired
from courselink_mcp.config import Config
from courselink_mcp.quizzes import parse_summary
from courselink_mcp.server import API, create_app, register_tools
from courselink_mcp.service import Service


def fixture(name='open'):
    return (Path(__file__).parent / 'fixtures' / f'quiz_summary_{name}.html').read_text()


@pytest.fixture
def service(tmp_path):
    service = Service(Config(state=tmp_path, school=tmp_path, courses={'123': 'example-course'}))
    service.browser.status = {'state': 'authenticated'}
    service.browser.versions = {'le': '1.99'}
    service.browser.paged = AsyncMock(return_value=[{'QuizId': 7, 'Name': 'Practice Quiz06', 'GradeItemId': 8}])
    service.browser.get = AsyncMock(return_value=fixture())
    service.get_grades = AsyncMock(return_value={'courses': [{'items': [
        {'id': '8', 'points_earned': 9, 'points_max': 10, 'percent': 90,
         'flags': {'hidden_or_unreleased': False}, 'display': '90%', 'weight_achieved': None}],
        'checked_at': '2040-10-27T12:00:00Z', 'freshness': {'age_seconds': 5},
        'coverage': {'complete': True, 'errors': []}, 'source': 'api'}]})
    yield service
    service.store.close()


@pytest.mark.parametrize('kind,status,completed,remaining,state', [
    ('open', 'in_progress_or_partial', 1, 1, 'open'),
    ('closed_zero', 'missed', 0, 2, 'closed'),
    ('closed_two', 'done', 2, 0, 'closed'),
])
def test_summary_fields(kind, status, completed, remaining, state):
    row = parse_summary(fixture(kind), 'Practice Quiz06')
    assert row['coverage'] == {'complete': True, 'errors': []}
    assert row['status'] == status and row['state'] == state
    assert row['attempts_completed'] == completed and row['attempts_remaining'] == remaining
    assert row['attempts_allowed'] == 2 and row['time_limit_minutes'] == 20
    assert row['attempted'] is (completed > 0)
    assert row['due_date'] == '2040-10-31T03:59:00Z'
    assert row['due_date_display'] == 'Oct 30, 2040 11:59 PM'


@pytest.mark.parametrize('mutation,status,allowed,remaining', [
    ('unlimited', 'in_progress_or_partial', None, None),
    ('zero', 'not_started', 2, 2), ('all', 'completed_all_attempts', 2, 0),
    ('ids', 'in_progress_or_partial', 2, 1),
])
def test_summary_fallback_and_other_counts(mutation, status, allowed, remaining):
    html = fixture()
    if mutation == 'unlimited':
        html = html.replace('Allowed - 2', 'Allowed - Unlimited')
    elif mutation == 'ids':
        html = html.replace('id="z_', 'id="changed_')
    else:
        html = html.replace('Completed - 1', 'Completed - ' + ('0' if mutation == 'zero' else '2'))
    row = parse_summary(html, 'Practice Quiz06')
    assert row['status'] == status and row['attempts_allowed'] == allowed
    assert row['attempts_remaining'] == remaining


def test_unknown_counts_and_script_text_are_not_evidence():
    html = fixture().replace('Allowed - 2, Completed - 1', 'Unknown')
    html += '<script>Allowed - 2, Completed - 9</script>'
    row = parse_summary(html, 'Practice Quiz06')
    assert row['attempts_completed'] is None and row['status'] == 'unknown'
    assert not row['coverage']['complete']
    with pytest.raises(ValueError):
        parse_summary(fixture(), 'Different Quiz')


async def test_cache_single_flight_and_score_join(service):
    results = await asyncio.gather(*(API(service).call('quizzes', {'course_id': 'example-course'}) for _ in range(5)))
    row = results[0]['quizzes'][0]
    assert row['score'] == {'points_earned': 9, 'points_max': 10, 'percent': 90}
    assert row['score_status'] == 'released' and row['grade_item_id'] == '8'
    assert row['freshness']['refresh_interval_seconds'] == 30
    assert row['score_freshness']['age_seconds'] == 5
    assert row['checked_at'] and results[-1]['source'] == 'cache'
    assert results[0]['coverage']['complete']
    service.browser.get.assert_awaited_once_with('/d2l/lms/quizzing/user/quiz_summary.d2l?qi=7&ou=123', response_type='html')
    service.browser.paged.assert_awaited_once_with(service.browser.api('le', '123/quizzes/'))
    service.get_grades.assert_awaited_once_with('123')
    row['score']['percent'] = 0
    assert (await service.quizzes.list('123'))['quizzes'][0]['score']['percent'] == 90


@pytest.mark.parametrize('hidden', [True, False])
async def test_unreleased_score_is_never_zero(service, hidden):
    grade = service.get_grades.return_value['courses'][0]['items'][0]
    grade['flags']['hidden_or_unreleased'] = hidden
    if not hidden:
        grade.update(points_earned=None, percent=None, display=None)
    row = (await service.quizzes.list('123'))['quizzes'][0]
    assert row['score_status'] == 'unreleased' and row['score'] is None
    assert all(row[k] is None for k in ('points_earned', 'points_max', 'percent'))


@pytest.mark.parametrize('mode', ['403', '404', 'redirect', 'login_html'])
async def test_summary_unavailable_never_claims_not_attempted(service, mode):
    if mode in ('403', '404'):
        service.browser.get.side_effect = EndpointUnavailable('SYNTHETIC SECRET', int(mode))
    elif mode == 'redirect':
        service.browser.get.side_effect = LoginRequired('SYNTHETIC SECRET')
    else:
        service.browser.get.return_value = fixture('login')
    result = await service.quizzes.status('123', 'quiz06')
    row = result['matches'][0]
    assert row['attempts_completed'] is None and row['attempted'] is None
    assert row['status'] == 'unknown' and row['is_open'] is None
    assert not row['coverage']['complete'] and not result['coverage']['complete']
    assert result['coverage']['errors'] and not service.quizzes.cache
    assert 'SYNTHETIC SECRET' not in json.dumps(result)
    assert all('/attempts/' not in call.args[0] for call in service.browser.get.call_args_list)


async def test_catalog_forbidden_returns_coverage(service):
    service.browser.paged.side_effect = EndpointUnavailable('SYNTHETIC SECRET', 403)
    result = await service.quizzes.list('123')
    assert not result['coverage']['complete'] and result['quizzes'] == []
    service.browser.get.assert_not_awaited()
    assert 'SYNTHETIC SECRET' not in json.dumps(result)


async def test_filter_quiz_id_and_detail_grade_item(service):
    service.browser.paged.return_value = [{'QuizId': 7, 'Name': 'Practice Quiz06'}, {'QuizId': 9, 'Name': 'Other'}]
    async def get(path, **kwargs):
        if path.endswith('/quizzes/7'):
            assert not kwargs
            return {'GradeItemId': 8}
        assert 'qi=7&ou=123' in path and kwargs == {'response_type': 'html'}
        return fixture()
    service.browser.get.side_effect = get
    result = await service.quizzes.list('123', '7')
    assert len(result['quizzes']) == 1 and result['quizzes'][0]['grade_item_id'] == '8'
    assert result['coverage']['complete']
    result = await service.quizzes.list('123', '99')
    assert not result['coverage']['complete'] and not result['quizzes']


async def test_name_search_ambiguity_and_validation(service):
    service.browser.paged.return_value *= 2
    result = await service.quizzes.status('123', 'qUIz06')
    assert len(result['matches']) == 2
    assert (await service.quizzes.status('123', 'absent'))['matches'] == []
    for args in [('123', ''), ('123', None)]:
        with pytest.raises(ValueError):
            await service.quizzes.status(*args)
    for course, quiz in [('../bad', None), ('999', None), ('123', '../bad')]:
        with pytest.raises(ValueError):
            await service.quizzes.list(course, quiz)


async def test_score_join_requires_id_and_reports_grade_coverage(service):
    book = service.get_grades.return_value['courses'][0]
    book['items'][0]['id'] = None
    book['source'] = 'html'
    result = await service.quizzes.list('123')
    assert result['quizzes'][0]['score_status'] == 'unknown'
    assert not result['coverage']['complete']
    book['items'][0]['id'] = '8'
    book['coverage']['complete'] = False
    result = await service.quizzes.list('123')
    assert result['quizzes'][0]['score']['points_earned'] == 9
    assert not result['coverage']['complete']


async def test_expired_cache_refreshes_and_offline_is_unknown(service):
    await service.quizzes.list('123')
    key = ('123', None)
    service.quizzes.cache[key] = (0, service.quizzes.cache[key][1])
    service.browser.status['state'] = 'login_required'
    result = await service.quizzes.list('123')
    assert not result['coverage']['complete'] and not result['quizzes']
    service.browser.status['state'] = 'authenticated'
    await service.quizzes.list('123')
    assert service.browser.paged.await_count == 2


async def test_server_tool_schemas_dispatch_and_rest(service):
    mcp = FastMCP('test')
    register_tools(mcp, API(service).call)
    tools = {t.name: t for t in await mcp.list_tools()}
    assert len(tools) == 21
    for name in ('list_quizzes', 'quiz_status'):
        assert tools[name].annotations.readOnlyHint and not tools[name].annotations.destructiveHint
        assert 'deadline_digest' in tools[name].description
        assert 'get_grades' in tools[name].description and 'list_submissions' in tools[name].description
    assert tools['list_quizzes'].inputSchema['required'] == ['course_id']
    assert tools['list_quizzes'].inputSchema['properties']['quiz_id']['default'] is None
    assert tools['quiz_status'].inputSchema['required'] == ['course_id', 'query']
    content = await mcp.call_tool('quiz_status', {'course_id': 'example-course', 'query': 'Quiz06'})
    result = json.loads(content[0].text)
    assert result['matches'][0]['attempts_completed'] == 1
    content = await mcp.call_tool('list_quizzes', {'course_id': 'example-course', 'quiz_id': '7'})
    result = json.loads(content[0].text)
    assert result['quizzes'][0]['quiz_id'] == '7'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service.config, service)), base_url='http://test') as client:
        assert (await client.get('/api/quizzes?course_id=example-course')).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + service.config.token()
        assert (await client.get('/api/quizzes?course_id=example-course&quiz_id=7')).json()['quizzes']
        assert (await client.post('/api/quiz_status', json={'course_id': 'example-course', 'query': 'Quiz06'})).json()['matches']


def test_unknown_allowance_preserves_completed_count():
    row = parse_summary(fixture().replace('Allowed - 2', 'Allowed - Unspecified'), 'Practice Quiz06')
    assert row['attempts_completed'] == 1 and row['attempted'] is True
    assert row['attempts_allowed'] is None and row['attempts_remaining'] is None
    assert row['status'] == 'unknown' and not row['coverage']['complete']


@pytest.mark.parametrize('grade_item', [None, 8])
async def test_no_released_grade_item_is_null(service, grade_item):
    service.browser.paged.return_value[0]['GradeItemId'] = grade_item
    service.get_grades.return_value['courses'][0]['items'] = []
    row = (await service.quizzes.list('123'))['quizzes'][0]
    assert row['score_status'] == 'unreleased' and row['score'] is None
    assert row['coverage']['complete']


def test_availability_fallback_uses_end_not_due_date():
    # Fixed historical/future dates keep this independent of the test-run clock.
    html = fixture('closed_two').replace('<label id="z_d"><strong>Quiz Period Over</strong></label>', '')
    html = html.replace('Oct 26, 2040 12:01 AM', 'Jan 1, 2000 12:01 AM').replace('Oct 30, 2040 11:59 PM </span>', 'Jan 2, 2000 11:59 PM </span>')
    row = parse_summary(html, 'Practice Quiz06')
    assert row['state'] == 'closed' and row['status'] == 'done'
    html = html.replace('Jan 2, 2000 11:59 PM', 'Jan 2, 2099 11:59 PM')
    row = parse_summary(html, 'Practice Quiz06')
    assert row['state'] == 'open' and row['status'] == 'completed_all_attempts'
    assert row['coverage']['complete']
