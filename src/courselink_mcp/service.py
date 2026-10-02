import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import tempfile
import json
import sys
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
        self.download_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.tasks = []
        self.ready = asyncio.Event()
        self.extract_lock = asyncio.Lock()

    async def start(self):
        self.tasks = [asyncio.create_task(self.session_loop()), asyncio.create_task(self.scan_loop()),
                      asyncio.create_task(self.index_loop())]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.browser.close()
        self.store.close()

    async def session_loop(self):
        while True:
            try:
                if self.browser.context is None or self.browser.closed:
                    if self.browser.context is not None:
                        await self.browser.close()
                    await self.browser.start()
                    self.ready.set()
                previous = self.browser.status.get('state')
                await self.browser.keepalive()
                if previous != 'authenticated' and self.browser.status['state'] == 'authenticated':
                    self.wake.set()
            except Exception as exc:
                log.warning('Browser health check failed: %s', type(exc).__name__)
                self.browser.status = {'state': 'connection_error', 'message': str(exc)[:300],
                                       'last_verified': self.browser.status.get('last_verified')}
            await asyncio.sleep(self.config.keepalive_seconds)

    async def scan_loop(self):
        await self.ready.wait()
        while True:
            self.wake.clear()
            if self.browser.status.get('state') == 'authenticated':
                try:
                    await self.scan()
                except Exception as exc:
                    log.warning('CourseLink scan failed: %s', type(exc).__name__)
                    self.store.set_state('last_error', {'time': now(), 'message': str(exc)[:300]})
            try:
                await asyncio.wait_for(self.wake.wait(), self.config.poll_seconds)
            except asyncio.TimeoutError:
                pass

    def status(self):
        return {'session': self.browser.status, 'scanning': self.scan_lock.locked(),
                'last_scan': self.store.get_state('last_scan'), 'last_error': self.store.get_state('last_error'),
                'poll_seconds': self.config.poll_seconds, 'file_check_seconds': self.config.file_check_seconds,
                'monitored_courses': self.config.courses, 'api_versions': self.browser.versions,
                'text_index': self.store.index_status(),
                'note': 'Cached results remain readable offline. The host running this service must be online for monitoring.'}

    def request_scan(self):
        self.wake.set()
        return {'queued': True, 'session': self.browser.status, 'already_scanning': self.scan_lock.locked()}

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
            try:
                for version in self.store.pending_documents():
                    await self.extract_document(version)
                    await asyncio.sleep(0.1)
            except Exception as exc:
                log.warning('Text index failed: %s', type(exc).__name__)
            await asyncio.sleep(5)

    async def scan(self):
        async with self.scan_lock:
            report = {'started_at': now(), 'finished_at': None, 'courses': {}, 'errors': [], 'files_saved': 0}
            try:
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
                    for kind, fetch in [('content', self.catalog.content), ('assignment', self.catalog.assignments),
                                        ('announcement', self.catalog.announcements), ('event', self.catalog.calendar),
                                        ('quiz', self.catalog.quizzes)]:
                        try:
                            rows = await fetch(course)
                            self.store.reconcile(course, kind, rows)
                            report['courses'][course][kind] = len(rows)
                            for row in rows:
                                if row.get('download_path') and self.store.needs_download(row, self.config.file_check_seconds):
                                    try:
                                        downloaded = await self.download(row['id'])
                                        report['files_saved'] += int(downloaded['new_version'])
                                    except (LoginRequired, RateLimited):
                                        raise
                                    except Exception as exc:
                                        report['errors'].append({'course': course, 'item': row['id'],
                                                                 'message': str(exc)[:250]})
                            await asyncio.sleep(0.2)
                        except (LoginRequired, RateLimited):
                            raise
                        except Exception as exc:
                            report['errors'].append({'course': course, 'section': kind, 'message': str(exc)[:250]})
                    try:
                        linked_count, saved = await self.scan_linked_files(course)
                        report['courses'][course]['linked_file'] = linked_count
                        report['files_saved'] += saved
                    except (LoginRequired, RateLimited):
                        raise
                    except Exception as exc:
                        report['errors'].append({'course': course, 'section': 'linked_file', 'message': str(exc)[:250]})
            except LoginRequired:
                self.browser.status = {'state': 'login_required', 'message': 'Please sign in again.'}
                raise
            finally:
                report['finished_at'] = now()
                report['complete'] = not report['errors'] and len(report['courses']) == len(self.config.courses)
                report['complete'] = report['complete'] and all(len(v) == 6 for v in report['courses'].values())
                self.store.set_state('last_scan', report)
            self.store.set_state('last_error', None)
            return report

    async def scan_linked_files(self, course):
        rows = self.store.db.execute('SELECT id FROM items WHERE course_id=? AND available=1 AND kind!=?',
                                     (course, 'linked_file')).fetchall()
        queue = [self.store.get(row[0]) for row in rows]
        found, visited, saved = {}, set(), 0
        while queue:
            parent = queue.pop(0)
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
                    raise ValueError('An HTML course page could not be downloaded; link coverage is incomplete.')
                documents.append(Path(versions[0]['path']).read_text(errors='replace'))
            for document in documents:
                for child in linked_files(parent, document, self.browser):
                    if child['id'] in found:
                        continue
                    found[child['id']] = child
                    # Reconcile only the full set at the end; incremental upserts must not hide other links.
                    self.store.upsert_link(child)
                    if self.store.needs_download(child, self.config.file_check_seconds):
                        result = await self.download(child['id'])
                        saved += int(result['new_version'])
                    queue.append(child)
        self.store.reconcile(course, 'linked_file', list(found.values()))
        return len(found), saved

    async def download(self, item_id):
        async with self.download_lock:
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
                async with httpx.AsyncClient(cookies=await self.browser.cookies(), timeout=60,
                                             follow_redirects=False, trust_env=False) as client:
                    for _ in range(6):
                        async with client.stream('GET', url, headers={'Cache-Control': 'no-cache'}) as response:
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
