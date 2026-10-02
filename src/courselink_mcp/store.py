from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import sqlite3
from .extract import EXTRACTOR_VERSION


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_name(name):
    name = re.sub(r'[^\w.() -]', '_', str(name)).strip(' .')[:140]
    return name or 'file'


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS courses(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items(
                id TEXT PRIMARY KEY, course_id TEXT NOT NULL, kind TEXT NOT NULL,
                title TEXT NOT NULL, data TEXT NOT NULL, fingerprint TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS changes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                item_id TEXT NOT NULL, course_id TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                path TEXT NOT NULL, size INTEGER NOT NULL, downloaded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS file_checks(
                item_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(
                version_id INTEGER PRIMARY KEY, result TEXT NOT NULL, text TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def set_state(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES(?, ?)', (key, canonical(value)))

    def get_state(self, key, default=None):
        row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def save_courses(self, courses):
        with self.db:
            for course in courses:
                self.db.execute('INSERT OR REPLACE INTO courses VALUES(?, ?)',
                                (str(course['id']), canonical(course)))

    def courses(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM courses ORDER BY id')]

    def event(self, item, event, details):
        self.db.execute('INSERT INTO changes(time,item_id,course_id,event,details) VALUES(?,?,?,?,?)',
                        (now(), item['id'], item['course_id'], event, canonical(details)))

    def reconcile(self, course_id, kind, items):
        # Only call after the entire endpoint, including every page, succeeds.
        seen = set()
        with self.db:
            for item in items:
                seen.add(item['id'])
                data = canonical(item)
                fingerprint = hashlib.sha256(data.encode()).hexdigest()
                old = self.db.execute('SELECT * FROM items WHERE id=?', (item['id'],)).fetchone()
                stamp = now()
                self.db.execute('''INSERT INTO items VALUES(?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,data=excluded.data,
                    fingerprint=excluded.fingerprint,last_seen=excluded.last_seen,available=1''',
                    (item['id'], course_id, kind, item['title'], data, fingerprint,
                     old['first_seen'] if old else stamp, stamp))
                if not old:
                    self.event(item, 'discovered', {'title': item['title']})
                elif not old['available']:
                    self.event(item, 'available_again', {'title': item['title']})
                elif old['fingerprint'] != fingerprint:
                    before = json.loads(old['data'])
                    fields = sorted(k for k in set(before) | set(item) if before.get(k) != item.get(k))
                    self.event(item, 'updated', {'title': item['title'], 'changed_fields': fields})
            for old in self.db.execute('SELECT * FROM items WHERE course_id=? AND kind=? AND available=1',
                                       (course_id, kind)).fetchall():
                if old['id'] not in seen:
                    self.db.execute('UPDATE items SET available=0 WHERE id=?', (old['id'],))
                    self.event(json.loads(old['data']), 'no_longer_visible', {'title': old['title']})

    def get(self, item_id):
        row = self.db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if not row:
            raise ValueError('Unknown item ID. Use list_items first.')
        return {**json.loads(row['data']), 'available': bool(row['available']),
                'first_seen': row['first_seen'], 'last_seen': row['last_seen']}

    def upsert_link(self, item):
        existing = [json.loads(r[0]) for r in self.db.execute(
            'SELECT data FROM items WHERE course_id=? AND kind=? AND available=1 AND id!=?',
            (item['course_id'], 'linked_file', item['id']))]
        self.reconcile(item['course_id'], 'linked_file', existing + [item])

    def items(self, course_id=None, kind=None, query='', limit=100, offset=0, category=None):
        clauses, params = ['available=1'], []
        for field, value in [('course_id', course_id), ('kind', kind)]:
            if value:
                clauses.append(field + '=?')
                params.append(value)
        if query:
            clauses.append('(instr(lower(title),lower(?))>0 OR instr(lower(data),lower(?))>0)')
            params.extend([query, query])
        if category:
            clauses.append("json_extract(data,'$.category')=?")
            params.append(category)
        where = ' AND '.join(clauses)
        total = self.db.execute('SELECT count(*) FROM items WHERE ' + where, params).fetchone()[0]
        rows = self.db.execute('SELECT id FROM items WHERE ' + where + ' ORDER BY course_id,kind,title LIMIT ? OFFSET ?',
                              params + [min(max(limit, 1), 500), max(0, offset)]).fetchall()
        return {'total': total, 'items': [self.get(r[0]) for r in rows], 'offset': max(0, offset)}

    def document(self, version_id):
        row = self.db.execute('SELECT result FROM documents WHERE version_id=?', (version_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_document(self, version_id, result):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?)',
                            (version_id, canonical(result), result.get('text', '')))

    def pending_documents(self):
        return [dict(row) for row in self.db.execute('''SELECT v.* FROM versions v
            JOIN items i ON i.id=v.item_id LEFT JOIN documents d ON d.version_id=v.id
            WHERE i.available=1 AND (d.version_id IS NULL OR coalesce(json_extract(d.result,'$.extractor_version'),0)!=?)
            AND v.id=(SELECT max(id) FROM versions WHERE item_id=v.item_id)
            ORDER BY CASE WHEN json_extract(i.data,'$.category') IN ('lab','assignment') THEN 0 ELSE 1 END,v.id LIMIT 5''', (EXTRACTOR_VERSION,))]

    def index_status(self):
        rows = self.db.execute('''SELECT coalesce(json_extract(d.result,'$.status'),'pending') AS status,count(*) AS count
            FROM versions v JOIN items i ON i.id=v.item_id LEFT JOIN documents d ON d.version_id=v.id
            WHERE i.available=1 AND v.id=(SELECT max(id) FROM versions WHERE item_id=i.id)
            GROUP BY status''').fetchall()
        return {row['status']: row['count'] for row in rows}

    def search_documents(self, query, course_id=None, limit=30):
        if not query.strip():
            raise ValueError('A nonempty search query is required.')
        sql = '''SELECT i.id,i.title,i.course_id,d.text,v.id AS version_id FROM documents d
            JOIN versions v ON v.id=d.version_id JOIN items i ON i.id=v.item_id
            WHERE i.available=1 AND v.id=(SELECT max(id) FROM versions WHERE item_id=i.id)
            AND instr(lower(d.text),lower(?))>0'''
        params = [query]
        if course_id:
            sql += ' AND i.course_id=?'
            params.append(course_id)
        sql += ' ORDER BY i.course_id,i.title LIMIT ?'
        params.append(min(max(limit, 1), 100))
        results = []
        for row in self.db.execute(sql, params):
            text = row['text']
            position = text.lower().find(query.lower())
            results.append({k: row[k] for k in ('id', 'title', 'course_id', 'version_id')} |
                           {'snippet': text[max(position-150, 0):position+450]})
        coverage = self.db.execute('''SELECT count(*) AS total,
            sum(CASE WHEN d.version_id IS NOT NULL THEN 1 ELSE 0 END) AS processed
            FROM versions v JOIN items i ON i.id=v.item_id LEFT JOIN documents d ON d.version_id=v.id
            WHERE i.available=1 AND v.id=(SELECT max(id) FROM versions WHERE item_id=i.id)''').fetchone()
        return {'matches': results, 'limit': min(max(limit, 1), 100), 'index_coverage': dict(coverage),
                'note': 'Search covers extracted text only; inspect read_document warnings and original files for images/unsupported formats.'}

    def changes(self, after=0, limit=100, course_id=None):
        sql, params = 'SELECT * FROM changes WHERE id>?', [max(0, after)]
        if course_id:
            sql += ' AND course_id=?'
            params.append(course_id)
        rows = self.db.execute(sql + ' ORDER BY id LIMIT ?', params + [min(max(limit, 1), 500)]).fetchall()
        results = [{**dict(r), 'details': json.loads(r['details'])} for r in rows]
        return {'changes': results, 'next_cursor': rows[-1]['id'] if rows else after}

    def versions(self, item_id):
        return [dict(r) for r in self.db.execute('SELECT * FROM versions WHERE item_id=? ORDER BY id DESC', (item_id,))]

    def record_file(self, item, temporary, destination, digest, size):
        latest = self.versions(item['id'])
        if latest and latest[0]['sha256'] == digest and Path(latest[0]['path']).is_file():
            temporary.unlink()
            return latest[0], False
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.replace(destination)
        with self.db:
            cursor = self.db.execute('INSERT INTO versions(item_id,sha256,path,size,downloaded_at) VALUES(?,?,?,?,?)',
                                    (item['id'], digest, str(destination), size, now()))
            self.event(item, 'file_updated' if latest else 'file_downloaded',
                       {'title': item['title'], 'sha256': digest, 'path': str(destination), 'size': size})
            version_id = cursor.lastrowid
        return dict(self.db.execute('SELECT * FROM versions WHERE id=?', (version_id,)).fetchone()), True

    def file_checked(self, item):
        row = self.db.execute('SELECT fingerprint FROM items WHERE id=?', (item['id'],)).fetchone()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO file_checks VALUES(?,?,?)', (item['id'], now(), row[0]))

    def needs_download(self, item, seconds):
        row = self.db.execute('''SELECT f.checked_at, f.fingerprint AS checked_fingerprint, i.fingerprint
            FROM items i LEFT JOIN file_checks f ON f.item_id=i.id WHERE i.id=?''', (item['id'],)).fetchone()
        versions = self.versions(item['id'])
        if not row or not row['checked_at'] or row['fingerprint'] != row['checked_fingerprint'] or not versions:
            return True
        if not Path(versions[0]['path']).is_file():
            return True
        return (datetime.now(timezone.utc) - datetime.fromisoformat(row['checked_at'])).total_seconds() >= seconds
