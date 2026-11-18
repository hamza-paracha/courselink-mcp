"""Private, immutable ZIP mailbox for cloud-generated course work.

Archives are never executed or extracted by the monitor or laptop sync.
"""
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import zipfile


class Results:
    def __init__(self, config):
        self.config = config
        self.root = config.state / 'completed-work'
        if self.root.is_symlink():
            raise ValueError('Results directory must not be a symlink.')
        self.root.mkdir(mode=0o700, exist_ok=True)

    def context(self):
        path = self.config.state / 'automation-context.json'
        return {'context': json.loads(path.read_text()) if path.is_file() else None,
                'results': self.list()['results']}

    def list(self):
        return {'results': [json.loads(path.read_text()) for path in sorted(self.root.glob('*.json'))
                            if not path.is_symlink()]}

    def save(self, course_id, task, archive_base64, sha256, report, source_item_ids):
        if course_id not in self.config.courses:
            raise ValueError('Unknown monitored course.')
        if not re.fullmatch(r'(labs/lab[0-9]+|assignments/a[0-9]+)(-[a-z0-9-]+)?', task):
            raise ValueError('Task must be labs/labN or assignments/aN, with an optional suffix.')
        if not isinstance(report, str) or not 1 <= len(report) <= 20000:
            raise ValueError('A verification report of 1..20000 characters is required.')
        if (not source_item_ids or len(source_item_ids) > 100 or
                any(not isinstance(v, str) or not v.startswith(course_id + ':') for v in source_item_ids)):
            raise ValueError('Source items must belong to this course.')
        if not isinstance(archive_base64, str) or len(archive_base64) > 8 * 1024 * 1024:
            raise ValueError('Archive is too large (6 MiB decoded maximum).')
        try:
            data = base64.b64decode(archive_base64, validate=True)
        except ValueError as exc:
            raise ValueError('Invalid base64 archive.') from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            raise ValueError('Archive SHA-256 mismatch.')
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if not entries or len(entries) > 2000 or sum(e.file_size for e in entries) > 100 * 1024 * 1024:
                    raise ValueError('ZIP entry count or expanded size exceeds limits.')
                for entry in entries:
                    path = PurePosixPath(entry.filename)
                    if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError('Unsafe ZIP member.')
        except zipfile.BadZipFile as exc:
            raise ValueError('Invalid ZIP archive.') from exc
        result_id = hashlib.sha256((course_id + '/' + task + '/' + digest).encode()).hexdigest()
        metadata = {'id': result_id, 'course_id': course_id, 'task': task,
                    'sha256': digest, 'size': len(data), 'source_item_ids': source_item_ids,
                    'report': report, 'created_at': datetime.now(timezone.utc).isoformat()}
        for suffix, payload in (('.zip', data), ('.json', (json.dumps(metadata, indent=2) + '\n').encode())):
            path = self.root / (result_id + suffix)
            try:
                with path.open('xb') as handle:
                    path.chmod(0o600)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if path.is_symlink() or (suffix == '.zip' and path.read_bytes() != data):
                    raise ValueError('Existing result conflict.')
        return json.loads((self.root / (result_id + '.json')).read_text())

    def read(self, result_id, offset=0, length=262144):
        if not re.fullmatch('[0-9a-f]{64}', result_id):
            raise ValueError('Invalid result ID.')
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 262144:
            raise ValueError('Invalid transfer range.')
        path = self.root / (result_id + '.zip')
        if path.is_symlink() or not path.is_file():
            raise ValueError('Result unavailable.')
        with path.open('rb') as handle:
            handle.seek(offset)
            data = handle.read(length)
        return {'offset': offset, 'next_offset': offset + len(data),
                'eof': offset + len(data) >= path.stat().st_size,
                'base64': base64.b64encode(data).decode()}
