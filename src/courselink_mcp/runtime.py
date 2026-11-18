"""Start one private local monitor when an MCP client needs it."""
import fcntl
import os
import subprocess
import sys
import time

import httpx


def ensure_service(config, timeout=20):
    base = f'http://127.0.0.1:{config.port}'
    override = os.environ.get('COURSELINK_SERVER_URL', base).rstrip('/')
    if override != base or os.environ.get('COURSELINK_SERVER_TOKEN'):
        raise ValueError('Automatic startup uses local state. Remove server URL/token overrides or use plain stdio.')
    deadline = time.monotonic() + timeout
    with httpx.Client(headers={'Authorization': 'Bearer ' + config.token()},
                      timeout=2, trust_env=False) as client:
        def running():
            try:
                response = client.get(base + '/api/status')
            except httpx.ConnectError:
                return False
            response.raise_for_status()
            data = response.json()
            if not isinstance(data.get('session'), dict) or 'monitored_courses' not in data:
                raise ValueError('The configured port is occupied by another service.')
            return True

        if running():
            return
        with (config.state / 'startup.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Another client is starting the monitor. Try again shortly.')
                    time.sleep(0.1)
            if running():
                return
            env = dict(os.environ, COURSELINK_STATE_DIR=str(config.state.resolve()), COURSELINK_BIND='127.0.0.1')
            log_path = config.state / 'service.log'
            with log_path.open('ab') as log:
                os.chmod(log_path, 0o600)
                process = subprocess.Popen([sys.executable, '-m', 'courselink_mcp.cli', 'serve'],
                    env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, umask=0o077)
            while time.monotonic() < deadline:
                if running():
                    return
                if process.poll() is not None:
                    raise RuntimeError(f'Monitor could not start. Check {log_path}; another login may own the browser.')
                time.sleep(0.1)
            raise TimeoutError(f'Monitor startup is taking too long. Check {log_path} before retrying.')
