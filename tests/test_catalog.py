import pytest
from courselink_mcp.browser import BrowserSession, LoginRequired, EndpointUnavailable
from courselink_mcp.catalog import Catalog, linked_files, category
from courselink_mcp.config import Config


class FakeBrowser:
    def __init__(self, response):
        self.response = response

    def api(self, product, suffix):
        return f'/d2l/api/{product}/1.99/{suffix}'

    def url(self, path):
        return 'https://courselink.uoguelph.ca' + path

    async def get(self, path):
        return self.response

    async def paged(self, path):
        return self.response


async def test_recursive_content_ignores_hidden_and_progress_fields():
    toc = {'Modules': [{'Title': 'Lab Materials', 'Topics': [], 'Modules': [
        {'Title': 'Week 1', 'Topics': [
            {'TopicId': 2, 'Title': 'Instructions', 'Url': '/content/enforced/1/lab.pdf', 'Unread': True},
            {'TopicId': 3, 'Title': 'Hidden', 'IsHidden': True},
            {'TopicId': 4, 'Title': 'External', 'Url': 'https://example.com/a.pdf'},
        ]}]}]}
    catalog = Catalog(FakeBrowser(toc))
    rows = await catalog.content('1')
    assert len(rows) == 2
    assert rows[0]['category'] == 'lab'
    assert rows[0]['module'] == 'Lab Materials/Week 1'
    assert rows[0]['download_path'].endswith('/topics/2/file')
    assert rows[1]['download_path'] is None
    toc['Modules'][0]['Modules'][0]['Topics'][0]['Unread'] = False
    assert await catalog.content('1') == rows


async def test_assignment_attachments_and_rubrics():
    rows = await Catalog(FakeBrowser([{'Id': 9, 'Name': 'Lab 1', 'CustomInstructions': {'Html': '<p>Use Java</p>'},
        'DueDate': '2026-10-03', 'Assessment': {'ScoreDenominator': 10},
        'Attachments': [{'FileId': 7, 'FileName': 'rubric.pdf', 'Size': 30}]}])).assignments('1')
    assert rows[0]['description'] == 'Use Java'
    assert rows[0]['assessment']['ScoreDenominator'] == 10
    assert rows[1]['parent_id'] == rows[0]['id']
    assert rows[1]['due_date'] == '2026-10-03'
    assert rows[1]['category'] == 'lab'


async def test_pagination_and_repeated_cursor(tmp_path):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    responses = [{'Items': [1], 'PagingInfo': {'HasMoreItems': True, 'Bookmark': 'a'}},
                 {'Items': [2], 'PagingInfo': {'HasMoreItems': False}}]
    async def get(path, params):
        return responses.pop(0)
    browser.get = get
    assert await browser.paged('/x') == [1, 2]
    responses.extend([{'Items': [], 'PagingInfo': {'HasMoreItems': True, 'Bookmark': 'a'}}] * 2)
    with pytest.raises(ValueError, match='repeated'):
        await browser.paged('/x')


def test_reject_foreign_download_hosts(tmp_path):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    for url in ['https://evil.test/a', '//evil.test/a', 'http://courselink.uoguelph.ca/a',
                'https://courselink.uoguelph.ca.evil.test/a', 'file:///etc/passwd']:
        with pytest.raises(ValueError):
            browser.url(url)


def test_inline_file_discovery_is_scoped_to_course_links(tmp_path):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    parent = {'id': '1:content:2', 'course_id': '1', 'title': 'Lab', 'category': 'lab',
              'source_url': '/content/enforced/1-class/unit/page.html'}
    html = '''<a href="../instructions.pdf">Lab instructions</a>
              <a href="https://evil.test/content/secret.pdf">bad</a>
              <a href="/d2l/logout">logout</a><a href="/shared/rubric.docx">Rubric</a>'''
    children = linked_files(parent, html, browser)
    assert len(children) == 2
    assert children[0]['source_url'] == 'https://courselink.uoguelph.ca/content/enforced/1-class/instructions.pdf'
    assert children[0]['category'] == 'lab'
    assert children[1]['filename'] == 'rubric.docx'


def test_assessment_modules_are_included_with_assignments():
    assert category('Essay instructions', 'Assessments') == 'assignment'
    assert category('StudentInstruction', 'Labs/Lab1') == 'lab'


def test_link_discovery_includes_unfamiliar_files_without_leaving_course_storage(tmp_path):
    browser = BrowserSession(Config(state=tmp_path, school=tmp_path))
    parent = {'id': '1:content:2', 'course_id': '1', 'title': 'Lab',
              'source_url': '/content/enforced/1/page.html'}
    html = '''<a href="starter.x68">Assembly</a><a href="diagram.png">Diagram</a>
              <a href="download">Extensionless attachment</a><a href="/shared/data.weird">Data</a>
              <a href="/content/">Directory</a><a href="/d2l/logout">Logout</a>
              <a href="https://evil.test/content/a.pdf">External</a>
              <a href="/content/%2e%2e/d2l/logout">Encoded traversal</a>'''
    assert [row['filename'] for row in linked_files(parent, html, browser)] == [
        'starter.x68', 'diagram.png', 'download', 'data.weird']


async def test_shared_content_topics_are_downloadable():
    toc = {'Modules': [{'Title': 'Files', 'Topics': [
        {'TopicId': 7, 'Title': 'Instructions', 'Url': '/shared/instructions.pdf'}]}]}
    rows = await Catalog(FakeBrowser(toc)).content('1')
    assert rows[0]['download_path'].endswith('/topics/7/file')


@pytest.mark.parametrize('method,raw', [
    ('calendar', {'CalendarEventId': 2, 'Title': 'Event'}),
    ('quizzes', {'QuizId': 2, 'Name': 'Quiz'}),
])
async def test_calendar_and_quiz_keep_links_for_file_discovery(method, raw):
    description = {'Html': '<a href="/content/enforced/1/instructions.pdf">Read this</a>'}
    rows = await getattr(Catalog(FakeBrowser([{**raw, 'Description': description}])), method)('1')
    assert rows[0]['instructions'] == description
