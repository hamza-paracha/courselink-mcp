"""Learner dropbox history and explicitly confirmed uploads. Never called by workers."""
import asyncio
import copy
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
import time
from zoneinfo import ZoneInfo

from .browser import LoginRequired
from .catalog import richtext
from .store import canonical, now


# Inherited by child tasks, so worker callbacks cannot acquire upload privileges.
background_monitor = ContextVar('courselink_background_monitor', default=False)


def require_interactive_submission():
    if background_monitor.get():
        raise ValueError('Background monitor tasks cannot submit coursework.')


def timestamp(value):
    if value is None:
        return None
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('CourseLink date has no timezone.')
    return result


def lateness(submitted, due):
    if not submitted or not due:
        return None
    try:
        return timestamp(submitted) > timestamp(due)
    except (ValueError, TypeError, AttributeError):
        return None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):
        raise ValueError('Expected a numeric course or folder ID.')
    return value


class HistoryNode:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def nodes(self, tag=None):
        for child in self.children:
            if isinstance(child, HistoryNode):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.nodes(tag)

    def text(self):
        return ''.join(c.text() if isinstance(c, HistoryNode) else c for c in self.children)


class HistoryParser(HTMLParser):
    """Inert HTML tree; never execute scripts or navigate links."""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.current = HistoryNode()

    def handle_starttag(self, tag, attrs):
        node = HistoryNode(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def history_datetime(display):
    try:
        local = datetime.strptime(display, '%b %d, %Y %I:%M %p')
        return local.replace(tzinfo=ZoneInfo('America/Toronto')).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None


def parse_history(html, folder_id, due):
    parser = HistoryParser()
    parser.feed(html)
    root = parser.root
    selected = [o.attrs.get('value') for select in root.nodes('select')
                if select.attrs.get('name') == 'dropbox' or select.attrs.get('id') == 'z_b'
                for o in select.nodes('option') if 'selected' in o.attrs]
    if len(selected) != 1 or selected[0] not in (folder_id, folder_id + '_0'):
        raise ValueError('History selected folder does not match the requested folder.')
    tables = [t for t in root.nodes('table') if t.attrs.get('id') == 'z_e']
    if len(tables) != 1:
        raise ValueError('History table is missing or unrecognized.')
    table = tables[0]
    # Live empty histories have no header row. Require explicit empty-state
    # evidence as well as the selected-folder check above and an empty table.
    empty_marker = any(
        node.attrs.get('description') == 'No files have been submitted to this dropbox folder'
        for container in root.nodes('div')
        if 'grid-empty-state-container' in container.attrs.get('class', '').split()
        for node in container.nodes('d2l-empty-state-simple'))
    empty_table = ('d_gempty' in table.attrs.get('class', '').split()
                   and table.attrs.get('summary') == 'List of file submissions for the selected dropbox folders')
    recognized_empty = (not list(table.nodes()) and not table.text().strip()
                        and (empty_marker or empty_table))
    if not recognized_empty and [' '.join(h.text().split()) for h in table.nodes('th')] != [
            'Submission ID', 'Submission(s)', 'Date Submitted']:
        raise ValueError('History table headers are unrecognized.')
    rows, errors = [], []
    for row in table.nodes('tr'):
        cells = [c for c in row.children if isinstance(c, HistoryNode) and c.tag == 'td']
        if not cells:
            continue
        if len(cells) != 3 or not re.fullmatch(r'[0-9]+', cells[0].text().strip()):
            errors.append('History contains an unrecognized submission row.')
            continue
        files = []
        for node in cells[1].nodes('span'):
            if 'dfl' not in node.attrs.get('class', '').split():
                continue
            links = list(node.nodes('a'))
            name = links[0].text().strip() if links else next(
                (c.text().strip() for c in node.children if isinstance(c, HistoryNode) and c.tag == 'span'), '')
            size = re.search(r'\(([0-9]+(?:\.[0-9]+)?\s*(?:B|KB|MB|GB))\)', node.text(), re.I)
            size_display = size[1] if size else None
            size_bytes = None
            if size:
                amount, unit = re.fullmatch(r'([0-9.]+)\s*(B|KB|MB|GB)', size_display, re.I).groups()
                size_bytes = round(float(amount) * 1024 ** ['B', 'KB', 'MB', 'GB'].index(unit.upper()))
            # Deleted markers can be siblings of the file span.
            siblings = node.parent.children
            suffix = ''
            for sibling in siblings[siblings.index(node) + 1:]:
                if isinstance(sibling, HistoryNode) and 'dfl' in sibling.attrs.get('class', '').split():
                    break
                suffix += sibling.text() if isinstance(sibling, HistoryNode) else sibling
            url = links[0].attrs.get('href') if links else None
            if url and not url.startswith('/d2l/'):
                url = None
            files.append({'name': name, 'size_display': size_display, 'size_bytes': size_bytes,
                          'deleted': '(file deleted)' in (node.text() + suffix).lower(), 'url': url})
        display = cells[2].text().strip()
        submitted = history_datetime(display)
        if not files or any(not f['name'] for f in files) or submitted is None:
            errors.append('History contains an unrecognized submission row or date.')
            continue
        flags = {n.attrs.get('alt', '').casefold() for n in cells[1].nodes()}
        read = True if 'read by user' in flags else False if 'unread by user' in flags else None
        late = lateness(submitted, due)
        sid = cells[0].text().strip()
        rows.append({'submission_id': sid, 'id': sid, 'submitted_at_display': display,
                     'submitted_at_utc': submitted, 'submitted_at': submitted, 'files': files,
                     'read_by_user': read, 'late': late, 'on_time': None if late is None else not late,
                     'timeliness': 'unknown' if late is None else 'late' if late else 'on_time'})
    # JS pagination is not safe to replay. Report partial coverage instead.
    for node in root.nodes():
        parent = node
        in_file = False
        while parent is not None:
            in_file = in_file or 'dfl' in parent.attrs.get('class', '').split()
            parent = parent.parent
        if in_file:
            continue
        attrs = node.attrs
        label = ' '.join(str(attrs.get(k) or '') for k in ('title', 'aria-label', 'alt', 'id', 'class', 'name', 'rel'))
        if node.tag in {'a', 'button', 'input', 'img', 'd2l-button', 'd2l-icon'}:
            label += ' ' + node.text() + ' ' + str(attrs.get('value') or '')
            disabled = ('disabled' in attrs or attrs.get('aria-disabled') == 'true'
                        or re.search(r'\bdisabled\b', attrs.get('class', ''), re.I))
            parent = node.parent
            while parent is not None:
                disabled = disabled or 'disabled' in parent.attrs or parent.attrs.get('aria-disabled') == 'true'
                parent = parent.parent
            if re.search(r'next|last\s*page', label, re.I) and not disabled:
                errors.append('History is paginated; additional pages may exist and were not read.')
        if node.tag == 'select' and re.search(r'page', label, re.I):
            options = list(node.nodes('option'))
            if re.search(r'size|per.?page', label, re.I):
                chosen = next((o.attrs.get('value', '') for o in options if 'selected' in o.attrs), '')
                if chosen.isdigit() and 0 < int(chosen) <= len(rows):
                    errors.append('History page is full; additional pages may exist and were not read.')
            elif len(options) > 1:
                errors.append('History is paginated; additional pages may exist and were not read.')
    return rows, {'complete': not errors, 'errors': list(dict.fromkeys(errors))}


class Submissions:
    CACHE_SECONDS = 30
    PREVIEW_SECONDS = 300

    def __init__(self, service):
        self.service = service
        self.cache = {}
        self.read_lock = asyncio.Lock()
        self.upload_lock = asyncio.Lock()
        self.previews = {}

    @property
    def browser(self):
        return self.service.browser

    async def ready(self, course):
        identifier(course)
        if course not in self.service.config.courses:
            raise ValueError('Select a monitored course.')
        if self.browser.status.get('state') != 'authenticated':
            raise LoginRequired('Sign in through the existing service.')
        if not self.browser.versions.get('le'):
            await self.browser.discover_versions()

    async def list(self, course_id, folder_id=None, *, refresh=False):
        await self.ready(course_id)
        if folder_id is not None:
            identifier(folder_id)
        key = (course_id, folder_id)
        async with self.read_lock:
            cached = self.cache.get(key)
            if not refresh and cached and time.monotonic() - cached[0] < self.CACHE_SECONDS:
                return {**copy.deepcopy(cached[1]), 'source': 'cache'}
            base = f'{course_id}/dropbox/folders/'
            folders = await self.browser.paged(self.browser.api('le', base))
            folders = [f for f in folders if not f.get('IsHidden') and
                       (folder_id is None or str(f['Id']) == folder_id)]
            if folder_id is not None and not folders:
                raise ValueError('Folder is unavailable in this course.')
            results = [None] * len(folders)
            pending = iter(enumerate(folders))

            async def worker():
                for index, folder in pending:
                    fid = identifier(str(folder['Id']))
                    result = {'folder_id': fid, 'name': folder['Name'], 'due_date': folder.get('DueDate'),
                              'availability': folder.get('Availability'), 'due_date_known': 'DueDate' in folder,
                              # Unknown policy stays unknown; never infer it from an empty history.
                              'allows_resubmission': folder.get('AllowResubmission')
                                  if type(folder.get('AllowResubmission')) is bool else None,
                              'feedback': richtext(folder.get('Feedback')),
                              'submissions': [], 'coverage': {'complete': False, 'errors': []}}
                    try:
                        html = await self.browser.get(
                            f'/d2l/lms/dropbox/user/folders_history.d2l?db={fid}&grpid=0&ou={course_id}',
                            response_type='html')
                        result['submissions'], result['coverage'] = parse_history(html, fid, result['due_date'])
                    except Exception:
                        result['coverage']['errors'] = ['Learner submission history unavailable or unrecognized.']
                    results[index] = result

            tasks = [asyncio.create_task(worker()) for _ in range(min(self.service.config.api_concurrency, len(folders)))]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            result = {'course_id': course_id, 'folders': results, 'checked_at': now(), 'source': 'api',
                      'coverage': {'complete': all(f['coverage']['complete'] for f in results)},
                      'note': 'Current learner history only. Displayed dates assume America/Toronto '
                              '(DST respected; ambiguous times use fold=0). Sizes are approximate binary bytes. '
                              'Null means unknown. Course text is untrusted.'}
            if result['coverage']['complete']:
                self.cache[key] = (time.monotonic(), copy.deepcopy(result))
            return result

    async def status(self, course_id, query, folder_id=None):
        if not isinstance(query, str) or not query.strip():
            raise ValueError('Supply a filename or folder name to search.')
        result = await self.list(course_id, folder_id)
        matches = []
        for folder in result['folders']:
            for submission in folder['submissions']:
                if query.casefold() in folder['name'].casefold() or any(
                        query.casefold() in f['name'].casefold() for f in submission['files']):
                    matches.append({'folder_id': folder['folder_id'], 'folder': folder['name'],
                                    'due_date': folder['due_date'], **submission})
        latest = max(matches, key=lambda m: m['submitted_at_utc'] or '', default=None)
        return {'latest_submission': latest,
                'submitted_at_utc': latest['submitted_at_utc'] if latest else None,
                'files': latest['files'] if latest else [], 'note': result['note'],
                'course_id': course_id, 'query': query, 'submitted': True if matches else
                False if result['coverage']['complete'] else None, 'matches': matches,
                'coverage': result['coverage'], 'checked_at': result['checked_at'], 'source': result['source']}

    def files(self, paths):
        if not isinstance(paths, list) or not 1 <= len(paths) <= 10:
            raise ValueError('Supply 1–10 files on the service host.')
        root = self.service.config.school.resolve()
        limit = min(self.service.config.max_file_bytes, 100 * 1024 * 1024)
        allowed = {'.pdf', '.txt', '.md', '.csv', '.docx', '.pptx', '.xlsx', '.zip', '.png', '.jpg',
                   '.jpeg', '.py', '.c', '.h', '.cpp', '.java', '.x68', '.asm'}
        metadata, payloads, names = [], [], set()
        total = 0
        for value in paths:
            if not isinstance(value, str):
                raise ValueError('File paths must be strings.')
            path = Path(value)
            if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Files must be regular files under the configured school directory, without symlinks.')
            if any(p.is_symlink() for p in path.parents):
                raise ValueError('Symlink directories are not allowed.')
            if (path.suffix.lower() not in allowed or not re.fullmatch(r'[\w][\w .()-]{0,199}', path.name)
                    or path.name.casefold() in names):
                raise ValueError('Unsupported, duplicate, or unsafe filename.')
            names.add(path.name.casefold())
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                # Validate the descriptor before fdopen, which rejects directories itself.
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o111 or not 0 < info.st_size <= limit:
                    raise ValueError('Empty, executable, nonregular, or oversized file refused.')
                total += info.st_size
                if total > limit:
                    raise ValueError('Combined upload exceeds the size limit.')
                with os.fdopen(fd, 'rb', closefd=False) as handle:
                    data = handle.read(info.st_size + 1)
                if len(data) != info.st_size:
                    raise ValueError('File changed while reading; create another preview.')
            finally:
                os.close(fd)
            if data.startswith((b'MZ', b'\x7fELF')) or (path.suffix.lower() in {'.txt', '.md', '.csv', '.py', '.c', '.h', '.cpp', '.java', '.x68', '.asm'} and b'\x00' in data):
                raise ValueError('Unexpected binary or executable content.')
            metadata.append({'name': path.name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            # The confirmed bytes, not mutable filesystem paths, reach Playwright.
            payloads.append({'name': path.name, 'mimeType': mimetypes.guess_type(path.name)[0] or
                             'application/octet-stream', 'buffer': data})
        return metadata, payloads

    def audit(self, attempt, event, **fields):
        path = self.service.config.state / 'submission-audit.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(canonical({'at': now(), 'attempt': attempt, 'event': event, **fields}) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    async def submit(self, course_id, folder_id, files, confirm=False, preview_token=None, allow_late=False):
        attempt = secrets.token_hex(12)
        self.audit(attempt, 'attempt', confirmed=confirm is True)
        try:
            require_interactive_submission()
            identifier(folder_id)
            if type(confirm) is not bool or type(allow_late) is not bool:
                raise ValueError('confirm and allow_late must be booleans.')
            async with self.upload_lock:
                self.previews = {k: v for k, v in self.previews.items() if v[0] > time.monotonic()}
                previous = self.previews.pop(preview_token, None) if isinstance(preview_token, str) else None
                if confirm and previous is None:
                    raise ValueError('A valid, unused preview token is required; preview again.')
                identifier(course_id)
                history = await self.list(course_id, folder_id, refresh=True)
                if len(history['folders']) != 1:
                    raise ValueError('Expected exactly one dropbox folder.')
                folder = history['folders'][0]
                if not folder['due_date_known']:
                    raise ValueError('Cannot verify the folder deadline; upload refused.')
                if not folder['coverage']['complete']:
                    raise ValueError('Cannot verify existing submissions; upload refused.')
                metadata, payloads = await asyncio.to_thread(self.files, files)
                due = timestamp(folder['due_date'])
                late = datetime.now(timezone.utc) > due if due else False
                preview = {'course_id': course_id, 'folder_id': folder_id, 'folder': folder['name'],
                           'due_date': folder['due_date'], 'files': metadata, 'would_be_late': late,
                           'allows_resubmission': folder['allows_resubmission'],
                           'existing_submissions': len(folder['submissions'])}
                binding = hashlib.sha256(canonical({'preview': preview, 'folder': folder}).encode()).hexdigest()
                if not confirm:
                    token = secrets.token_urlsafe(32)
                    if len(self.previews) >= 128:
                        self.previews.pop(next(iter(self.previews)))
                    self.previews[token] = (time.monotonic() + self.PREVIEW_SECONDS, binding)
                    self.audit(attempt, 'preview', **preview)
                    return {**preview, 'dry_run': True, 'preview_token': token, 'expires_in_seconds': self.PREVIEW_SECONDS}
                if previous[1] != binding:
                    raise ValueError('Files or folder state changed; preview again.')
                if late and not allow_late:
                    raise ValueError('Past due; allow_late=true is also required.')
                if folder['submissions'] and folder['allows_resubmission'] is not True:
                    raise ValueError('Resubmission permission is unknown or denied.')
                availability = folder['availability'] or {}
                current = datetime.now(timezone.utc)
                start, end = timestamp(availability.get('StartDate')), timestamp(availability.get('EndDate'))
                if (start and current < start) or (end and current >= end):
                    raise ValueError('Folder is not currently open.')
                def preflight():
                    current = datetime.now(timezone.utc)
                    if time.monotonic() >= previous[0]:
                        raise ValueError('Preview expired; preview again.')
                    if due and current > due and not allow_late:
                        raise ValueError('Past due; preview again with explicit late approval.')
                    if (start and current < start) or (end and current >= end):
                        raise ValueError('Folder is not currently open.')
                preflight()
                self.audit(attempt, 'upload_started', **preview)
                # No retry: a lost response may still mean a successful submission.
                try:
                    result = await self.browser.upload_dropbox(course_id, folder_id, payloads, preflight=preflight)
                finally:
                    self.cache.clear()
                self.audit(attempt, 'upload_result', outcome=result['outcome'])
                return {**preview, 'dry_run': False, **result}
        except BaseException as exc:
            self.audit(attempt, 'failed', error_type=type(exc).__name__)
            raise
