import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from courselink_mcp.browser import BrowserSession, EndpointUnavailable, LoginRequired, RateLimited
from courselink_mcp.config import Config
from courselink_mcp.grades import fetch_grades, normalize, parse_html
from courselink_mcp.server import API, create_app, register_tools
from courselink_mcp.service import Service

FIXTURES = Path(__file__).parent / 'fixtures' / 'grades'


class GradeBrowser:
    def __init__(self):
        self.data = json.loads((FIXTURES / 'weighted.json').read_text())
        self.failures = {}
        self.calls = []
        self.versions = {'le': '1.99'}
        self.status = {'state': 'authenticated'}

    def api(self, product, suffix):
        return f'/d2l/api/{product}/{self.versions[product]}/{suffix}'

    async def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        key = ('html' if 'main.d2l' in path else 'values' if 'myGradeValues/' in path
               else 'final' if 'myGradeValue' in path else 'categories' if 'categories/' in path else 'objects')
        if key in self.failures:
            raise self.failures[key]
        return (FIXTURES / 'learner.html').read_text() if key == 'html' else self.data[key]

    async def paged(self, path):
        return await self.get(path)


async def test_weighted_api_categories_and_released_final():
    browser = GradeBrowser()
    result = await fetch_grades(browser, '123')
    lab, ungraded, text, bonus, category, hidden = result['items']
    assert lab['category'] == 'Labs' and lab['percent'] == 80
    assert (lab['weight_achieved'], lab['weight_max'], lab['configured_weight']) == (4, 5, 50)
    assert lab['last_modified'] == '2040-10-20T12:00:00Z' and lab['feedback_present']
    assert ungraded['points_earned'] is None and ungraded['percent'] is None
    assert text['display'] == 'Complete' and text['flags']['excluded']
    assert bonus['flags']['bonus'] and bonus['flags']['dropped'] and bonus['percent'] is None
    assert category['is_category'] and hidden['flags']['hidden_or_unreleased']
    assert hidden['points_earned'] is None and hidden['points_max'] == 100
    assert result['categories'][0]['lowest_to_drop'] == 1
    assert result['final_calculated_grade']['letter'] == 'A-'
    assert result['coverage']['complete']
    assert 'Synthetic feedback' not in json.dumps(result) and 'private note' not in json.dumps(result)
    assert all(path.startswith('/d2l/api/le/1.99/123/grades/') for path, _ in browser.calls)


@pytest.mark.parametrize('status', [403, 404])
async def test_unavailable_values_fall_back_to_visible_html(status):
    browser = GradeBrowser()
    browser.failures['values'] = EndpointUnavailable(f'HTTP {status}', status)
    result = await fetch_grades(browser, '123')
    assert result['source'] == 'html' and result['coverage']['warnings']
    assert result['items'][0]['is_category']
    assert result['items'][1]['category'] == 'Labs' and result['items'][1]['feedback_present']
    assert result['items'][2]['flags']['dropped']
    assert result['items'][3]['flags']['bonus']
    assert result['items'][3]['category'] is None
    assert result['categories'][0]['weight'] == 20
    assert result['items'][4]['flags']['hidden_or_unreleased']
    assert not result['items'][4]['feedback_present']
    assert result['final_calculated_grade']['percent'] == 80
    assert browser.calls[-1] == ('/d2l/lms/grades/my_grades/main.d2l?ou=123', {'response_type': 'html'})
    assert 'Synthetic feedback' not in json.dumps(result)


@pytest.mark.parametrize('status', [403, 404])
async def test_missing_metadata_preserves_values_with_coverage_errors(status):
    browser = GradeBrowser()
    for section in ('objects', 'categories', 'final'):
        browser.failures[section] = EndpointUnavailable(f'HTTP {status}', status)
    result = await fetch_grades(browser, '123')
    assert result['items'][0]['points_earned'] == 8
    assert result['items'][0]['category'] is None
    assert result['final_calculated_grade'] is None
    assert not result['coverage']['complete']
    assert {e['section'] for e in result['coverage']['errors']} >= {'objects', 'categories'}


@pytest.mark.parametrize('final', [None, {}, {'ReleasedDate': None, 'PointsNumerator': 80, 'DisplayedGrade': '80 %'}])
async def test_empty_and_unreleased_grades(final):
    browser = GradeBrowser()
    browser.data = {'values': [], 'objects': [], 'categories': [], 'final': final}
    result = await fetch_grades(browser, '1')
    assert result['items'] == [] and result['final_calculated_grade'] is None
    assert result['coverage']['complete']


async def test_final_404_is_absent_not_fabricated_zero():
    browser = GradeBrowser()
    browser.failures['final'] = EndpointUnavailable('HTTP 404', 404)
    result = await fetch_grades(browser, '1')
    assert result['coverage']['complete'] and result['coverage']['warnings']
    assert result['final_calculated_grade'] is None


@pytest.mark.parametrize('error', [LoginRequired('expired'), RateLimited('slow down')])
async def test_session_and_throttling_do_not_try_html(error):
    browser = GradeBrowser()
    browser.failures['values'] = error
    with pytest.raises(type(error)):
        await fetch_grades(browser, '1')
    assert len(browser.calls) == 1


@pytest.mark.parametrize('html', ['<title>Sign in</title><input type="password">', '<title>Grades</title><p>Access denied</p>', '<title>Grades</title><table></table>'])
def test_unrecognized_html_fails_closed(html):
    with pytest.raises(ValueError, match='Unrecognized'):
        parse_html(html)


def test_explicit_empty_html_and_unreleased_final():
    assert parse_html('<title>Grades - Course</title><p>No grades have been released.</p>')['items'] == []
    html = (FIXTURES / 'learner.html').read_text().replace('<td>A-</td>', '<td>Not released</td>')
    assert parse_html(html)['final_calculated_grade'] is None


def test_unknown_flags_and_nulls_are_not_fabricated():
    row = normalize({'PointsNumerator': 0, 'PointsDenominator': 10})
    assert row['percent'] == 0 and row['flags']['dropped'] is None
    assert normalize({})['flags']['hidden_or_unreleased'] is True
    assert normalize({})['flags']['dropped'] is None
    assert normalize({'PointsNumerator': 'NaN', 'PointsDenominator': 0})['percent'] is None


@pytest.fixture
def service(tmp_path):
    service = Service(Config(state=tmp_path, school=tmp_path, courses={'123': 'sample-course', '456': 'other-course'}))
    service.browser = GradeBrowser()
    yield service
    service.store.close()


async def test_cache_expiry_offline_preservation_and_monitored_scope(service):
    first = await service.get_grades('123')
    count = len(service.browser.calls)
    assert await service.get_grades('123') == first
    assert len(service.browser.calls) == count
    cached = service.store.grades('123')
    cached['last_attempt_at'] = '2000-01-01T00:00:00+00:00'
    service.store.save_grades('123', cached)
    service.browser.status['state'] = 'login_required'
    stale = (await service.get_grades('123'))['courses'][0]
    assert stale['items'] == first['courses'][0]['items']
    assert stale['checked_at'] == first['courses'][0]['checked_at']
    assert stale['freshness']['refresh_due'] and not stale['coverage']['complete']
    assert len(service.browser.calls) == count
    with pytest.raises(ValueError, match='monitored'):
        await service.get_grades('999')
    assert len((await service.get_grades())['courses']) == 2


async def test_single_flight_cache_and_course_failure_isolation(service):
    await asyncio.gather(*(service.get_grades('123') for _ in range(5)))
    assert len(service.browser.calls) == 4
    first = service.store.grades('123')
    service.browser.failures['values'] = EndpointUnavailable('HTTP 403', 403)
    service.browser.failures['html'] = EndpointUnavailable('HTTP 403', 403)
    result = await service.get_grades(refresh=True)
    assert result['courses'][0]['items'] == first['items']
    assert result['courses'][0]['checked_at'] == first['checked_at']
    assert result['courses'][1]['checked_at'] is None
    assert result['courses'][1]['no_grades_released'] is None


async def test_grade_rest_auth_alias_and_refresh(service):
    config = service.config
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config, service)), base_url='http://test') as client:
        assert (await client.get('/api/grades')).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + config.token()
        assert (await client.get('/api/grades?course_id=sample-course')).status_code == 200
        count = len(service.browser.calls)
        assert (await client.get('/api/grades?course_id=sample-course&refresh=true')).status_code == 200
        assert len(service.browser.calls) == count + 4
        assert (await client.get('/api/grades?refresh=bad')).status_code == 400
        assert (await client.get('/api/grades?course_id=999')).status_code == 400
        assert (await client.post('/api/grades', json={'refresh': 'false'})).status_code == 400
        assert (await API(service).call('grades', {'course_id': 'sample-course'}))['courses'][0]['course_id'] == '123'


async def test_tool_schema_and_dispatch():
    mcp = FastMCP('test')
    calls = []
    async def call(operation, args):
        calls.append((operation, args))
        return {'courses': []}
    register_tools(mcp, call)
    tool = next(t for t in await mcp.list_tools() if t.name == 'get_grades')
    assert tool.inputSchema['properties']['course_id']['default'] is None
    assert not tool.inputSchema.get('required')
    assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
    await mcp.call_tool('get_grades', {'course_id': '123'})
    assert calls == [('grades', {'course_id': '123'})]


@pytest.mark.parametrize('status,content_type,body,error', [
    (200, 'text/html', '<title>Grades</title><p>Visible gradebook</p>', None),
    (200, 'text/html', '<input type="password">', LoginRequired),
    (302, 'text/html', '', LoginRequired),
    (403, 'text/html', '', EndpointUnavailable),
    (404, 'text/html', '', EndpointUnavailable),
])
async def test_browser_html_uses_get_and_disposes_responses(tmp_path, status, content_type, body, error):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    disposed, calls = [], []
    async def text():
        return body
    async def dispose():
        disposed.append(True)
    async def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status=status, headers={'content-type': content_type}, text=text, dispose=dispose)
    browser.context = SimpleNamespace(request=SimpleNamespace(get=get))
    browser.closed = False
    if error:
        with pytest.raises(error):
            await browser.get('/d2l/lms/grades/my_grades/main.d2l?ou=123', response_type='html')
    else:
        assert await browser.get('/d2l/lms/grades/my_grades/main.d2l?ou=123', response_type='html') == body
    assert disposed == [True] and len(calls) == 1
    assert calls[0][1]['max_redirects'] == 0 and calls[0][1]['headers']['Accept'] == 'text/html'


async def test_version_discovery_uses_existing_browser(service):
    service.browser.versions = {}
    async def discover():
        service.browser.versions = {'le': '1.98'}
    service.browser.discover_versions = discover
    await service.get_grades('123')
    assert service.browser.calls[0][0].startswith('/d2l/api/le/1.98/')


@pytest.mark.parametrize('as_json', [True, False])
def test_cli_reads_running_service_without_starting_or_logging_in(tmp_path, monkeypatch, capsys, as_json):
    from courselink_mcp import cli
    config = Config(state=tmp_path, school=tmp_path)
    monkeypatch.setattr(cli.Config, 'load', lambda: config)
    monkeypatch.setenv('COURSELINK_SERVER_TOKEN', 'synthetic-test-token')
    monkeypatch.setenv('COURSELINK_SERVER_URL', 'http://localhost:9999')
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'courses': [], 'coverage': {'complete': True}})
    real_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr('sys.argv', ['courselink', 'grades', '--course', '123', '--refresh'] + (['--json'] if as_json else []))
    cli.main()
    assert len(calls) == 1 and calls[0].method == 'GET'
    assert calls[0].url.path == '/api/grades'
    assert dict(calls[0].url.params) == {'course_id': '123', 'refresh': 'true'}
    if as_json:
        assert json.loads(capsys.readouterr().out)['coverage']['complete']


async def test_grade_cache_survives_store_reopen(service):
    from courselink_mcp.store import Store
    await service.get_grades('123')
    expected = service.store.grades('123')
    other = Store(service.config.state / 'catalog.sqlite3')
    try:
        assert other.grades('123') == expected
    finally:
        other.close()


async def test_partial_http_failure_keeps_usable_grade_values(service):
    service.browser.failures['categories'] = RuntimeError('HTTP 500')
    result = (await service.get_grades('123'))['courses'][0]
    assert result['checked_at'] and result['items'][0]['points_earned'] == 8
    assert result['coverage']['errors'][0]['section'] == 'categories'


async def test_empty_gradebook_and_independent_course_errors(service):
    browser_get = service.browser.get
    async def get(path, **kwargs):
        if '/456/' in path:
            raise RuntimeError('HTTP 500')
        return await browser_get(path, **kwargs)
    service.browser.get = get
    service.browser.data = {'values': [], 'objects': [], 'categories': [], 'final': None}
    result = await service.get_grades()
    assert result['courses'][0]['no_grades_released'] is True
    assert result['courses'][0]['coverage']['complete']
    assert not result['courses'][1]['coverage']['complete']
    assert result['courses'][1]['no_grades_released'] is None


async def test_cancelled_refresh_leaves_previous_cache_intact(service):
    await service.get_grades('123')
    previous = service.store.grades('123')
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError
    service.browser.get = cancel
    with pytest.raises(asyncio.CancelledError):
        await service.get_grades('123', refresh=True)
    assert service.store.grades('123') == previous


@pytest.mark.parametrize('display', ['', '-', 'Not released', 'Unreleased'])
async def test_final_placeholders_are_not_released(display):
    browser = GradeBrowser()
    browser.data['final'] = {'GradeObjectName': 'Final Calculated Grade', 'DisplayedGrade': display}
    result = await fetch_grades(browser, '1')
    assert result['final_calculated_grade'] is None


async def test_category_embedded_objects_and_zero_grade():
    browser = GradeBrowser()
    browser.data['objects'] = []
    browser.data['categories'][0]['Grades'] = [
        {'Id': 11, 'Name': 'Lab 1', 'MaxPoints': 10, 'IsBonus': False}]
    browser.data['values'][0]['PointsNumerator'] = 0
    browser.data['values'][0]['WeightedNumerator'] = 0
    browser.data['values'][0]['DisplayedGrade'] = '0 %'
    result = await fetch_grades(browser, '1')
    row = result['items'][0]
    assert row['category'] == 'Labs' and row['percent'] == 0
    assert row['points_earned'] == 0 and row['weight_achieved'] == 0


def test_unrelated_html_tables_are_not_grade_rows():
    html = (FIXTURES / 'learner.html').read_text() + '<table><tr><td>Unrelated</td><td>Secret text</td></tr></table>'
    result = parse_html(html)
    assert all(row['name'] != 'Unrelated' for row in result['items'])


async def test_adjusted_final_is_not_labeled_calculated():
    browser = GradeBrowser()
    browser.data['final']['GradeObjectType'] = 8
    browser.data['final']['GradeObjectName'] = 'Final Adjusted Grade'
    assert (await fetch_grades(browser, '1'))['final_calculated_grade'] is None


def test_value_flags_and_object_type_are_preserved():
    row = normalize({'GradeObjectType': 5, 'PointsNumerator': 12, 'PointsDenominator': 10,
                     'IsBonus': True, 'IsExcluded': False, 'IsDropped': False},
                    {'CanExceedMaxPoints': True, 'IsBonus': False})
    assert row['grade_object_type'] == 5
    assert row['percent'] == 120
    assert row['flags']['bonus'] is True
    assert row['flags']['can_exceed_max_points'] is True
    assert row['flags']['dropped'] is False and row['flags']['excluded'] is False


async def test_partial_refresh_preserves_previous_snapshot_and_timestamp(service):
    previous = (await service.get_grades('123'))['courses'][0]
    service.browser.data['values'][0]['PointsNumerator'] = 9
    service.browser.failures['categories'] = RuntimeError('HTTP 500')
    current = (await service.get_grades('123', refresh=True))['courses'][0]
    assert current['items'] == previous['items']
    assert current['categories'] == previous['categories']
    assert current['checked_at'] == previous['checked_at']
    assert not current['coverage']['complete'] and current['freshness']['refresh_due']


async def test_authentication_recovery_retries_without_waiting_for_cache_interval(service):
    service.browser.status['state'] = 'login_required'
    assert not (await service.get_grades('123'))['coverage']['complete']
    service.browser.status['state'] = 'authenticated'
    assert (await service.get_grades('123'))['coverage']['complete']
    assert service.browser.calls


async def test_missing_le_version_is_rediscovered(service):
    service.browser.versions = {'lp': '1.50'}
    async def discover():
        service.browser.versions['le'] = '1.98'
    service.browser.discover_versions = discover
    assert (await service.get_grades('123'))['coverage']['complete']


@pytest.mark.parametrize('section', ['values', 'objects', 'categories', 'final'])
async def test_errors_do_not_persist_or_return_response_bodies_secrets_or_feedback(service, caplog, section):
    secret = 'SYNTHETIC-secret-cookie-and-feedback'
    service.browser.failures[section] = RuntimeError(secret)
    result = await service.get_grades('123')
    assert not result['coverage']['complete']
    assert secret not in json.dumps(result)
    assert secret not in json.dumps(service.store.grades('123'))
    assert secret not in caplog.text


def test_html_reordered_columns_do_not_return_feedback_as_name():
    result = parse_html('<title>Grades</title><table><tr><th>Comments and Assessments</th>'
                        '<th>Grade Item</th><th>Grade</th></tr><tr><td>PRIVATE FEEDBACK</td>'
                        '<th scope="row">Quiz</th><td>80 %</td></tr></table>')
    assert result['items'][0]['name'] == 'Quiz'
    assert result['items'][0]['feedback_present']
    assert 'PRIVATE FEEDBACK' not in json.dumps(result)


def test_html_nested_feedback_table_does_not_corrupt_grade_rows():
    html = (FIXTURES / 'learner.html').read_text().replace(
        'Synthetic feedback must not be returned.',
        '<table><tr><td>PRIVATE FEEDBACK</td></tr></table>')
    result = parse_html(html)
    assert result['items'][1]['name'] == 'Lab 1'
    assert result['items'][1]['points_earned'] == 8
    assert 'PRIVATE FEEDBACK' not in json.dumps(result)


@pytest.mark.parametrize('row', ['<tr><td colspan="3">Temporarily unavailable</td></tr>',
                                '<tr><td>Quiz</td><td>80 %</td></tr>'])
def test_html_ambiguous_rows_fail_closed(row):
    with pytest.raises(ValueError, match='Unrecognized'):
        parse_html('<table><tr><th>Grade Item</th><th>Points</th><th>Grade</th></tr>' + row + '</table>')


def test_html_grade_name_is_not_a_hidden_status():
    row = parse_html('<table><tr><th>Grade Item</th><th>Grade</th></tr>'
                     '<tr><th>Hidden Markov Models</th><td>90 %</td></tr></table>')['items'][0]
    assert row['flags']['hidden_or_unreleased'] is not True


async def test_category_value_uses_own_category_metadata():
    browser = GradeBrowser()
    browser.data['categories'][0]['ExcludeFromFinalGrade'] = True
    row = (await fetch_grades(browser, '123'))['items'][4]
    assert row['is_category'] and row['configured_weight'] == 20
    assert row['flags']['excluded'] is True


async def test_final_endpoint_rejects_non_final_grade_type():
    browser = GradeBrowser()
    browser.data['final']['GradeObjectType'] = 4
    result = await fetch_grades(browser, '123')
    assert result['final_calculated_grade'] is None
    assert not result['coverage']['complete']


@pytest.mark.parametrize('tail', ['', '<script>No grades have been released</script>'])
def test_html_header_only_cannot_erase_a_snapshot(tail):
    with pytest.raises(ValueError, match='Unrecognized'):
        parse_html('<title>Grades</title><table><tr><th>Grade Item</th><th>Grade</th></tr></table>' + tail)


def test_html_hidden_rows_are_not_returned():
    html = (FIXTURES / 'learner.html').read_text().replace(
        '</tbody>', '<tr style="display: none"><th>PRIVATE UNRELEASED ITEM</th>'
        '<td>9 / 10</td><td>9 / 10</td><td>90 %</td><td></td></tr></tbody>')
    assert 'PRIVATE UNRELEASED ITEM' not in json.dumps(parse_html(html))


async def test_real_pagination_and_version_discovery_for_grades(tmp_path):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    data = GradeBrowser().data
    paths = []
    async def get(path, params=None, **kwargs):
        paths.append((path, dict(params or {})))
        if path == '/d2l/api/versions/':
            return [{'ProductCode': 'le', 'LatestVersion': '1.98'},
                    {'ProductCode': 'lp', 'LatestVersion': '1.50'}]
        if 'myGradeValues/' in path:
            if params.get('bookmark') == 'second':
                return {'Items': data['values'][2:], 'PagingInfo': {'HasMoreItems': False}}
            return {'Items': data['values'][:2], 'PagingInfo': {'HasMoreItems': True, 'Bookmark': 'second'}}
        if 'categories/' in path:
            return data['categories']
        if 'myGradeValue' in path:
            return data['final']
        if params.get('page') == 'second':
            return {'Objects': data['objects'][2:], 'Next': None}
        if 'page=second' in path:
            return {'Objects': data['objects'][2:], 'Next': None}
        return {'Objects': data['objects'][:2],
                'Next': '/d2l/api/le/1.98/123/grades/?page=second'}
    browser.get = get
    await browser.discover_versions()
    result = await fetch_grades(browser, '123')
    assert result['coverage']['complete'] and len(result['items']) == 6
    assert result['items'][3]['flags']['bonus']
    assert len(paths) == 7
    assert all('/le/1.98/123/grades/' in path for path, _ in paths[1:])


@pytest.mark.parametrize('grade_type', [1, 2, 3, 4, 5, 6, 9])
async def test_grade_types_remain_items_without_final_estimation(grade_type):
    browser = GradeBrowser()
    browser.data = {'objects': [], 'categories': [], 'final': None, 'values': [
        {'GradeObjectIdentifier': '1', 'GradeObjectType': grade_type,
         'GradeObjectName': 'Synthetic item', 'DisplayedGrade': 'Complete' if grade_type == 4 else '75 %'}]}
    result = await fetch_grades(browser, '123')
    assert result['coverage']['complete']
    assert result['items'][0]['grade_object_type'] == grade_type
    assert result['items'][0]['is_category'] == (grade_type == 9)
    assert result['final_calculated_grade'] is None


@pytest.mark.parametrize('body,login', [
    ('<title>Grades</title><a href="/d2l/login/logout.d2l">Log out</a>', False),
    ('<input TYPE = "password">', True),
    ('<form action="/d2l/lms/authenticate/authenticate.d2l"></form>', True),
])
async def test_html_login_detection_does_not_reject_logout_links(tmp_path, body, login):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    async def text():
        return body
    async def dispose():
        pass
    async def get(*args, **kwargs):
        return SimpleNamespace(status=200, headers={'content-type': 'text/html'}, text=text, dispose=dispose)
    browser.context = SimpleNamespace(request=SimpleNamespace(get=get))
    browser.closed = False
    if login:
        with pytest.raises(LoginRequired):
            await browser.get('/grades', response_type='html')
    else:
        assert await browser.get('/grades', response_type='html') == body


@pytest.mark.parametrize('release', [
    {'IsReleased': False}, {'IsHidden': True}, {'DisplayedGrade': 'Not released'},
])
async def test_unreleased_scores_are_not_returned_or_cached(service, release):
    service.browser.data['values'][0].update(release)
    row = (await service.get_grades('123'))['courses'][0]['items'][0]
    assert row['flags']['hidden_or_unreleased'] is True
    for key in ('points_earned', 'weight_achieved', 'percent', 'display', 'letter'):
        assert row[key] is None
        assert service.store.grades('123')['items'][0][key] is None
    assert row['points_max'] == 10 and row['weight_max'] == 5


def test_hidden_definition_suppresses_value():
    row = normalize({'PointsNumerator': 8, 'DisplayedGrade': 'A'}, {'IsHidden': True})
    assert row['points_earned'] is None and row['letter'] is None


@pytest.mark.parametrize('denominator', [None, ''])
def test_missing_denominator_uses_definition(denominator):
    row = normalize({'PointsNumerator': 8, 'PointsDenominator': denominator}, {'MaxPoints': 10})
    assert row['points_max'] == 10 and row['percent'] == 80
    assert normalize({'PointsNumerator': 1, 'PointsDenominator': 0}, {'MaxPoints': 10})['percent'] is None


@pytest.mark.parametrize('score,percent', [('4 / 5', 80), ('0 / 5', 0), ('– / 5', None)])
def test_weighted_only_html(score, percent):
    row = parse_html('<table><tr><th>Grade Item</th><th>Weight Achieved</th></tr>'
                     f'<tr><th>Quiz</th><td>{score}</td></tr></table>')['items'][0]
    assert row['percent'] == percent
    assert row['points_earned'] is None and row['weight_max'] == 5


def test_html_unreleased_score_is_suppressed():
    row = parse_html('<table><tr><th>Grade Item</th><th>Points</th><th>Grade</th></tr>'
                     '<tr><th>Quiz</th><td>8 / 10</td><td>Not released</td></tr></table>')['items'][0]
    assert row['points_earned'] is None and row['percent'] is None and row['display'] is None


async def test_null_embedded_grade_definitions_do_not_discard_values():
    browser = GradeBrowser()
    browser.data['categories'][0]['Grades'] = None
    result = await fetch_grades(browser, '123')
    assert result['coverage']['complete'] and result['items'][0]['points_earned'] == 8


@pytest.mark.parametrize('kind', ['builtin', 'playwright'])
async def test_timeout_is_clear_and_does_not_leak_secrets(service, kind, caplog):
    from playwright.async_api import TimeoutError as BrowserTimeoutError
    error = TimeoutError if kind == 'builtin' else BrowserTimeoutError
    service.browser.failures['values'] = error('SYNTHETIC-SECRET-cookie')
    result = await service.get_grades('123')
    assert 'timed out' in result['courses'][0]['coverage']['errors'][0]['message']
    assert 'SYNTHETIC-SECRET' not in json.dumps(result) + json.dumps(service.store.grades('123')) + caplog.text


@pytest.mark.parametrize('status,error', [(401, LoginRequired), (403, EndpointUnavailable)])
async def test_grade_api_http_errors_use_get_and_dispose(tmp_path, status, error):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    calls, disposed = [], []
    async def dispose():
        disposed.append(True)
    async def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status=status, headers={}, dispose=dispose)
    browser.context = SimpleNamespace(request=SimpleNamespace(get=get))
    browser.closed = False
    with pytest.raises(error):
        await browser.get('/d2l/api/le/1.99/123/grades/values/myGradeValues/')
    assert len(calls) == 1 and disposed == [True]


@pytest.mark.parametrize('paging', [
    {'PagingInfo': {'HasMoreItems': True, 'Bookmark': 'same'}},
    {'Next': '/d2l/api/le/1.99/123/grades/?page=same'},
])
async def test_grade_pagination_repetition_fails_closed(tmp_path, paging):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    calls = []
    async def get(path, params=None):
        calls.append(path)
        return {'Items': [], **paging}
    browser.get = get
    with pytest.raises(ValueError, match='repeated'):
        await browser.paged('/d2l/api/le/1.99/123/grades/')
    assert len(calls) == 2


async def test_adjusted_final_preserved_separately_in_api_and_html():
    browser = GradeBrowser()
    browser.data['final']['GradeObjectType'] = 8
    browser.data['final']['GradeObjectName'] = 'Final Adjusted Grade'
    result = await fetch_grades(browser, '123')
    assert result['final_calculated_grade'] is None
    assert result['final_adjusted_grade']['letter'] == 'A-'
    html = (FIXTURES / 'learner.html').read_text().replace('Final Calculated Grade', 'Final Adjusted Grade')
    result = parse_html(html)
    assert result['final_calculated_grade'] is None and result['final_adjusted_grade']['percent'] == 80


async def test_grade_metadata_requests_overlap_without_extra_fetches():
    browser = GradeBrowser()
    original = browser.get
    entered = set()
    release = asyncio.Event()
    async def get(path, **kwargs):
        if 'myGradeValues/' not in path:
            entered.add(path)
            if len(entered) == 3:
                release.set()
            await asyncio.wait_for(release.wait(), 1)
        return await original(path, **kwargs)
    browser.get = get
    result = await fetch_grades(browser, '123')
    assert result['coverage']['complete'] and len(browser.calls) == 4


async def test_final_in_grade_values_is_not_lost_if_final_endpoint_unavailable():
    browser = GradeBrowser()
    browser.data['values'].append({'GradeObjectIdentifier': '999', 'GradeObjectName': 'Final Adjusted Grade',
                                  'GradeObjectType': 8, 'DisplayedGrade': '90 %', 'IsReleased': True})
    browser.failures['final'] = EndpointUnavailable('HTTP 404', 404)
    result = await fetch_grades(browser, '123')
    assert result['final_adjusted_grade']['percent'] == 90
    assert all(r['id'] != '999' for r in result['items'])


@pytest.mark.parametrize('release_date', [None, ''])
async def test_individual_score_with_empty_release_date_is_returned_and_cached(service, release_date):
    service.browser.data['values'][0]['ReleasedDate'] = release_date
    row = (await service.get_grades('123'))['courses'][0]['items'][0]
    assert row['points_earned'] == 8
    assert row['percent'] == 80
    assert service.store.grades('123')['items'][0]['points_earned'] == 8


@pytest.mark.parametrize('grade_type', [7, 8])
@pytest.mark.parametrize('release_date', [None, ''])
def test_final_score_with_empty_release_date_remains_hidden(grade_type, release_date):
    row = normalize({'GradeObjectType': grade_type, 'ReleasedDate': release_date,
                     'PointsNumerator': 8, 'PointsDenominator': 10})
    assert row['flags']['hidden_or_unreleased'] is True
    assert row['points_earned'] is None
