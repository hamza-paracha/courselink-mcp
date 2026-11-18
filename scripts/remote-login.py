"""Refresh a remote CourseLink session through SSH and a local browser."""
import argparse
import asyncio
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import tempfile

from courselink_mcp.config import Config
from courselink_mcp.login import login


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Your SSH host or alias')
    parser.add_argument('--state-dir', required=True, help='Absolute private state directory on the server')
    parser.add_argument('--service', default='courselink.service', help='User systemd service to restart')
    args = parser.parse_args()
    if args.host.startswith('-') or not args.host.strip():
        parser.error('Provide a valid SSH host.')
    if not PurePosixPath(args.state_dir).is_absolute():
        parser.error('--state-dir must be an absolute server path.')
    if args.service.startswith('-') or not args.service.strip():
        parser.error('Provide a valid service name.')
    os.umask(0o077)
    original = Config.load()
    previous_state = os.environ.get('COURSELINK_STATE_DIR')
    try:
        with tempfile.TemporaryDirectory(prefix='courselink-login-') as folder:
            settings = json.loads((original.state / 'config.json').read_text())
            settings['headless'] = False
            Path(folder, 'config.json').write_text(json.dumps(settings))
            os.environ['COURSELINK_STATE_DIR'] = folder
            asyncio.run(login())
            session = Path(folder, 'browser-auth.json').read_bytes()
            script = '''import json, os, pathlib, sys
os.umask(0o077)
state=json.load(sys.stdin)
assert all(c['domain'].lstrip('.') == 'courselink.uoguelph.ca' for c in state['cookies'])
assert all(o['origin'] == 'https://courselink.uoguelph.ca' for o in state.get('origins', []))
target=pathlib.Path(sys.argv[1]) / 'browser-auth.json'
temp=target.with_suffix('.tmp')
temp.write_text(json.dumps(state))
temp.chmod(0o600)
temp.replace(target)
'''
            ssh = ['ssh', '-T', '-o', 'BatchMode=yes', args.host]
            service = shlex.quote(args.service)
            subprocess.run(ssh + ['systemctl --user stop ' + service], check=True)
            try:
                command = 'python3 -c ' + shlex.quote(script) + ' ' + shlex.quote(args.state_dir)
                subprocess.run(ssh + [command], input=session, check=True)
            finally:
                subprocess.run(ssh + ['systemctl --user start ' + service], check=True)
    finally:
        if previous_state is None:
            os.environ.pop('COURSELINK_STATE_DIR', None)
        else:
            os.environ['COURSELINK_STATE_DIR'] = previous_state
    print('Remote session refreshed. The monitor will verify it and resume scanning.')


if __name__ == '__main__':
    main()
