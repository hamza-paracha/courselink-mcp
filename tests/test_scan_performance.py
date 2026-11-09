import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from courselink_mcp.browser import RateLimited
from courselink_mcp.config import Config
from courselink_mcp.service import Service
from courselink_mcp.server import API


@pytest.fixture
async def service(tmp_path):
    instance = Service(Config(state=tmp_path, school=tmp_path / 'downloads', courses={'1': 'example'}))
    yield instance
    await instance.close()


async def test_scan_content_cache_refreshes_periodically_and_preserves_failed_toc(service):
    toc = {'Modules': [{'ModuleId': 1, 'Title': 'Week', 'LastModifiedDate': 'toc-date'}]}
    calls = []
    description = 'Original'
    fail = False
    async def get(path):
        calls.append(path)
        if path.endswith('/toc'):
            return deepcopy(toc)
        if fail:
            raise ValueError('synthetic detail failure')
        return {'Description': {'Text': description}, 'LastModifiedDate': 'detail-date'}
    service.browser.versions = {'le': '1.99'}
    service.browser.get = get
    async def courses(): return [{'id': '1'}]
    async def empty(course): return []
    service.catalog.courses = courses
    for method in ('assignments', 'announcements', 'calendar', 'quizzes'):
        setattr(service.catalog, method, empty)
    assert (await service.scan())['complete']
    changes = service.store.changes()['next_cursor']
    calls.clear()
    assert (await service.scan())['complete']
    assert len(calls) == 1 and not service.store.changes(after=changes)['changes']
    description = 'Edited without TOC timestamp change'
    assert (await service.scan(verify_files=True))['complete']
    assert service.store.get('1:content:module:1')['description'] == description
    service.store.set_state('content_refreshed:1', (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    calls.clear()
    assert (await service.scan())['complete'] and len(calls) == 2
    previous = service.store.get_state('content_toc:1')
    toc['Modules'][0]['LastModifiedDate'] = 'new-toc-date'
    fail = True
    assert not (await service.scan())['complete']
    assert service.store.get_state('content_toc:1') == previous


async def test_course_sections_overlap_and_session_errors_cancel_all_siblings(service):
    service.config.courses['2'] = 'second'
    async def courses(): return [{'id': '1'}, {'id': '2'}]
    service.catalog.courses = courses
    started = set()
    ready = asyncio.Event()
    cancelled = []
    async def fetch(course, **kwargs):
        task = asyncio.current_task()
        started.add(task)
        if len(started) == 10:
            ready.set()
        try:
            await asyncio.wait_for(ready.wait(), 1)
            if course == '1' and len(cancelled) == 0:
                # Choose exactly one task to raise while the others stay blocked.
                cancelled.append('failure')
                raise RateLimited('synthetic cooldown')
            await asyncio.Event().wait()
        finally:
            cancelled.append(task)
    for method in ('content', 'assignments', 'announcements', 'calendar', 'quizzes'):
        setattr(service.catalog, method, fetch)
    with pytest.raises(RateLimited):
        await asyncio.wait_for(service.scan(), 2)
    assert len(started) == 10 and len(cancelled) == 11
    assert not service.store.get_state('last_scan')['complete']


async def test_latest_versions_filters_availability_and_course(service, tmp_path):
    for course in ('1', '2'):
        row = {'id': f'{course}:content:1', 'course_id': course, 'kind': 'content', 'title': 'File'}
        service.store.reconcile(course, 'content', [row])
        for index in range(2):
            temp = tmp_path / 'temporary'
            temp.write_bytes(b'example')
            service.store.record_file(row, temp, tmp_path / f'{course}-{index}', str(index), 7)
    versions = (await API(service).call('latest_versions', {'course_id': '1'}))['versions']
    assert list(versions) == ['1:content:1'] and versions['1:content:1']['sha256'] == '1'
    service.store.reconcile('1', 'content', [])
    assert service.store.latest_versions('1') == {}


async def test_extractor_reuses_worker_and_recovers_after_timeout(service, tmp_path, monkeypatch):
    path = tmp_path / 'instructions.txt'
    path.write_text('Synthetic instructions')
    first = await service.extract_document({'id': 1, 'path': str(path)})
    pool = service.extract_pool
    second = await service.extract_document({'id': 2, 'path': str(path)})
    assert first['text'] == second['text'] == 'Synthetic instructions'
    assert service.extract_pool is pool
    wait_for = asyncio.wait_for
    async def timeout(awaitable, seconds):
        awaitable.cancel()
        raise asyncio.TimeoutError
    monkeypatch.setattr(asyncio, 'wait_for', timeout)
    assert (await service.extract_document({'id': 3, 'path': str(path)}))['status'] == 'extraction_timeout'
    assert service.extract_pool is None
    monkeypatch.setattr(asyncio, 'wait_for', wait_for)
    assert (await service.extract_document({'id': 4, 'path': str(path)}))['text'] == 'Synthetic instructions'
