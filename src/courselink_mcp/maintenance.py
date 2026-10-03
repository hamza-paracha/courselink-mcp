"""Private, local maintenance jobs for the optional Linux systemd timers."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

import httpx

from .config import Config


def write_state(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(data))
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def recovery_due(record, healthy, timestamp):
    """Three failures, with at most one recovery attempt per ten minutes."""
    record['failures'] = 0 if healthy else min(record.get('failures', 0) + 1, 3)
    last = record.get('last_restart')
    if last is not None and last > timestamp:  # Recover from a wall-clock correction.
        record['last_restart'] = timestamp
        last = timestamp
    due = record['failures'] >= 3 and (last is None or timestamp - last >= 600)
    if due:
        record.update(failures=0, last_restart=timestamp)
    return due


def monitor_healthy(client, config):
    try:
        response = client.get(f'http://127.0.0.1:{config.port}/api/health',
                              headers={'Authorization': 'Bearer ' + config.token()})
        response.raise_for_status()
        return response.json().get('healthy') is True
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def tunnel_ready(client):
    try:
        return client.get('http://127.0.0.1:8080/readyz').status_code == 200
    except httpx.HTTPError:
        return False


def restart(unit):
    subprocess.run(['systemctl', '--user', 'restart', unit], check=True, timeout=90,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def watchdog(config, tunnel=False):
    path = config.state / 'watchdog.json'
    try:
        records = json.loads(path.read_text())
        if not isinstance(records, dict):
            records = {}
    except (FileNotFoundError, ValueError):
        records = {}
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        healthy = monitor_healthy(client, config)
        checks = {'courselink.service': healthy}
        # Give the backend time to recover before judging tunnel connectivity.
        if tunnel and healthy:
            checks['courselink-tunnel.service'] = tunnel_ready(client)
    actions = []
    timestamp = time.time()
    for unit, ok in checks.items():
        record = records.setdefault(unit, {})
        if recovery_due(record, ok, timestamp):
            actions.append(unit)
    # Persist before restarting: failed systemctl calls must not cause a restart storm.
    write_state(path, records)
    for unit in actions:
        print(f'Recovering {unit} after repeated health-check failures.', flush=True)
        restart(unit)


def backup_catalog(config, retain=7):
    if retain < 1:
        raise ValueError('Keep at least one backup.')
    source = config.state / 'catalog.sqlite3'
    if not source.is_file():
        raise FileNotFoundError('No catalog exists to back up.')
    directory = config.state / 'backups'
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    destination = directory / (datetime.now(timezone.utc).strftime('catalog-%Y-%m-%d') + '.sqlite3')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
        # SQLite's backup API includes committed WAL data without stopping the monitor.
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst, pages=256, sleep=0.1)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Catalog backup failed integrity verification.')
        temporary.replace(destination)
        for old in sorted(directory.glob('catalog-????-??-??.sqlite3'), reverse=True)[retain:]:
            old.unlink()
        return destination
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=['watchdog', 'backup'])
    parser.add_argument('--tunnel', action='store_true', help='Also check the tunnel client on localhost:8080.')
    args = parser.parse_args()
    config = Config.load()
    if args.job == 'watchdog':
        watchdog(config, tunnel=args.tunnel)
    else:
        backup_catalog(config)
        print('Catalog backup verified; retaining seven daily snapshots.')


if __name__ == '__main__':
    main()
