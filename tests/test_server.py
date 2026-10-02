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
