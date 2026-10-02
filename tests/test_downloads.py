from pathlib import Path
import hashlib

import httpx
import pytest

from courselink_mcp.browser import LoginRequired
from courselink_mcp.config import Config
from courselink_mcp.service import Service


@pytest.fixture
def download_service(tmp_path, monkeypatch):
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
    service.store.close()


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
