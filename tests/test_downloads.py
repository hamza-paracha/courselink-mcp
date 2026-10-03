from pathlib import Path
import hashlib

import httpx
import pytest

from courselink_mcp.browser import LoginRequired
from courselink_mcp.config import Config
from courselink_mcp.service import Service


@pytest.fixture
async def download_service(tmp_path, monkeypatch):
    config = Config(state=tmp_path, school=tmp_path, courses={'1': 'example-course'})
    service = Service(config)
    service.browser.status = {'state': 'authenticated'}
    async def cookies(): return httpx.Cookies()
    service.browser.cookies = cookies
    row = {'id': '1:content:2', 'course_id': '1', 'kind': 'content', 'title': 'Lab',
           'filename': '../../lab.pdf', 'download_path': '/d2l/api/le/1.99/1/content/topics/2/file'}
    service.store.reconcile('1', 'content', [row])
    real_client = httpx.AsyncClient
    def mock(handler):
        monkeypatch.setattr('courselink_mcp.service.httpx.AsyncClient',
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    yield service, row, mock
    await service.close()


async def test_same_url_replacement_is_versioned_without_metadata_change(download_service):
    service, row, mock = download_service
    data = b'%PDF-first'
    mock(lambda request: httpx.Response(200, content=data, headers={'content-type': 'application/pdf'}))
    first = await service.download(row['id'])
    same = await service.download(row['id'])
    data = b'%PDF-updated'
    changed = await service.download(row['id'])
    assert first['new_version'] and not same['new_version'] and changed['new_version']
    assert len(service.store.versions(row['id'])) == 2
    assert Path(first['version']['path']).read_bytes() == b'%PDF-first'
    assert Path(changed['version']['path']).read_bytes() == data
    assert changed['version']['sha256'] == hashlib.sha256(data).hexdigest()
    assert Path(changed['version']['path']).is_relative_to(service.config.school / 'example-course')


async def test_login_html_never_overwrites_a_file(download_service):
    service, row, mock = download_service
    mock(lambda request: httpx.Response(200, content=b'<html>Sign in</html>', headers={'content-type': 'text/html'}))
    with pytest.raises(LoginRequired):
        await service.download(row['id'])
    assert service.store.versions(row['id']) == []
    assert list(service.config.school.rglob('*.partial')) == []


async def test_redirect_to_foreign_domain_is_not_followed(download_service):
    service, row, mock = download_service
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://evil.test/collect'})
    mock(handler)
    with pytest.raises(LoginRequired):
        await service.download(row['id'])
    assert len(calls) == 1


async def test_size_limit_cleans_partial_files(download_service):
    service, row, mock = download_service
    service.config.max_file_bytes = 3
    mock(lambda request: httpx.Response(200, content=b'12345'))
    with pytest.raises(ValueError):
        await service.download(row['id'])
    assert list(service.config.school.rglob('*.partial')) == []


async def test_scan_downloads_are_bounded_and_reuse_a_client(download_service):
    import asyncio
    service, row, mock = download_service
    rows = [dict(row, id=f'1:content:{i}', download_path=f'/content/{i}.pdf') for i in range(7)]
    service.store.reconcile('1', 'content', rows)
    active = peak = calls = 0

    async def handler(request):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(200, content=b'%PDF-test', headers={'content-type': 'application/pdf'})
        finally:
            active -= 1
    mock(handler)
    report = {'files_saved': 0, 'errors': []}
    await service.download_rows('1', rows, report)
    client = service.download_client
    assert peak == 3 and calls == 7 and report == {'files_saved': 7, 'errors': []}
    await service.download_rows('1', rows, report)
    assert calls == 7 and service.download_client is client
    assert service.active_downloads == 0


async def test_document_reads_reuse_cache_but_allow_forced_refresh(download_service):
    from courselink_mcp.server import API
    service, row, mock = download_service
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b'%PDF-test', headers={'content-type': 'application/pdf'})
    mock(handler)
    async def extract(version, member=None):
        return {'text': 'Assignment instructions'}
    service.extract_document = extract
    api = API(service)
    first = await api.call('document', {'item_id': row['id']})
    second = await api.call('document', {'item_id': row['id'], 'offset': 5})
    assert first['freshly_checked'] and not second['freshly_checked'] and calls == 1
    await api.call('document', {'item_id': row['id'], 'refresh': True})
    assert calls == 2
    service.store.reconcile('1', 'content', [dict(row, title='Updated instructions')])
    cached = await api.call('document', {'item_id': row['id']})
    assert cached['refresh_queued'] and not cached['freshly_checked'] and calls == 2
    assert cached['source_checked_at'] and service.wake.is_set()
    await api.call('document', {'item_id': row['id'], 'refresh': True})
    assert calls == 3
    service.browser.status = {'state': 'login_required'}
    cached = await api.call('document', {'item_id': row['id']})
    assert cached['text'] == 'Assignment instructions' and not cached['freshly_checked'] and calls == 3


async def test_pooled_downloads_use_current_browser_cookies(download_service):
    service, row, mock = download_service
    cookie_value = 'synthetic-first'
    async def cookies():
        return httpx.Cookies({'example_session': cookie_value})
    service.browser.cookies = cookies
    seen = []
    def handler(request):
        seen.append(request.headers.get('cookie'))
        return httpx.Response(200, content=b'%PDF-test', headers={'content-type': 'application/pdf'})
    mock(handler)
    await service.download(row['id'])
    cookie_value = 'synthetic-renewed'
    await service.download(row['id'])
    assert seen == ['example_session=synthetic-first', 'example_session=synthetic-renewed']


async def test_rate_limit_cancels_remaining_batch_work(download_service):
    import asyncio
    from courselink_mcp.browser import RateLimited
    service, row, mock = download_service
    rows = [dict(row, id=f'1:content:{i}', download_path=f'/content/{i}.pdf') for i in range(10)]
    service.store.reconcile('1', 'content', rows)
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        if request.url.path.endswith('/0.pdf'):
            return httpx.Response(429)
        await asyncio.sleep(5)
        return httpx.Response(200, content=b'%PDF-test')
    mock(handler)
    with pytest.raises(RateLimited):
        await service.download_rows('1', rows, {'files_saved': 0, 'errors': []})
    assert calls <= 3 and service.active_downloads == 0
    assert not list(service.config.school.rglob('*.partial'))


async def test_broken_link_does_not_starve_other_files_or_hide_previous_links(download_service):
    service, row, mock = download_service
    parent = dict(row, download_path=None, instructions={'Html':
        '<a href="/content/broken.pdf">Broken</a><a href="/content/good.x68">Starter</a>'})
    service.store.reconcile('1', 'content', [parent])
    old = {'id': '1:linked_file:old', 'course_id': '1', 'kind': 'linked_file', 'title': 'Keep cached'}
    service.store.upsert_link(old)
    broken = True
    def handler(request):
        if broken and request.url.path.endswith('broken.pdf'):
            return httpx.Response(500)
        return httpx.Response(200, content=b'MOVE D0,D1', headers={'content-type': 'text/plain'})
    mock(handler)
    count, saved, errors = await service.scan_linked_files('1')
    assert count == 2 and saved == 1 and len(errors) == 1
    assert service.store.get(old['id'])['available']
    good = next(r for r in service.store.items(kind='linked_file')['items'] if r.get('filename') == 'good.x68')
    assert service.store.versions(good['id'])
    broken = False
    count, saved, errors = await service.scan_linked_files('1')
    assert count == 2 and saved == 1 and not errors
    assert not service.store.get(old['id'])['available']


async def test_thorough_check_finds_byte_changes_before_periodic_recheck(download_service):
    service, row, mock = download_service
    data = b'%PDF-original'
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=data, headers={'content-type': 'application/pdf'})
    mock(handler)
    await service.download(row['id'])
    data = b'%PDF-replaced-without-metadata-change'
    report = {'files_saved': 0, 'errors': []}
    await service.download_rows('1', [row], report)
    assert calls == 1 and not report['files_saved']
    await service.download_rows('1', [row], report, verify_files=True)
    assert calls == 2 and report['files_saved'] == 1
    assert len(service.store.versions(row['id'])) == 2


async def test_full_scan_reports_link_failure_without_losing_successful_files(download_service):
    service, row, mock = download_service
    parent = dict(row, download_path=None, instructions={'Html':
        '<a href="/content/unavailable.pdf">Missing</a><a href="/content/available.pdf">Available</a>'})
    async def courses(): return [{'id': '1'}]
    async def content(course): return [parent]
    async def empty(course): return []
    service.catalog.courses = courses
    service.catalog.content = content
    for name in ('assignments', 'announcements', 'calendar', 'quizzes'):
        setattr(service.catalog, name, empty)
    def handler(request):
        return httpx.Response(404 if request.url.path.endswith('/unavailable.pdf') else 200,
                              content=b'%PDF-synthetic', headers={'content-type': 'application/pdf'})
    mock(handler)
    report = await service.scan(verify_files=True)
    assert not report['complete'] and report['verify_files']
    assert report['duration_seconds'] >= 0 and report['files_saved'] == 1
    assert len(report['errors']) == 1 and report['errors'][0]['section'] == 'linked_file'
    assert report['courses']['1']['linked_file'] == 2


async def test_topic_file_404_uses_same_origin_course_source(download_service):
    service, row, mock = download_service
    row = dict(row, source_url='/shared/instructions.pdf')
    service.store.reconcile('1', 'content', [row])
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path.startswith('/d2l/api/'):
            return httpx.Response(404)
        return httpx.Response(200, content=b'%PDF-direct', headers={'content-type': 'application/pdf'})
    mock(handler)
    result = await service.download(row['id'])
    assert result['new_version']
    assert paths == [row['download_path'], '/shared/instructions.pdf']


@pytest.mark.parametrize('source', ['https://evil.test/content/file.pdf', '/d2l/logout', '/content/%2e%2e/d2l/logout'])
async def test_topic_fallback_never_follows_untrusted_sources(download_service, source):
    service, row, mock = download_service
    row = dict(row, source_url=source)
    service.store.reconcile('1', 'content', [row])
    paths = []
    def handler(request):
        paths.append(str(request.url))
        return httpx.Response(404)
    mock(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await service.download(row['id'])
    assert len(paths) == 1


async def test_download_rate_limit_also_gates_metadata(download_service):
    import time
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from courselink_mcp.browser import RateLimited
    service, row, mock = download_service
    mock(lambda request: httpx.Response(429, headers={'retry-after': '120'}))
    with pytest.raises(RateLimited):
        await service.download(row['id'])
    assert service.browser.rate_limited_until - time.monotonic() > 119
    request = AsyncMock()
    service.browser.context = SimpleNamespace(request=SimpleNamespace(get=request), close=AsyncMock())
    service.browser.closed = False
    with pytest.raises(RateLimited):
        await service.browser.get('/d2l/api/example')
    with pytest.raises(RateLimited):
        await service.download(row['id'])
    request.assert_not_called()
    assert not list(service.config.school.rglob('*.partial'))


async def test_failed_parent_html_refresh_keeps_previous_link_index(download_service):
    from pathlib import Path
    from unittest.mock import AsyncMock
    service, row, mock = download_service
    parent = dict(row, filename='index.html', source_url='/content/index.html')
    service.store.reconcile('1', 'content', [parent])
    saved = service.config.state / 'old-page'
    saved.write_text('<p>Old cached page</p>')
    service.store.record_file(parent, saved, service.config.state / 'page.html', 'synthetic', 22)
    old = {'id': '1:linked_file:old', 'course_id': '1', 'kind': 'linked_file', 'title': 'Keep previous link'}
    service.store.upsert_link(old)
    service.catalog.courses = AsyncMock(return_value=[{'id': '1'}])
    service.catalog.content = AsyncMock(return_value=[parent])
    for name in ('assignments', 'announcements', 'calendar', 'quizzes'):
        setattr(service.catalog, name, AsyncMock(return_value=[]))
    mock(lambda request: httpx.Response(500))
    report = await service.scan(verify_files=True)
    assert not report['complete'] and service.store.get(old['id'])['available']
    assert any('Parent metadata or files' in e['message'] for e in report['errors'])
