import asyncio
import json
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from .store import now


class LoginRequired(RuntimeError):
    pass


class EndpointUnavailable(RuntimeError):
    pass


class RateLimited(RuntimeError):
    pass


def retry_after_seconds(value):
    """Honor both Retry-After forms, without shortening a server's delay."""
    try:
        delay = float(value)
        if not math.isfinite(delay):
            raise ValueError('Non-finite delay')
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                raise ValueError('Retry-After date needs a timezone')
            delay = date.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            delay = 10
    return max(delay, 1)


class BrowserSession:
    def __init__(self, config):
        self.config = config
        self.context = None
        self.playwright = None
        self.page = None
        self.versions = {}
        self.status = {'state': 'starting', 'last_verified': None}
        self.lock = asyncio.Lock()
        self.api_slots = asyncio.Semaphore(config.api_concurrency)
        self.rate_limited_until = 0
        self.metrics = dict.fromkeys(('requests', 'rate_limits', 'request_seconds',
            'queue_seconds', 'cooldown_seconds', 'successes', 'errors', 'cancellations'), 0)
        self.metric_scope = ContextVar('courselink_api_metrics', default=None)
        self.closed = True

    def count(self, key, value=1):
        self.metrics[key] += value
        scope = self.metric_scope.get()
        if scope is not None:
            scope[key] += value

    @contextmanager
    def measure(self):
        metrics = dict.fromkeys(self.metrics, 0)
        token = self.metric_scope.set(metrics)
        try:
            yield metrics
        finally:
            self.metric_scope.reset(token)

    def rate_limit(self, retry_after):
        self.count('rate_limits')
        delay = retry_after_seconds(retry_after)
        self.rate_limited_until = max(self.rate_limited_until, time.monotonic() + delay)

    async def wait_for_cooldown(self, deadline=None):
        deadline = time.monotonic() + 60 if deadline is None else deadline
        started = time.monotonic()
        try:
            while self.rate_limited_until > time.monotonic():
                if self.rate_limited_until > deadline:
                    raise RateLimited('CourseLink requested a longer cooldown. Work deferred until a later scan.')
                await asyncio.sleep(self.rate_limited_until - time.monotonic())
        finally:
            self.count('cooldown_seconds', time.monotonic() - started)

    async def start(self):
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        profile = self.config.state / 'browser-profile'
        profile.mkdir(exist_ok=True, mode=0o700)
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(profile), headless=self.config.headless,
            accept_downloads=True, viewport={'width': 1100, 'height': 800},
            args=['--disable-background-timer-throttling'])
        self.closed = False
        self.context.on('close', lambda _: setattr(self, 'closed', True))
        self.context.set_default_timeout(10000)
        auth = self.config.state / 'browser-auth.json'
        if auth.exists():
            try:
                await self.context.add_cookies(json.loads(auth.read_text()).get('cookies', []))
            except (ValueError, TypeError):
                pass
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        # Navigation may need MFA; a slow login must not prevent the MCP listener starting.
        try:
            await self.page.goto(self.config.base_url + '/d2l/home', wait_until='domcontentloaded', timeout=20000)
        except Exception:
            self.status = {'state': 'login_required', 'message': 'Open the browser and sign in.'}

    async def close(self):
        context, playwright = self.context, self.playwright
        self.context = self.playwright = self.page = None
        self.closed = True
        try:
            if context:
                await context.close()
        finally:
            if playwright:
                await playwright.stop()

    def url(self, path):
        url = urljoin(self.config.base_url + '/', path)
        parsed = urlsplit(url)
        origin = urlsplit(self.config.base_url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc) or parsed.username or parsed.password:
            raise ValueError('Only CourseLink URLs can be fetched.')
        return url

    async def cookies(self):
        if not self.context:
            raise LoginRequired('Browser is not ready.')
        cookies = httpx.Cookies()
        for cookie in await self.context.cookies(self.config.base_url):
            cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'])
        return cookies

    async def get(self, path, params=None):
        # Use browser requests so refreshed session cookies are retained by Chromium.
        url = self.url(path)
        if self.closed or not self.context:
            raise LoginRequired('Browser is reconnecting.')
        context = self.context
        deadline = time.monotonic() + 60
        for attempt in range(3):
            # Cooldown waits do not occupy request slots; recheck after admission.
            while True:
                await self.wait_for_cooldown(deadline)
                queued = time.monotonic()
                try:
                    await self.api_slots.acquire()
                finally:
                    self.count('queue_seconds', time.monotonic() - queued)
                if self.rate_limited_until <= time.monotonic():
                    break
                self.api_slots.release()
            try:
                if self.closed or self.context is not context:
                    raise LoginRequired('Browser reconnected during the request; retry with its new session.')
                self.count('requests')
                started = time.monotonic()
                response = None
                try:
                    response = await context.request.get(url, params=params,
                        timeout=30000, max_redirects=0, headers={'Accept': 'application/json'})
                    status = response.status
                    if status == 429:
                        self.rate_limit(response.headers.get('retry-after', '5'))
                        if attempt == 2:
                            raise RateLimited('CourseLink requested slower polling. Will retry later.')
                    elif status in (301, 302, 303, 307, 308, 401):
                        raise LoginRequired('CourseLink requires sign-in in the dedicated browser.')
                    elif status in (403, 404):
                        raise EndpointUnavailable(f'CourseLink endpoint returned {status}: {urlsplit(url).path}')
                    elif status >= 400:
                        raise RuntimeError(f'CourseLink returned HTTP {status}.')
                    elif 'json' not in response.headers.get('content-type', '').lower():
                        raise LoginRequired('CourseLink returned a sign-in page instead of data.')
                    else:
                        data = await response.json()
                        self.count('successes')
                        return data
                except asyncio.CancelledError:
                    self.count('cancellations')
                    raise
                except Exception:
                    self.count('errors')
                    raise
                finally:
                    self.count('request_seconds', time.monotonic() - started)
                    if response is not None:
                        await response.dispose()
            finally:
                self.api_slots.release()
        raise RateLimited('CourseLink rate limit reached.')

    async def discover_versions(self):
        data = await self.get('/d2l/api/versions/')
        self.versions = {row['ProductCode']: row['LatestVersion'] for row in data
                         if row['ProductCode'] in ('lp', 'le')}
        if not all(k in self.versions for k in ('lp', 'le')):
            raise RuntimeError('CourseLink API versions could not be discovered.')

    def api(self, product, suffix):
        return f'/d2l/api/{product}/{self.versions[product]}/{suffix.lstrip("/")}'

    async def paged(self, path, params=None):
        params = dict(params or {})
        rows, seen = [], set()
        for _ in range(1000):
            data = await self.get(path, params)
            if isinstance(data, list):
                return rows + data
            entries = data.get('Items', data.get('Objects'))
            if entries is None:
                raise ValueError('Unrecognized paginated CourseLink response.')
            rows.extend(entries)
            info = data.get('PagingInfo', {})
            next_url = data.get('Next')
            if info.get('HasMoreItems'):
                bookmark = str(info.get('Bookmark', ''))
                if not bookmark or bookmark in seen:
                    raise ValueError('CourseLink pagination repeated a bookmark.')
                seen.add(bookmark)
                params['bookmark'] = bookmark
            elif next_url:
                if next_url in seen:
                    raise ValueError('CourseLink pagination repeated a URL.')
                seen.add(next_url)
                path, params = self.url(next_url), {}
            else:
                return rows
        raise ValueError('CourseLink pagination exceeded its safety limit.')

    async def open_login(self):
        if self.config.headless:
            return {'message': 'Remote service is headless. Run the local remote-login helper to sign in and refresh the server session.'}
        async with self.lock:
            if not self.page or self.page.is_closed():
                self.page = await self.context.new_page()
            if self.status.get('state') != 'authenticated':
                await self.page.goto(self.config.base_url + '/d2l/home', wait_until='domcontentloaded')
            await self.page.bring_to_front()
        return {'message': 'Use the CourseLink browser window to sign in and complete MFA.'}

    async def keepalive(self):
        async with self.lock:
            if not self.page or self.page.is_closed():
                self.page = await self.context.new_page()
                await self.page.goto(self.config.base_url + '/d2l/home', wait_until='domcontentloaded')
            # Only extend a CourseLink inactivity dialog, never a generic Yes/MFA prompt.
            if urlsplit(self.page.url).netloc == urlsplit(self.config.base_url).netloc:
                dialogs = self.page.get_by_role('dialog')
                for dialog in await dialogs.all():
                    if not await dialog.is_visible():
                        continue
                    text = await dialog.inner_text()
                    if re.search(r'(inactiv|session.*(expir|end)|still there|still here|sign.?out|log.?out)', text, re.I):
                        button = dialog.get_by_role('button', name=re.compile(
                            r'^(Yes|Stay (Signed|Logged) In|Extend Session|Continue Session|Keep Working)$', re.I))
                        if await button.count() == 1:
                            await button.click()
            try:
                if not self.versions:
                    await self.discover_versions()
                who = await self.get(self.api('lp', 'users/whoami'))
                if not who.get('Identifier'):
                    raise LoginRequired('Sign in to CourseLink.')
                self.status = {'state': 'authenticated', 'last_verified': now()}
                auth = self.config.state / 'browser-auth.json'
                temporary = auth.with_suffix('.tmp')
                state = await self.context.storage_state()
                # Keep only CourseLink authentication, not Microsoft SSO cookies.
                state['cookies'] = [c for c in state['cookies'] if
                    c['domain'].lstrip('.') == 'courselink.uoguelph.ca']
                state['origins'] = [o for o in state['origins'] if o['origin'] == self.config.base_url]
                temporary.write_text(json.dumps(state))
                temporary.chmod(0o600)
                temporary.replace(auth)
            except (LoginRequired, EndpointUnavailable):
                self.status = {'state': 'login_required', 'last_verified': self.status.get('last_verified'),
                               'message': 'Complete sign-in/MFA in the dedicated CourseLink browser.'}
            return self.status
