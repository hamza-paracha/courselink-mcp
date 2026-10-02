from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin, unquote
import hashlib
import re


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            self.links.extend(value for key, value in attrs if key.lower() == 'href' and value)


def linked_files(parent, html, browser):
    parser = LinkParser()
    parser.feed(html)
    result = []
    base = urljoin(browser.config.base_url, parent.get('source_url') or parent.get('url') or '/')
    for link in parser.links:
        url = urljoin(base, link)
        parsed = urlsplit(url)
        if not parsed.path.startswith(('/content/', '/shared/')):
            continue
        if not re.search(r'\.(pdf|docx?|pptx?|xlsx?|csv|txt|md|zip|gz|tar|java|py|c|h|cpp|html?)$', parsed.path, re.I):
            continue
        try:
            url = browser.url(url.split('#', 1)[0])
        except ValueError:
            continue
        identifier = hashlib.sha256(url.encode()).hexdigest()[:24]
        filename = unquote(parsed.path.rsplit('/', 1)[-1])
        result.append(item(parent['course_id'], 'linked_file', identifier, filename,
            category=parent.get('category', category(parent['title'])),
            source_url=url, url=url, filename=filename, download_path=url))
    return result


def richtext(value):
    if isinstance(value, dict):
        value = value.get('Text') or value.get('Html') or ''
    parser = TextParser()
    parser.feed(str(value or ''))
    return ' '.join(' '.join(parser.parts).split())


def category(title, path=''):
    text = (path + ' ' + title).lower()
    if re.search(r'\blab(?:s|oratory|oratories)?\b', text):
        return 'lab'
    if re.search(r'\b(assignments?|assessments?|homework|projects?)\b', text):
        return 'assignment'
    return 'content'


def item(course, kind, identifier, title, **extra):
    return {'id': f'{course}:{kind}:{identifier}', 'course_id': str(course),
            'kind': kind, 'title': title or str(identifier), **extra}


class Catalog:
    def __init__(self, browser):
        self.browser = browser

    async def courses(self):
        rows = await self.browser.paged(self.browser.api('lp', 'enrollments/myenrollments/'))
        result = []
        for row in rows:
            unit = row.get('OrgUnit', row)
            if unit.get('Type', {}).get('Id') not in (3, '3'):
                continue
            result.append({'id': str(unit['Id']), 'name': unit.get('Name', ''),
                           'code': unit.get('Code', ''), 'access': row.get('Access'),
                           'url': self.browser.url(f'/d2l/home/{unit["Id"]}')})
        return result

    async def content(self, course):
        toc = await self.browser.get(self.browser.api('le', f'{course}/content/toc'))
        if not isinstance(toc.get('Modules'), list):
            raise ValueError('Unrecognized content table of contents.')
        results = []
        async def walk(modules, parent=''):
            for module in modules:
                if module.get('IsHidden') or module.get('IsLocked'):
                    continue
                path = f'{parent}/{module["Title"]}'.strip('/')
                if 'ModuleId' in module:
                    mid = module['ModuleId']
                    details = await self.browser.get(self.browser.api('le', f'{course}/content/modules/{mid}'))
                    results.append(item(course, 'content', f'module:{mid}', module['Title'],
                        category=category(module['Title'], path), module=path, content_type='module',
                        description=richtext(details.get('Description')), instructions=details.get('Description'),
                        start_date=details.get('StartDate'), end_date=details.get('EndDate'),
                        due_date=details.get('DueDate'), modified_at=module.get('LastModifiedDate'),
                        url=self.browser.url(f'/d2l/le/content/{course}/Home'), download_path=None))
                for topic in module.get('Topics', []):
                    if topic.get('IsHidden') or topic.get('IsLocked'):
                        continue
                    identifier = topic['TopicId']
                    details = await self.browser.get(self.browser.api('le', f'{course}/content/topics/{identifier}'))
                    source = topic.get('Url', '')
                    # Only file-backed course topics; links to other activities stay as metadata.
                    file_backed = urlsplit(source).path.startswith('/content/')
                    results.append(item(course, 'content', identifier, topic['Title'],
                        category=category(topic['Title'], path), module=path,
                        url=self.browser.url(f'/d2l/le/content/{course}/viewContent/{identifier}/View'),
                        source_url=source, modified_at=topic.get('LastModifiedDate'),
                        description=richtext(details.get('Description')), instructions=details.get('Description'),
                        due_date=details.get('DueDate'), content_type='topic',
                        start_date=topic.get('StartDateTime'), end_date=topic.get('EndDateTime'),
                        download_path=self.browser.api('le', f'{course}/content/topics/{identifier}/file') if file_backed else None,
                        filename=urlsplit(source).path.rsplit('/', 1)[-1] if file_backed else None))
                await walk(module.get('Modules', []), path)
        await walk(toc['Modules'])
        return results

    async def assignments(self, course):
        rows = await self.browser.paged(self.browser.api('le', f'{course}/dropbox/folders/'))
        results = []
        for row in rows:
            if row.get('IsHidden'):
                continue
            identifier = row['Id']
            title = row['Name']
            parent = item(course, 'assignment', identifier, title,
                category='lab' if category(title) == 'lab' else 'assignment',
                description=richtext(row.get('CustomInstructions')),
                instructions=row.get('CustomInstructions'), due_date=row.get('DueDate'),
                availability=row.get('Availability'), assessment=row.get('Assessment'),
                links=row.get('LinkAttachments', []),
                url=self.browser.url(f'/d2l/lms/dropbox/dropbox.d2l?ou={course}'), download_path=None)
            results.append(parent)
            for attachment in row.get('Attachments', []):
                fid = attachment['FileId']
                results.append(item(course, 'assignment', f'{identifier}:file:{fid}', attachment['FileName'],
                    category=parent['category'], parent_id=parent['id'], due_date=parent['due_date'],
                    filename=attachment['FileName'], size=attachment.get('Size'),
                    url=parent['url'], download_path=self.browser.api('le',
                        f'{course}/dropbox/folders/{identifier}/attachments/{fid}')))
        return results

    async def announcements(self, course):
        rows = await self.browser.paged(self.browser.api('le', f'{course}/news/'))
        results = []
        for row in rows:
            if row.get('IsHidden') or row.get('IsPublished') is False:
                continue
            identifier = row['Id']
            parent = item(course, 'announcement', identifier, row['Title'],
                description=richtext(row.get('Body')), body=row.get('Body'),
                modified_at=row.get('LastModifiedDate'), start_date=row.get('StartDate'),
                url=self.browser.url(f'/d2l/home/{course}'), download_path=None)
            results.append(parent)
            for attachment in row.get('Attachments', []):
                fid = attachment['FileId']
                results.append(item(course, 'announcement', f'{identifier}:file:{fid}', attachment['FileName'],
                    parent_id=parent['id'], filename=attachment['FileName'], size=attachment.get('FileSize'),
                    url=parent['url'], download_path=self.browser.api('le',
                        f'{course}/news/{identifier}/attachments/{fid}')))
        return results

    async def calendar(self, course):
        rows = await self.browser.paged(self.browser.api('le', f'{course}/calendar/events/'))
        return [item(course, 'event', row['CalendarEventId'], row['Title'],
                     description=richtext(row.get('Description')),
                     start_date=row.get('StartDateTime') or row.get('StartDay'),
                     end_date=row.get('EndDateTime') or row.get('EndDay'),
                     due_date=row.get('StartDateTime') if row.get('EventType') == 6 else None,
                     location=row.get('LocationName'), recurrence=row.get('RecurrenceInfo'),
                     url=row.get('CalendarEventViewUrl'), download_path=None) for row in rows]

    async def quizzes(self, course):
        rows = await self.browser.paged(self.browser.api('le', f'{course}/quizzes/'))
        return [item(course, 'quiz', row['QuizId'], row['Name'],
                     due_date=row.get('DueDate'), start_date=row.get('StartDate'),
                     end_date=row.get('EndDate'), description=richtext(row.get('Description')),
                     url=self.browser.url(f'/d2l/lms/quizzing/quizzes.d2l?ou={course}'),
                     download_path=None) for row in rows if not row.get('IsHidden')]
