"""Dependency-free tests also runnable with unittest in an offline checkout."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from courselink_mcp.config import Config
from courselink_mcp.submissions import Submissions, lateness


class SubmissionsCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
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
            paged=AsyncMock(side_effect=paged), get=AsyncMock(side_effect=get))
        self.config = Config(state=root, school=school, courses={'123': 'example'})
        self.submissions = Submissions(SimpleNamespace(config=self.config, browser=self.browser))

    def tearDown(self):
        self.directory.cleanup()

    async def test_single_flight_cache_and_partial_history(self):
        results = await asyncio.gather(*(self.submissions.list('123') for _ in range(5)))
        self.assertTrue(all(r['coverage']['complete'] for r in results))
        self.assertEqual(self.browser.paged.await_count, 1)
        self.assertEqual(self.browser.get.await_count, 1)
        self.submissions.cache.clear()
        self.rows = [{'Unexpected': 'shape'}]
        result = await self.submissions.list('123', query='answer')
        self.assertIsNone(result['submitted'])
        self.assertFalse(result['coverage']['complete'])

    def test_timezone_aware_deadlines(self):
        self.assertFalse(lateness('2040-01-01T12:00:00Z', '2040-01-01T07:00:00-05:00'))
        self.assertTrue(lateness('2040-01-01T12:00:01Z', '2040-01-01T12:00:00Z'))
        self.assertIsNone(lateness('invalid', None))


if __name__ == '__main__':
    unittest.main()
