from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import sqlite3
from .extract import EXTRACTOR_VERSION


def now():
    return datetime.now(timezone.utc).isoformat()


def utc_deadline(value):
    """Normalize a timezone-aware ISO timestamp for exact SQLite comparisons."""
    if not isinstance(value, str):
        return None
    try:
        due = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if due.tzinfo is not None:
            return due.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, OverflowError):
        return None


def safe_name(name):
    name = re.sub(r'[^\w.() -]', '_', str(name)).strip(' .')[:140]
    return name or 'file'


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.create_function('utc_deadline', 1, utc_deadline, deterministic=True)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA recursive_triggers=ON')
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
            CREATE INDEX IF NOT EXISTS versions_item ON versions(item_id,id DESC);
            CREATE INDEX IF NOT EXISTS items_course_kind ON items(course_id,kind,available);
            CREATE INDEX IF NOT EXISTS changes_course ON changes(course_id,id);
        ''')

        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(versions)')}
        with self.db:
            for column in ('etag', 'last_modified', 'validator_url'):
                if column not in columns:
                    self.db.execute(f'ALTER TABLE versions ADD COLUMN {column} TEXT')

        self.fts = False
        try:
            with self.db:
                for table, key, fields in [('documents', 'version_id', ['text']),
                                            ('items', 'rowid', ['title', 'data'])]:
                    index = table + '_fts'
                    exists = self.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (index,)).fetchone()
                    columns = ','.join(fields)
                    self.db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {index} USING fts5("
                                    f"{columns},content='{table}',content_rowid='{key}',tokenize='trigram')")
                    old = ','.join('old.' + field for field in fields)
                    new = ','.join('new.' + field for field in fields)
                    # executescript commits implicitly; individual trigger statements
                    # keep creation and the initial rebuild in the same transaction.
                    self.db.execute(f"""CREATE TRIGGER IF NOT EXISTS {index}_insert AFTER INSERT ON {table}
                        BEGIN INSERT INTO {index}(rowid,{columns}) VALUES(new.{key},{new}); END""")
                    self.db.execute(f"""CREATE TRIGGER IF NOT EXISTS {index}_delete AFTER DELETE ON {table}
                        BEGIN INSERT INTO {index}({index},rowid,{columns}) VALUES('delete',old.{key},{old}); END""")
                    self.db.execute(f"""CREATE TRIGGER IF NOT EXISTS {index}_update AFTER UPDATE ON {table}
                        BEGIN INSERT INTO {index}({index},rowid,{columns}) VALUES('delete',old.{key},{old});
                        INSERT INTO {index}(rowid,{columns}) VALUES(new.{key},{new}); END""")
                    if not exists:
                        self.db.execute(f"INSERT INTO {index}({index}) VALUES('rebuild')")
            self.fts = True
        except sqlite3.OperationalError:
            # FTS5/trigram is optional in system SQLite builds.
            pass

    def fts_query(self, query):
        if self.fts and len(query) >= 3 and query.isascii() and '\x00' not in query:
            return '"' + query.replace('"', '""') + '"'
        return None

    def latest_versions(self, course_id):
        return {row['item_id']: dict(row) for row in self.db.execute(
            """SELECT v.* FROM versions v JOIN items i ON i.id=v.item_id
               WHERE i.course_id=? AND i.available=1
               AND v.id=(SELECT max(id) FROM versions WHERE item_id=i.id)""", (course_id,))}

    def file_validators(self, version_id, etag, last_modified, url):
        with self.db:
            self.db.execute('UPDATE versions SET etag=?,last_modified=?,validator_url=? WHERE id=?',
                            (etag, last_modified, url, version_id))

    def close(self):
        self.db.close()

    def set_state(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES(?, ?)', (key, canonical(value)))

    def get_state(self, key, default=None):
        row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def grades(self, course_id):
        return self.get_state('grades:' + str(course_id))

    def save_grades(self, course_id, snapshot):
        self.set_state('grades:' + str(course_id), snapshot)

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

    def reconcile(self, course_id, kind, items, *, mark_missing=True):
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
            if mark_missing:
                for old in self.db.execute('SELECT * FROM items WHERE course_id=? AND kind=? AND available=1',
                                           (course_id, kind)).fetchall():
                    if old['id'] not in seen:
                        self.db.execute('UPDATE items SET available=0 WHERE id=?', (old['id'],))
                        self.event(json.loads(old['data']), 'no_longer_visible', {'title': old['title']})

    def get(self, item_id):
        row = self.db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if not row:
            raise ValueError('Unknown item ID. Use list_items first.')
        return self.item_data(row)

    @staticmethod
    def item_data(row):
        return {**json.loads(row['data']), 'available': bool(row['available']),
                'first_seen': row['first_seen'], 'last_seen': row['last_seen']}

    def upsert_link(self, item):
        self.reconcile(item['course_id'], 'linked_file', [item], mark_missing=False)

    def items(self, course_id=None, kind=None, query='', limit=100, offset=0, category=None):
        clauses, params = ['available=1'], []
        for field, value in [('course_id', course_id), ('kind', kind)]:
            if value:
                clauses.append(field + '=?')
                params.append(value)
        if query:
            fts_query = self.fts_query(query)
            if fts_query:
                clauses.append('rowid IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)')
                params.append(fts_query)
            clauses.append('(instr(lower(title),lower(?))>0 OR instr(lower(data),lower(?))>0)')
            params.extend([query, query])
        if category:
            clauses.append("json_extract(data,'$.category')=?")
            params.append(category)
        where = ' AND '.join(clauses)
        total = self.db.execute('SELECT count(*) FROM items WHERE ' + where, params).fetchone()[0]
        rows = self.db.execute('SELECT data,available,first_seen,last_seen FROM items WHERE ' + where + ' ORDER BY course_id,kind,title,id LIMIT ? OFFSET ?',
                              params + [min(max(limit, 1), 500), max(0, offset)]).fetchall()
        return {'total': total, 'items': [self.item_data(row) for row in rows], 'offset': max(0, offset)}

    def deadlines(self, start, end):
        rows = self.db.execute("""
            WITH candidates AS (
                SELECT i.id, i.course_id, i.kind, i.title,
                       json_extract(i.data, '$.due_date') AS due_date,
                       json_extract(i.data, '$.url') AS url
                FROM items i
                WHERE i.available=1
                  AND coalesce(json_extract(i.data, '$.parent_id'), '') IN ('', 0)
                  AND json_type(i.data, '$.due_date')='text'
            )
            SELECT i.*, json_extract(c.data, '$.name') AS course_name
            FROM candidates i LEFT JOIN courses c ON c.id=i.course_id
            WHERE utc_deadline(i.due_date) BETWEEN ? AND ?
            ORDER BY utc_deadline(i.due_date), i.id
        """, (start.isoformat(timespec='microseconds'), end.isoformat(timespec='microseconds')))
        return [dict(row) for row in rows]

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
        fts_query = self.fts_query(query)
        if fts_query:
            sql += ' AND d.version_id IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)'
            params.append(fts_query)
            sql += ' ORDER BY (SELECT rank FROM documents_fts WHERE documents_fts MATCH ? AND rowid=d.version_id),i.course_id,i.title LIMIT ?'
            params.append(fts_query)
        else:
            sql += ' ORDER BY i.course_id,i.title LIMIT ?'
        params.append(min(max(limit, 1), 100))
        results = []
        for row in self.db.execute(sql, params):
            text = row['text']
            position = text.lower().find(query.lower())
            results.append({k: row[k] for k in ('id', 'title', 'course_id', 'version_id')} |
                           {'snippet': text[max(position-150, 0):position+450]})
        coverage_sql = '''SELECT count(*) AS total,
            coalesce(sum(CASE WHEN d.version_id IS NOT NULL THEN 1 ELSE 0 END),0) AS processed
            FROM versions v JOIN items i ON i.id=v.item_id LEFT JOIN documents d ON d.version_id=v.id
            WHERE i.available=1 AND v.id=(SELECT max(id) FROM versions WHERE item_id=i.id)'''
        coverage_params = []
        if course_id:
            coverage_sql += ' AND i.course_id=?'
            coverage_params.append(course_id)
        coverage = self.db.execute(coverage_sql, coverage_params).fetchone()
        return {'matches': results, 'limit': min(max(limit, 1), 100), 'index_coverage': dict(coverage),
                'note': 'Search covers extracted text only; inspect read_document warnings and original files for images/unsupported formats.'}

    def changes(self, after=0, limit=100, course_id=None):
        sql, params = 'SELECT * FROM changes WHERE id>?', [max(0, after)]
        if course_id:
            sql += ' AND course_id=?'
            params.append(course_id)
        limit = min(max(limit, 1), 500)
        rows = self.db.execute(sql + ' ORDER BY id LIMIT ?', params + [limit + 1]).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        results = [{**dict(r), 'details': json.loads(r['details'])} for r in rows]
        return {'changes': results, 'next_cursor': rows[-1]['id'] if rows else after,
                'has_more': has_more}

    def versions(self, item_id):
        return [dict(r) for r in self.db.execute('SELECT * FROM versions WHERE item_id=? ORDER BY id DESC', (item_id,))]

    def latest_version(self, item_id):
        row = self.db.execute('SELECT * FROM versions WHERE item_id=? ORDER BY id DESC LIMIT 1', (item_id,)).fetchone()
        return dict(row) if row else None

    def record_file(self, item, temporary, destination, digest, size):
        latest = self.latest_version(item['id'])
        if latest and latest['sha256'] == digest and Path(latest['path']).is_file():
            temporary.unlink()
            return latest, False
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
        version = self.latest_version(item['id'])
        if not row or not row['checked_at'] or row['fingerprint'] != row['checked_fingerprint'] or not version:
            return True
        if not Path(version['path']).is_file():
            return True
        return (datetime.now(timezone.utc) - datetime.fromisoformat(row['checked_at'])).total_seconds() >= seconds
