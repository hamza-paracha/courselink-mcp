import argparse
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import sys
import tempfile

import httpx
import uvicorn

from .config import Config


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='CourseLink monitor and MCP server')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('serve', help='Run the monitor, authenticated REST API and HTTP MCP server')
    sub.add_parser('stdio', help='MCP stdio bridge to the already-running service')
    sub.add_parser('computer-stdio', help='Local MCP bridge with downloads into local course folders')
    sync = sub.add_parser('sync', help='Sync remote files and course metadata onto this computer')
    sync.add_argument('--course', default=None)
    sub.add_parser('login', help='Sign in once before starting the service')
    sub.add_parser('status', help='Read service health')
    sub.add_parser('check', help='Queue a fresh scan')
    fetch = sub.add_parser('fetch', help='Download an item from the server to this machine')
    fetch.add_argument('item_id')
    fetch.add_argument('--output', type=Path, required=True)
    sub.add_parser('init', help='Create config and protected server token')
    args = parser.parse_args()
    config = Config.load()
    if args.command == 'computer-stdio':
        from .computer import computer_stdio
        asyncio.run(computer_stdio(config))
        return
    if args.command == 'sync':
        from .computer import sync_command
        try:
            report = asyncio.run(sync_command(config, args.course))
            print(json.dumps(report, indent=2))
            if not report['complete']:
                sys.exit(1)
        except Exception as exc:
            print('Computer sync failed: ' + str(exc), file=sys.stderr)
            sys.exit(1)
        return
    if args.command == 'init':
        config.token()
        print('Configuration: ' + str(config.state / 'config.json'))
        return
    if args.command == 'stdio':
        from .server import stdio
        stdio(config)
        return
    if args.command in ('serve', 'login'):
        # A profile can only have one owner; fail cleanly instead of corrupting sessions.
        lock = (config.state / 'service.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('CourseLink is already running. Use the open_login MCP tool instead.')
        if args.command == 'login':
            from .login import login
            asyncio.run(login())
        else:
            from .server import create_app
            logging.basicConfig(level=logging.INFO)
            uvicorn.run(create_app(config), host=os.environ.get('COURSELINK_BIND', '127.0.0.1'),
                        port=config.port, access_log=False)
        return
    base = os.environ.get('COURSELINK_SERVER_URL', f'http://127.0.0.1:{config.port}').rstrip('/')
    token = os.environ.get('COURSELINK_SERVER_TOKEN') or config.token()
    with httpx.Client(headers={'Authorization': 'Bearer ' + token}, timeout=180, trust_env=False) as client:
        if args.command in ('status', 'check'):
            operation = 'status' if args.command == 'status' else 'scan'
            response = client.post(base + '/api/' + operation, json={})
            response.raise_for_status()
            print(json.dumps(response.json(), indent=2))
        else:
            response = client.post(base + '/api/download', json={'item_id': args.item_id})
            response.raise_for_status()
            version = response.json()['version']
            if args.output.exists():
                sys.exit('Output already exists; choose a new filename to preserve it.')
            with client.stream('GET', base + f'/api/files/{version["id"]}') as download:
                download.raise_for_status()
                import hashlib
                digest = hashlib.sha256()
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=args.output.parent, prefix=args.output.name + '.', suffix='.partial', delete=False) as handle:
                        temporary = Path(handle.name)
                        for chunk in download.iter_bytes():
                            digest.update(chunk)
                            handle.write(chunk)
                    if digest.hexdigest() != version['sha256']:
                        raise ValueError('Downloaded file hash did not match.')
                    os.link(temporary, args.output)
                finally:
                    if temporary and temporary.exists():
                        temporary.unlink()
            print(str(args.output.resolve()))


if __name__ == '__main__':
    main()
