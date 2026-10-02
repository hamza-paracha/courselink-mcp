import httpx
import pytest

from courselink_mcp.config import Config
from courselink_mcp.runtime import ensure_service


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv('COURSELINK_STATE_DIR', str(tmp_path))
    monkeypatch.delenv('COURSELINK_SERVER_URL', raising=False)
    monkeypatch.delenv('COURSELINK_SERVER_TOKEN', raising=False)
    return Config.load()


def test_startup_reuses_running_monitor(config, monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr('courselink_mcp.runtime.httpx.Client', lambda **kw: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            'session': {'state': 'authenticated'}, 'monitored_courses': {}})), **kw))
    monkeypatch.setattr('courselink_mcp.runtime.subprocess.Popen', lambda *a, **kw: pytest.fail('duplicate process'))
    ensure_service(config)


def test_startup_is_private_and_waits_for_health(config, monkeypatch):
    real_client = httpx.Client
    started = []
    def handler(request):
        assert request.headers['authorization'] == 'Bearer ' + config.token()
        if not started:
            raise httpx.ConnectError('not running')
        return httpx.Response(200, json={'session': {'state': 'starting'}, 'monitored_courses': {}})
    monkeypatch.setattr('courselink_mcp.runtime.httpx.Client', lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    def spawn(args, **kw):
        started.append((args, kw))
        return object()
    monkeypatch.setattr('courselink_mcp.runtime.subprocess.Popen', spawn)
    ensure_service(config)
    assert len(started) == 1
    args, options = started[0]
    assert args[-1] == 'serve' and options['start_new_session']
    assert options['env']['COURSELINK_BIND'] == '127.0.0.1'
    assert options['env']['COURSELINK_STATE_DIR'] == str(config.state)
    assert (config.state / 'service.log').stat().st_mode & 0o777 == 0o600


def test_startup_does_not_spawn_on_auth_conflict(config, monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr('courselink_mcp.runtime.httpx.Client', lambda **kw: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401)), **kw))
    monkeypatch.setattr('courselink_mcp.runtime.subprocess.Popen', lambda *a, **kw: pytest.fail('unsafe start'))
    with pytest.raises(httpx.HTTPStatusError):
        ensure_service(config)


def test_auto_start_rejects_remote_override(config, monkeypatch):
    monkeypatch.setenv('COURSELINK_SERVER_URL', 'https://example.com')
    with pytest.raises(ValueError, match='local state'):
        ensure_service(config)
