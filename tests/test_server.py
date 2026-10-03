import httpx
import pytest
from courselink_mcp.config import Config
from courselink_mcp.server import create_app, API
from courselink_mcp.service import Service


@pytest.fixture
def setup(tmp_path):
    config = Config(state=tmp_path, school=tmp_path, courses={'1': 'example-course'})
    service = Service(config)
    yield config, service
    service.store.close()


async def test_auth_required_and_cross_origin_rejected(setup):
    config, service = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config, service)), base_url='http://localhost') as client:
        assert (await client.get('/api/status')).status_code == 401
        assert (await client.get('/mcp/')).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + config.token()
        assert (await client.get('/api/status')).status_code == 200
        assert (await client.get('/api/status', headers={'Origin': 'https://evil.test'})).status_code == 403
        assert (await client.get('/api/login')).status_code == 405
        assert (await client.post('/api/items', json={'offset': 'bad'})).status_code == 400


async def test_partial_failure_does_not_delete_cached_items(setup):
    config, service = setup
    previous = {'id': '1:assignment:2', 'course_id': '1', 'kind': 'assignment', 'title': 'Keep me'}
    service.store.reconcile('1', 'assignment', [previous])
    async def courses(): return [{'id': '1'}]
    async def empty(course): return []
    async def fail(course): raise RuntimeError('unavailable')
    service.catalog.courses = courses
    service.catalog.content = empty
    service.catalog.assignments = fail
    service.catalog.announcements = empty
    service.catalog.calendar = empty
    service.catalog.quizzes = empty
    report = await service.scan()
    assert not report['complete']
    assert report['errors'][0]['section'] == 'assignment'
    assert service.store.get(previous['id'])['available']


def test_file_api_cannot_read_arbitrary_files(setup, tmp_path):
    config, service = setup
    outside = tmp_path / 'secret'
    outside.write_text('secret')
    service.store.db.execute('INSERT INTO versions(item_id,sha256,path,size,downloaded_at) VALUES(?,?,?,?,?)',
                            ('1:content:2', 'x', str(outside), 6, 'now'))
    with pytest.raises(ValueError):
        API(service).file_path(1)


async def test_course_configuration_applies_on_next_scan_without_restart(setup):
    import json
    config, service = setup
    (config.state / 'config.json').write_text(json.dumps({'courses': {'2': 'second-course'}}))
    async def courses(): return [{'id': '2'}]
    async def empty(course):
        assert course == '2'
        return []
    service.catalog.courses = courses
    for method in ('content', 'assignments', 'announcements', 'calendar', 'quizzes'):
        setattr(service.catalog, method, empty)
    report = await service.scan()
    assert report['complete'] and config.courses == {'2': 'second-course'}
    assert service.store.courses()[0]['monitored']


async def test_invalid_configuration_never_reports_complete_scan(setup):
    import json
    config, service = setup
    (config.state / 'config.json').write_text(json.dumps({'courses': {'2': '../outside'}}))
    with pytest.raises(ValueError):
        await service.scan()
    report = service.store.get_state('last_scan')
    assert not report['complete'] and report['errors']
    assert config.courses == {'1': 'example-course'}


@pytest.mark.parametrize('declared_length', [None, '1', '70000'])
async def test_post_body_limit_checks_stream_not_just_headers(setup, declared_length):
    config, service = setup
    async def chunks():
        yield b'{"padding":"'
        yield b'x' * 65536
        raise AssertionError('Oversized request must stop being consumed immediately')
    headers = {'Authorization': 'Bearer ' + config.token()}
    if declared_length is not None:
        headers['Content-Length'] = declared_length
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config, service)), base_url='http://localhost') as client:
        response = await client.post('/api/status', content=chunks(), headers=headers)
        assert response.status_code == 413


async def test_small_chunked_request_still_works_and_invalid_json_fails(setup):
    config, service = setup
    async def chunks():
        yield b'{"limit":'
        yield b'10}'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config, service)), base_url='http://localhost',
                                 headers={'Authorization': 'Bearer ' + config.token()}) as client:
        response = await client.post('/api/items', content=chunks())
        assert response.status_code == 200 and response.json()['items'] == []
        assert (await client.post('/api/items', content=b'{bad')).status_code == 400


async def test_scan_request_preserves_thorough_check_when_requests_coalesce(setup):
    _, service = setup
    api = API(service)
    assert (await api.call('scan', {'verify_files': True}))['verify_files']
    assert (await api.call('scan', {}))['verify_files']
    assert service.verify_files_requested and service.wake.is_set()
    with pytest.raises(ValueError):
        await api.call('scan', {'verify_files': 'false'})


async def test_parent_failure_preserves_links_and_reports_stale_cache(setup):
    from datetime import datetime, timezone, timedelta
    _, service = setup
    old = {'id': '1:linked_file:previous', 'course_id': '1', 'kind': 'linked_file', 'title': 'Keep link'}
    service.store.upsert_link(old)
    checked = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    service.store.set_state('last_successful_scan', {'complete': True, 'finished_at': checked})
    async def courses(): return [{'id': '1'}]
    async def empty(course): return []
    async def fail(course): raise RuntimeError('Synthetic section outage')
    service.catalog.courses = courses
    for name in ('content', 'assignments', 'announcements', 'calendar', 'quizzes'):
        setattr(service.catalog, name, empty)
    service.catalog.assignments = fail
    report = await service.scan()
    assert not report['complete'] and service.store.get(old['id'])['available']
    assert any(e['section'] == 'linked_file' for e in report['errors'])
    result = await API(service).call('changes', {'after': 0})
    assert result['freshness']['checked_at'] == checked
    assert result['freshness']['refresh_due'] and not result['freshness']['latest_scan_complete']
    assert set(report['phases']) == {'enrollment', 'content', 'assignment', 'announcement', 'event', 'quiz', 'downloads', 'linked_files'}
    assert all(p['seconds'] >= 0 and p['calls'] == 1 for p in report['phases'].values())
    service.catalog.assignments = empty
    report = await service.scan()
    assert report['complete'] and not service.store.get(old['id'])['available']
    fresh = (await API(service).call('changes', {}))['freshness']
    assert fresh['latest_scan_complete'] and not fresh['refresh_due']
    assert fresh['checked_at'] == report['finished_at']
