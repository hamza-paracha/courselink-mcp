import asyncio
import time
from types import SimpleNamespace

from courselink_mcp.browser import BrowserSession
from courselink_mcp.config import Config


class Response:
    def __init__(self, status=200):
        self.status = status
        self.headers = {'content-type': 'application/json', 'retry-after': '1'}
        self.disposed = False
    async def json(self):
        return {'ok': True}
    async def dispose(self):
        self.disposed = True


async def test_api_requests_share_a_concurrency_limit(tmp_path):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path, api_concurrency=2))
    active = peak = 0
    responses = []
    async def get(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(0.01)
            response = Response()
            responses.append(response)
            return response
        finally:
            active -= 1
    browser.context = SimpleNamespace(request=SimpleNamespace(get=get))
    browser.closed = False
    await asyncio.gather(*(browser.get('/d2l/api/example') for _ in range(8)))
    assert peak == 2 and active == 0
    assert browser.metrics['requests'] == 8 and all(r.disposed for r in responses)


async def test_rate_limit_cooldown_applies_to_new_calls_too(tmp_path):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path, api_concurrency=2))
    timestamps = []
    limited = asyncio.Event()
    responses = []
    async def get(*args, **kwargs):
        timestamps.append(time.monotonic())
        response = Response(429 if len(timestamps) == 1 else 200)
        responses.append(response)
        if response.status == 429:
            limited.set()
        return response
    browser.context = SimpleNamespace(request=SimpleNamespace(get=get))
    browser.closed = False
    first = asyncio.create_task(browser.get('/d2l/api/example'))
    await limited.wait()
    second = asyncio.create_task(browser.get('/d2l/api/another'))
    await asyncio.gather(first, second)
    assert len(timestamps) == 3
    assert min(timestamps[1:]) - timestamps[0] >= 0.95
    assert browser.metrics['rate_limits'] == 1 and all(r.disposed for r in responses)


async def test_long_cooldown_defers_without_early_retry(tmp_path):
    import pytest
    from courselink_mcp.browser import RateLimited
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    calls = 0
    response = Response(429)
    response.headers['retry-after'] = '120'
    async def get(*args, **kwargs):
        nonlocal calls
        calls += 1
        return response
    browser.context = SimpleNamespace(request=SimpleNamespace(get=get))
    browser.closed = False
    for _ in range(2):
        with pytest.raises(RateLimited):
            await browser.get('/d2l/api/example')
    assert calls == 1 and response.disposed
    assert browser.rate_limited_until - time.monotonic() > 119
    # Deferred work released the slot.
    await asyncio.wait_for(browser.api_slots.acquire(), 0.5)
    browser.api_slots.release()


def test_retry_after_dates_and_malformed_values(monkeypatch):
    from courselink_mcp.browser import retry_after_seconds
    from email.utils import formatdate
    monkeypatch.setattr('courselink_mcp.browser.time.time', lambda: 1000000)
    assert retry_after_seconds(formatdate(1000120, usegmt=True)) == 120
    assert retry_after_seconds('3600') == 3600
    assert retry_after_seconds('-1') == 1
    for value in ('bad', 'nan', 'inf', None):
        assert retry_after_seconds(value) == 10


async def test_transport_errors_are_timed_and_scopes_exclude_other_tasks(tmp_path):
    import pytest
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    started, release = asyncio.Event(), asyncio.Event()
    async def get(path, **kwargs):
        if path.endswith('/failure'):
            started.set()
            await release.wait()
            raise OSError('Synthetic transport failure')
        return Response()
    browser.context = SimpleNamespace(request=SimpleNamespace(get=get))
    browser.closed = False
    async def background():
        await started.wait()
        await browser.get('/background')
        release.set()
    task = asyncio.create_task(background())  # Created outside the measurement scope.
    with browser.measure() as metrics:
        with pytest.raises(OSError):
            await browser.get('/failure')
    await task
    assert metrics['requests'] == metrics['errors'] == 1
    assert metrics['successes'] == 0 and metrics['request_seconds'] > 0
    assert browser.metrics['requests'] == 2 and browser.metrics['successes'] == 1


async def test_cancellation_releases_request_slot(tmp_path):
    import pytest
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path, api_concurrency=1))
    started = asyncio.Event()
    async def get(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    browser.context = SimpleNamespace(request=SimpleNamespace(get=get))
    browser.closed = False
    task = asyncio.create_task(browser.get('/example'))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(browser.api_slots.acquire(), 0.5)
    browser.api_slots.release()
    assert browser.metrics['cancellations'] == 1
