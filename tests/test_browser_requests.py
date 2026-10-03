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
