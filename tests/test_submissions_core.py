"""Dependency-free tests also runnable with unittest in an offline checkout."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from courselink_mcp.browser import BrowserSession
from courselink_mcp.config import Config
from courselink_mcp.submissions import Submissions, lateness


class SubmissionsCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Keep this offline harness independent of sandbox thread-wakeup sockets.
        # File validation itself is exercised; the production executor is not mocked in pytest tests.
        async def inline(function, *args):
            return function(*args)
        self.thread_patch = patch('courselink_mcp.submissions.asyncio.to_thread', inline)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        root = Path(self.directory.name)
        school = root / 'school'
        school.mkdir()
        self.folder = {'Id': 1, 'Name': 'Example', 'DueDate': '2099-01-01T00:00:00Z', 'AllowResubmission': True}
        self.rows = []
        async def paged(path):
            return [dict(self.folder)] if path.endswith('/folders/') else self.rows
        async def get(path, *, response_type):
            assert path == '/d2l/lms/dropbox/user/folders_history.d2l?db=1&grpid=0&ou=123'
            assert response_type == 'html'
            if self.rows and 'Unexpected' in self.rows[0]:
                return '<html>Unknown</html>'
            fixture = 'one_submission' if self.rows else 'empty'
            return (Path(__file__).parent / 'fixtures' / f'dropbox_history_{fixture}.html').read_text().replace('41001_0', '1_0')
        self.browser = SimpleNamespace(status={'state': 'authenticated'}, versions={'le': '1.99'},
            api=lambda product, suffix: '/d2l/api/' + product + '/1.99/' + suffix,
            paged=AsyncMock(side_effect=paged), get=AsyncMock(side_effect=get), upload_dropbox=AsyncMock(return_value={'outcome': 'submitted'}))
        self.config = Config(state=root, school=school, courses={'123': 'example'})
        self.submissions = Submissions(SimpleNamespace(config=self.config, browser=self.browser))
        self.file = school / 'answer.txt'
        self.file.write_text('Synthetic answer')
        self.files = [str(self.file)]

    def tearDown(self):
        self.directory.cleanup()

    async def preview(self):
        return await self.submissions.submit('123', '1', self.files)

    async def confirm(self, preview, **kwargs):
        return await self.submissions.submit('123', '1', self.files, confirm=True,
                                             preview_token=preview['preview_token'], **kwargs)

    async def test_preview_confirmation_and_single_use(self):
        preview = await self.preview()
        self.browser.upload_dropbox.assert_not_awaited()
        self.assertTrue(preview['dry_run'])
        self.assertEqual((await self.confirm(preview))['outcome'], 'submitted')
        self.browser.upload_dropbox.assert_awaited_once()
        self.assertEqual(self.browser.upload_dropbox.call_args.args[2][0]['buffer'], b'Synthetic answer')
        with self.assertRaises(ValueError):
            await self.confirm(preview)
        audit = (self.config.state / 'submission-audit.jsonl').read_text()
        self.assertNotIn(preview['preview_token'], audit)
        self.assertEqual(json.loads(audit.splitlines()[-1])['event'], 'failed')

    async def test_changed_file_and_policy_refused(self):
        preview = await self.preview()
        self.file.write_text('Changed content')
        with self.assertRaises(ValueError):
            await self.confirm(preview)
        self.rows = [{'Id': 2, 'Files': [], 'SubmissionDate': '2040-01-01T00:00:00Z'}]
        self.folder['AllowResubmission'] = None
        preview = await self.preview()
        with self.assertRaises(ValueError):
            await self.confirm(preview)
        self.browser.upload_dropbox.assert_not_awaited()

    async def test_late_requires_explicit_opt_in(self):
        self.folder['DueDate'] = '2000-01-01T00:00:00Z'
        preview = await self.preview()
        self.assertTrue(preview['would_be_late'])
        with self.assertRaises(ValueError):
            await self.confirm(preview)
        self.assertEqual((await self.confirm(await self.preview(), allow_late=True))['outcome'], 'submitted')

    async def test_single_flight_cache_and_partial_history(self):
        results = await asyncio.gather(*(self.submissions.list('123') for _ in range(5)))
        self.assertTrue(all(r['coverage']['complete'] for r in results))
        self.assertEqual(self.browser.paged.await_count, 1)
        self.assertEqual(self.browser.get.await_count, 1)
        self.submissions.cache.clear()
        self.rows = [{'Unexpected': 'shape'}]
        result = await self.submissions.status('123', 'answer')
        self.assertIsNone(result['submitted'])
        self.assertFalse(result['coverage']['complete'])

    async def test_invalid_files_are_audited(self):
        for data in (b'', b'\x7fELFbad', b'MZbad', b'unexpected\x00binary'):
            self.file.write_bytes(data)
            with self.assertRaises(ValueError):
                await self.preview()
        self.browser.upload_dropbox.assert_not_awaited()
        events = [json.loads(line)['event'] for line in (self.config.state / 'submission-audit.jsonl').read_text().splitlines()]
        self.assertEqual(events.count('failed'), 4)

    async def test_missing_deadline_is_not_assumed_unlimited(self):
        del self.folder['DueDate']
        with self.assertRaises(ValueError):
            await self.preview()
        self.browser.upload_dropbox.assert_not_awaited()

    async def test_browser_outcome_and_last_moment_guard(self):
        for mode in ('success', 'unknown', 'expired'):
            with self.subTest(mode=mode):
                browser = BrowserSession(self.config)
                browser.closed = False
                browser.status = {'state': 'authenticated'}
                control = SimpleNamespace(count=AsyncMock(return_value=1),
                    is_enabled=AsyncMock(return_value=True), set_input_files=AsyncMock(), click=AsyncMock())
                receipt = SimpleNamespace(wait_for=AsyncMock(side_effect=TimeoutError() if mode == 'unknown' else None))
                page = SimpleNamespace(url='', locator=lambda _: control,
                    get_by_role=lambda *a, **kw: control, get_by_text=lambda *a, **kw: receipt, close=AsyncMock())
                async def goto(url, **kw):
                    page.url = url
                page.goto = goto
                browser.context = SimpleNamespace(new_page=AsyncMock(return_value=page))
                calls = 0
                def preflight():
                    nonlocal calls
                    calls += 1
                    if mode == 'expired' and calls == 2:
                        raise ValueError('Expired')
                if mode == 'expired':
                    with self.assertRaises(ValueError):
                        await browser.upload_dropbox('123', '1', [], preflight=preflight)
                    control.click.assert_not_awaited()
                else:
                    result = await browser.upload_dropbox('123', '1', [], preflight=preflight)
                    self.assertEqual(result['outcome'], 'submitted' if mode == 'success' else 'unknown')
                    control.click.assert_awaited_once()
                page.close.assert_awaited_once()

    def test_timezone_aware_deadlines(self):
        self.assertFalse(lateness('2040-01-01T12:00:00Z', '2040-01-01T07:00:00-05:00'))
        self.assertTrue(lateness('2040-01-01T12:00:01Z', '2040-01-01T12:00:00Z'))
        self.assertIsNone(lateness('invalid', None))


if __name__ == '__main__':
    unittest.main()
