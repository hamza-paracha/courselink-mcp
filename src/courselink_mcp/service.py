import asyncio
from collections import deque
from weakref import WeakValueDictionary
from contextlib import suppress
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
import tempfile
import json
import sys
import time
from urllib.parse import unquote, urljoin

import httpx

from .browser import BrowserSession, LoginRequired, RateLimited
from .catalog import Catalog, linked_files
from .store import Store, now, safe_name
from .extract import EXTRACTOR_VERSION

log = logging.getLogger(__name__)


class Service:
    def __init__(self, config):
        self.config = config
        self.store = Store(config.state / 'catalog.sqlite3')
        self.browser = BrowserSession(config)
        self.catalog = Catalog(self.browser)
        self.scan_lock = asyncio.Lock()
        self.download_locks = WeakValueDictionary()
        self.download_slots = asyncio.Semaphore(config.download_concurrency)
        self.active_downloads = 0
        self.download_client = None
        self.wake = asyncio.Event()
        self.tasks = []
        self.ready = asyncio.Event()
        self.extract_lock = asyncio.Lock()
        self.heartbeats = {}
        self.verify_files_requested = False

    async def start(self):
        self.heartbeats = {name: time.monotonic() for name in ('session', 'scan', 'index')}
        self.tasks = [asyncio.create_task(self.session_loop()), asyncio.create_task(self.scan_loop()),
                      asyncio.create_task(self.index_loop())]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        try:
            await self.browser.close()
        finally:
            try:
                if self.download_client:
                    await self.download_client.aclose()
            finally:
                self.store.close()

    async def session_loop(self):
        while True:
            try:
                if self.browser.context is None or self.browser.closed:
                    if self.browser.playwright is not None:
                        await asyncio.wait_for(self.browser.close(), 30)
                    await asyncio.wait_for(self.browser.start(), 60)
                    self.ready.set()
                previous = self.browser.status.get('state')
                await asyncio.wait_for(self.browser.keepalive(), 180)
                if previous != 'authenticated' and self.browser.status['state'] == 'authenticated':
                    self.wake.set()
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    self.browser.closed = True
                log.warning('Browser health check failed: %s', type(exc).__name__)
                self.browser.status = {'state': 'connection_error', 'message': str(exc)[:300],
                                       'last_verified': self.browser.status.get('last_verified')}
            self.heartbeats['session'] = time.monotonic()
            await asyncio.sleep(self.config.keepalive_seconds)

    async def scan_loop(self):
        await self.ready.wait()
        while True:
            self.wake.clear()
            if self.browser.status.get('state') == 'authenticated':
                try:
                    verify_files = self.verify_files_requested
                    self.verify_files_requested = False
                    await asyncio.wait_for(self.scan(verify_files=verify_files), 1800)
                except Exception as exc:
                    log.warning('CourseLink scan failed: %s', type(exc).__name__)
                    self.store.set_state('last_error', {'time': now(), 'message': str(exc)[:300]})
            self.heartbeats['scan'] = time.monotonic()
            try:
                await asyncio.wait_for(self.wake.wait(), self.config.poll_seconds)
            except asyncio.TimeoutError:
                pass

    def health(self):
        # Login expiry and upstream outages do not make the local process unhealthy.
        # Detect dead workers and missed progress, rather than restarting for MFA.
        limits = {'session': self.config.keepalive_seconds + 300,
                  'scan': self.config.poll_seconds + 1860, 'index': 600}
        current = time.monotonic()
        stalled = [name for name, limit in limits.items()
                   if current - self.heartbeats.get(name, 0) > limit]
        workers_alive = len(self.tasks) == 3 and all(not task.done() for task in self.tasks)
        return {'healthy': workers_alive and not stalled, 'workers_alive': workers_alive,
                'stalled_workers': stalled}

    def status(self):
        return {'health': self.health(), 'session': self.browser.status, 'scanning': self.scan_lock.locked(),
                'last_scan': self.store.get_state('last_scan'), 'last_error': self.store.get_state('last_error'),
                'poll_seconds': self.config.poll_seconds, 'file_check_seconds': self.config.file_check_seconds,
                'downloads': {'active': self.active_downloads, 'concurrency': self.config.download_concurrency},
                'monitored_courses': self.config.courses, 'api_versions': self.browser.versions,
                'text_index': self.store.index_status(),
                'coverage_scope': 'Accessible content, assignments, announcements, calendar and quiz metadata '
                    'for selected courses, plus linked files under CourseLink /content/ and /shared/. '
                    'Hidden/locked material, external services and quiz questions are outside this scope. '
                    'A complete scan does not guarantee every file is text-extractable; inspect text_index and document warnings.',
                'note': 'Cached results remain readable offline. The host running this service must be online for monitoring.'}

    def request_scan(self, verify_files=False):
        if type(verify_files) is not bool:
            raise ValueError('verify_files must be true or false.')
        self.verify_files_requested = self.verify_files_requested or verify_files
        self.wake.set()
        return {'queued': True, 'verify_files': self.verify_files_requested,
                'session': self.browser.status, 'already_scanning': self.scan_lock.locked()}

    async def extract_document(self, version, member=None):
        async with self.extract_lock:
            cached = self.store.document(version['id']) if member is None else None
            if cached is not None and cached.get('extractor_version') == EXTRACTOR_VERSION:
                return cached
            args = [sys.executable, '-m', 'courselink_mcp.extract', version['path']]
            if member is not None:
                args.append(member)
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), 30)
                result = json.loads(out) if proc.returncode == 0 else {
                    'status': 'extraction_error', 'text': '', 'warning': 'Document extraction exceeded limits or failed.'}
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                result = {'status': 'extraction_timeout', 'text': '', 'warning': 'Retrieve the original file for inspection.'}
            except BaseException:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            if member is None:
                result['extractor_version'] = EXTRACTOR_VERSION
                self.store.save_document(version['id'], result)
            return result

    async def index_loop(self):
        while True:
            delay = 5
            try:
                self.heartbeats['index'] = time.monotonic()
                pending = self.store.pending_documents()
                for version in pending:
                    await self.extract_document(version)
                    self.heartbeats['index'] = time.monotonic()
                    await asyncio.sleep(0.1)
                if pending:
                    delay = 0  # Keep draining a backlog; idle polling still sleeps.
            except Exception as exc:
                log.warning('Text index failed: %s', type(exc).__name__)
            await asyncio.sleep(delay)

    async def scan(self, verify_files=False):
        async with self.scan_lock:
            started = time.monotonic()
            report = {'started_at': now(), 'finished_at': None, 'courses': {}, 'errors': [], 'files_saved': 0}
            report['verify_files'] = verify_files
            try:
                settings_path = self.config.state / 'config.json'
                if settings_path.exists():
                    settings = json.loads(settings_path.read_text())
                    selected = replace(self.config, courses=settings.get('courses', self.config.courses)).validate()
                    self.config.courses = selected.courses
                courses = await self.catalog.courses()
                for course in courses:
                    course['monitored'] = course['id'] in self.config.courses
                    course['folder'] = self.config.courses.get(course['id'])
                self.store.save_courses(courses)
                enrolled = {course['id'] for course in courses}
                for course in self.config.courses:
                    if course not in enrolled:
                        report['errors'].append({'course': course, 'section': 'enrollment',
                                                 'message': 'Configured course not present in current enrollments.'})
                        continue
                    report['courses'][course] = {}
                    pending = []
                    for kind, fetch in [('content', self.catalog.content), ('assignment', self.catalog.assignments),
                                        ('announcement', self.catalog.announcements), ('event', self.catalog.calendar),
                                        ('quiz', self.catalog.quizzes)]:
                        try:
                            rows = await fetch(course)
                            self.store.reconcile(course, kind, rows)
                            report['courses'][course][kind] = len(rows)
                            pending.extend(rows)
                            await asyncio.sleep(0.2)
                        except (LoginRequired, RateLimited):
                            raise
                        except Exception as exc:
                            report['errors'].append({'course': course, 'section': kind, 'message': str(exc)[:250]})
                    await self.download_rows(course, pending, report, verify_files=verify_files)
                    try:
                        linked_count, saved, errors = await self.scan_linked_files(course, verify_files=verify_files)
                        report['courses'][course]['linked_file'] = linked_count
                        report['files_saved'] += saved
                        report['errors'].extend(errors)
                    except (LoginRequired, RateLimited):
                        raise
                    except Exception as exc:
                        report['errors'].append({'course': course, 'section': 'linked_file', 'message': str(exc)[:250]})
            except asyncio.CancelledError:
                report['errors'].append({'section': 'scan', 'message': 'Scan interrupted or exceeded its time limit.'})
                raise
            except LoginRequired:
                self.browser.status = {'state': 'login_required', 'message': 'Please sign in again.'}
                report['errors'].append({'section': 'session', 'message': 'Sign in again.'})
                raise
            except Exception as exc:
                report['errors'].append({'section': 'scan', 'message': str(exc)[:250]})
                raise
            finally:
                report['duration_seconds'] = round(time.monotonic() - started, 3)
                report['finished_at'] = now()
                report['complete'] = not report['errors'] and len(report['courses']) == len(self.config.courses)
                report['complete'] = report['complete'] and all(len(v) == 6 for v in report['courses'].values())
                self.store.set_state('last_scan', report)
            self.store.set_state('last_error', None)
            return report

    async def scan_linked_files(self, course, verify_files=False):
        rows = self.store.db.execute('SELECT id FROM items WHERE course_id=? AND available=1 AND kind!=?',
                                     (course, 'linked_file')).fetchall()
        queue = deque(self.store.get(row[0]) for row in rows)
        found, visited, saved = {}, set(), 0
        errors = []
        while queue:
            parent = queue.popleft()
            if parent['id'] in visited:
                continue
            visited.add(parent['id'])
            if len(visited) > 2000:
                raise ValueError('Too many linked course pages; leaving the previous link index intact.')
            documents = []
            for field in ('body', 'instructions'):
                raw = parent.get(field)
                if isinstance(raw, dict) and raw.get('Html'):
                    documents.append(raw['Html'])
            # Link attachments can point at a file without being in the formal attachment list.
            if parent.get('links'):
                from html import escape
                documents.append(''.join('<a href="' + escape(link.get('Href', ''), quote=True) + '">file</a>'
                                         for link in parent['links']))
            if (parent.get('filename') or '').lower().endswith(('.html', '.htm')):
                versions = self.store.versions(parent['id'])
                if not versions:
                    errors.append({'course': course, 'item': parent['id'], 'section': 'linked_file',
                                   'message': 'HTML page unavailable; link coverage is incomplete.'})
                else:
                    try:
                        documents.append(Path(versions[0]['path']).read_text(errors='replace'))
                    except OSError as exc:
                        errors.append({'course': course, 'item': parent['id'], 'section': 'linked_file',
                                       'message': type(exc).__name__})
            for document in documents:
                for child in linked_files(parent, document, self.browser):
                    if child['id'] in found:
                        continue
                    found[child['id']] = child
                    # Reconcile only the full set at the end; incremental upserts must not hide other links.
                    self.store.upsert_link(child)
                    try:
                        if verify_files or self.store.needs_download(child, self.config.file_check_seconds):
                            result = await self.download(child['id'])
                            saved += int(result['new_version'])
                    except (LoginRequired, RateLimited):
                        raise
                    except Exception as exc:
                        errors.append({'course': course, 'item': child['id'], 'section': 'linked_file',
                                       'message': str(exc)[:250]})
                    queue.append(child)
        self.store.reconcile(course, 'linked_file', list(found.values()), mark_missing=not errors)
        return len(found), saved, errors

    async def download_rows(self, course, rows, report, verify_files=False):
        pending = iter(row for row in rows if row.get('download_path')
                       and (verify_files or self.store.needs_download(row, self.config.file_check_seconds)))

        async def worker():
            for row in pending:
                try:
                    result = await self.download(row['id'])
                    report['files_saved'] += int(result['new_version'])
                except (LoginRequired, RateLimited):
                    raise
                except Exception as exc:
                    report['errors'].append({'course': course, 'item': row['id'], 'message': str(exc)[:250]})

        tasks = [asyncio.create_task(worker()) for _ in range(self.config.download_concurrency)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def download(self, item_id):
        lock = self.download_locks.setdefault(item_id, asyncio.Lock())
        async with lock, self.download_slots:
            self.active_downloads += 1
            try:
                return await self._download(item_id)
            finally:
                self.active_downloads -= 1

    async def _download(self, item_id):
        row = self.store.get(item_id)
        if not row['available'] or not row.get('download_path'):
            raise ValueError('This item does not have a currently available downloadable file.')
        if self.browser.status.get('state') != 'authenticated':
            raise LoginRequired('Sign in before downloading. Cached versions remain available.')
        course = row['course_id']
        folder = self.config.courses.get(course)
        if not folder:
            raise ValueError('This course is not enabled for downloads.')
        root = (self.config.school / folder / 'CourseLink Downloads').resolve()
        expected = (self.config.school / folder).resolve()
        if not root.is_relative_to(self.config.school) or not root.is_relative_to(expected):
            raise ValueError('Download directory escapes the school folder.')
        item_folder = root / safe_name(row['kind']) / safe_name(row['id'])
        item_folder.mkdir(parents=True, exist_ok=True)
        if not item_folder.resolve().is_relative_to(root):
            raise ValueError('Unsafe download directory.')
        filename = safe_name(unquote(row.get('filename') or row['title']))
        url = self.browser.url(row['download_path'])
        temp = None
        try:
            if self.download_client is None:
                self.download_client = httpx.AsyncClient(timeout=60, follow_redirects=False,
                    trust_env=False, limits=httpx.Limits(max_connections=self.config.download_concurrency,
                        max_keepalive_connections=self.config.download_concurrency))
            client = self.download_client
            for _ in range(6):
                cookies = await self.browser.cookies()
                cookie_header = httpx.Request('GET', url, cookies=cookies).headers.get('cookie', '')
                async with client.stream('GET', url, headers={'Cache-Control': 'no-cache', 'Cookie': cookie_header}) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        redirect = urljoin(url, response.headers.get('location', ''))
                        try:
                            url = self.browser.url(redirect)
                        except ValueError:
                            raise LoginRequired('Download redirected outside CourseLink; sign in again.')
                        if any(part in url.lower() for part in ('/login', '/auth/', '/saml')):
                            raise LoginRequired('Download requires sign-in.')
                        continue
                    if response.status_code == 401:
                        raise LoginRequired('Download requires sign-in.')
                    if response.status_code == 429:
                        raise RateLimited('Download rate-limited; will retry on the next scan.')
                    response.raise_for_status()
                    size_header = response.headers.get('content-length')
                    if size_header and int(size_header) > self.config.max_file_bytes:
                        raise ValueError('File exceeds the configured 100 MiB limit.')
                    digest, size, beginning = hashlib.sha256(), 0, b''
                    with tempfile.NamedTemporaryFile(dir=item_folder, suffix='.partial', delete=False) as handle:
                        temp = Path(handle.name)
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.config.max_file_bytes:
                                raise ValueError('File exceeds the configured size limit.')
                            if len(beginning) < 8192:
                                beginning += chunk[:8192-len(beginning)]
                            digest.update(chunk)
                            handle.write(chunk)
                    content_type = response.headers.get('content-type', '').lower()
                    if 'text/html' in content_type and not filename.lower().endswith(('.html', '.htm')):
                        raise LoginRequired('Received an HTML page instead of the requested file.')
                    if b'login.microsoftonline.com' in beginning or b'name="password"' in beginning.lower():
                        raise LoginRequired('Received a sign-in page instead of a course file.')
                    hexdigest = digest.hexdigest()
                    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                    destination = item_folder / f'{stamp}-{hexdigest[:12]}-{filename}'
                    version, changed = self.store.record_file(row, temp, destination, hexdigest, size)
                    self.store.file_checked(row)
                    return {'item_id': item_id, 'new_version': changed, 'version': version}
            raise RuntimeError('Too many redirects for a CourseLink file.')
        finally:
            if temp and temp.exists():
                temp.unlink()
