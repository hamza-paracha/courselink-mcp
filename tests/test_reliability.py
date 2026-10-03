import asyncio
from contextlib import closing
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from courselink_mcp.browser import BrowserSession
from courselink_mcp.config import Config
from courselink_mcp import maintenance
from courselink_mcp.service import Service
from courselink_mcp.server import create_app


@pytest.fixture
def config(tmp_path):
    return Config(state=tmp_path, school=tmp_path, courses={'1': 'example'})


async def test_worker_health_detects_stalls_and_crashes_but_allows_sign_in(config):
    service = Service(config)
    service.tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(3)]
    service.heartbeats = dict.fromkeys(('session', 'scan', 'index'), time.monotonic())
    service.browser.status = {'state': 'login_required'}
    assert service.health()['healthy']
    service.heartbeats['session'] -= 1000
    assert service.health()['stalled_workers'] == ['session']
    service.heartbeats['session'] = time.monotonic()
    service.tasks[0].cancel()
    await asyncio.sleep(0)
    assert not service.health()['healthy']
    await service.close()


async def test_browser_cleanup_stops_driver_even_when_context_close_fails(config):
    browser = BrowserSession(config)
    driver = SimpleNamespace(stop=AsyncMock())
    browser.context = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('closed')))
    browser.playwright = driver
    with pytest.raises(RuntimeError):
        await browser.close()
    driver.stop.assert_awaited_once()
    assert browser.closed and browser.context is None and browser.playwright is None
    await browser.close()  # Cleanup can safely be repeated.


async def test_failed_worker_does_not_skip_shutdown_cleanup(config):
    service = Service(config)
    async def fail():
        raise RuntimeError('worker failed')
    service.tasks = [asyncio.create_task(fail())]
    await asyncio.sleep(0)
    service.browser.close = AsyncMock(side_effect=RuntimeError('browser failed'))
    service.download_client = SimpleNamespace(aclose=AsyncMock())
    with pytest.raises(RuntimeError):
        await service.close()
    service.download_client.aclose.assert_awaited_once()
    with pytest.raises(sqlite3.ProgrammingError):
        service.store.db.execute('SELECT 1')


async def test_cancelled_scan_never_reports_complete(config):
    service = Service(config)
    service.catalog.courses = AsyncMock(return_value=[{'id': '1'}])
    for name in ('content', 'assignments', 'announcements', 'calendar', 'quizzes'):
        setattr(service.catalog, name, AsyncMock(return_value=[]))
    started = asyncio.Event()
    async def linked(course):
        started.set()
        await asyncio.Event().wait()
    service.scan_linked_files = linked
    task = asyncio.create_task(service.scan())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = service.store.get_state('last_scan')
    assert not report['complete'] and report['finished_at']
    assert report['errors'][0]['section'] == 'scan'
    await service.close()


async def test_health_endpoint_requires_authentication(config):
    service = Service(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config, service)),
                                 base_url='http://localhost') as client:
        assert (await client.get('/api/health')).status_code == 401
        response = await client.get('/api/health', headers={'Authorization': 'Bearer ' + config.token()})
        assert response.status_code == 200 and response.json()['healthy'] is False
        assert 'session' not in response.json()
    await service.close()


def test_recovery_requires_repeated_failures_and_obeys_cooldown():
    state = {}
    assert not maintenance.recovery_due(state, False, 1000)
    assert not maintenance.recovery_due(state, False, 1060)
    assert not maintenance.recovery_due(state, True, 1120)
    assert state['failures'] == 0
    for timestamp in (1180, 1240):
        assert not maintenance.recovery_due(state, False, timestamp)
    assert maintenance.recovery_due(state, False, 1300)
    for timestamp in range(1360, 1900, 60):
        assert not maintenance.recovery_due(state, False, timestamp)
    assert maintenance.recovery_due(state, False, 1900)


def test_watchdog_persists_failures_and_recovers_only_unhealthy_backend(config, monkeypatch):
    monkeypatch.setattr(maintenance, 'monitor_healthy', lambda *_: False)
    monkeypatch.setattr(maintenance, 'tunnel_ready', lambda *_: pytest.fail('Backend must recover first'))
    calls = []
    monkeypatch.setattr(maintenance, 'restart', calls.append)
    for _ in range(3):
        maintenance.watchdog(config, tunnel=True)
    assert calls == ['courselink.service']
    maintenance.watchdog(config, tunnel=True)
    assert len(calls) == 1
    assert (config.state / 'watchdog.json').stat().st_mode & 0o777 == 0o600


def test_watchdog_can_recover_tunnel_independently(config, monkeypatch):
    monkeypatch.setattr(maintenance, 'monitor_healthy', lambda *_: True)
    monkeypatch.setattr(maintenance, 'tunnel_ready', lambda *_: False)
    calls = []
    monkeypatch.setattr(maintenance, 'restart', calls.append)
    for _ in range(3):
        maintenance.watchdog(config, tunnel=True)
    assert calls == ['courselink-tunnel.service']


def test_monitor_probe_uses_private_auth_and_fails_closed(config):
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer ' + config.token()
        assert request.url.host == '127.0.0.1' and request.url.path == '/api/health'
        return httpx.Response(200, json={'healthy': True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert maintenance.monitor_healthy(client, config)
    for response in (httpx.Response(503), httpx.Response(200, text='bad'),
                     httpx.Response(200, json={'healthy': 'true'})):
        with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
            assert not maintenance.monitor_healthy(client, config)


def test_backup_restores_committed_wal_and_rotates_only_own_snapshots(config):
    service = Service(config)
    service.store.set_state('example', {'value': 42})
    directory = config.state / 'backups'
    directory.mkdir()
    for day in range(1, 10):
        (directory / f'catalog-2000-01-{day:02d}.sqlite3').touch()
    (directory / 'keep.txt').write_text('unrelated')
    backup = maintenance.backup_catalog(config)
    service.store.close()
    with closing(sqlite3.connect(backup)) as restored:
        assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert json.loads(restored.execute('SELECT value FROM state WHERE key=?', ('example',)).fetchone()[0]) == {'value': 42}
    assert backup.stat().st_mode & 0o777 == 0o600
    assert len(list(directory.glob('*.sqlite3'))) == 7
    assert (directory / 'keep.txt').is_file()
    assert not list(directory.glob('*.tmp'))


def test_failed_backup_preserves_previous_snapshots(config):
    source = config.state / 'catalog.sqlite3'
    source.write_bytes(b'not a database')
    directory = config.state / 'backups'
    directory.mkdir()
    old = directory / 'catalog-2000-01-01.sqlite3'
    old.write_bytes(b'previous')
    with pytest.raises(sqlite3.DatabaseError):
        maintenance.backup_catalog(config)
    assert old.read_bytes() == b'previous'
    assert not list(directory.glob('*.tmp'))


async def test_indexer_drains_backlog_before_idle_sleep(config, monkeypatch):
    service = Service(config)
    remaining = list(range(12))
    processed = []
    service.store.pending_documents = lambda: remaining[:5]
    async def extract(version):
        processed.append(version)
        remaining.remove(version)
    async def sleep(seconds):
        if seconds >= 5:
            assert not remaining
            raise asyncio.CancelledError
    service.extract_document = extract
    monkeypatch.setattr('courselink_mcp.service.asyncio.sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await service.index_loop()
    assert processed == list(range(12))
    await service.close()


async def test_indexer_backs_off_after_extraction_failure(config, monkeypatch):
    service = Service(config)
    service.store.pending_documents = lambda: [1]
    service.extract_document = AsyncMock(side_effect=RuntimeError('broken file'))
    async def sleep(seconds):
        assert seconds == 5
        raise asyncio.CancelledError
    monkeypatch.setattr('courselink_mcp.service.asyncio.sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await service.index_loop()
    service.extract_document.assert_awaited_once()
    await service.close()
