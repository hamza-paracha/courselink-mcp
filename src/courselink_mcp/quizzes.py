"""Read-only learner quiz summaries. Never navigate to or start an attempt."""
import asyncio
import copy
from datetime import datetime, timezone
import re
import time

from .grades import has_grade
from .store import now
from .submissions import HistoryParser, history_datetime, identifier


class SummaryParser(HistoryParser):
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == 'br':
            self.current.children.append(' ')

    def handle_data(self, data):
        node = self.current
        while node is not None:
            if node.tag in ('script', 'style'):
                return
            node = node.parent
        super().handle_data(data)


DATE = r'[A-Za-z]{3} \d{1,2}, \d{4} \d{1,2}:\d{2}\s*[AP]M'


def parse_summary(html, name):
    """Parse inert English learner markup, validating the summary heading first."""
    parser = SummaryParser()
    parser.feed(html)
    root = parser.root
    clean = lambda node: ' '.join(node.text().split())
    headings = [clean(n) for n in root.nodes() if n.tag in ('h1', 'h2', 'h3')]
    if not any(re.fullmatch(r'Summary\s*-\s*' + re.escape(name), h, re.I) for h in headings):
        raise ValueError('Quiz summary heading unavailable or mismatched.')
    nodes = list(root.nodes())

    def find(element_id, pattern):
        # IDs are hints only: require matching content, then fall back to text.
        ordered = [n for n in nodes if n.attrs.get('id') == element_id]
        ordered += [n for n in nodes if n.tag in ('label', 'span', 'p', 'div')]
        return next((m for n in ordered if (m := re.search(pattern, clean(n), re.I))), None)

    allowance = find('z_h', r'Allowed\s*-\s*(\d+|Unlimited|No limit)')
    attempts = find('z_h', r'Completed\s*-\s*(\d+)')
    allowed = int(allowance[1]) if allowance and allowance[1].isdigit() else None
    completed = int(attempts[1]) if attempts else None
    limit = find('z_f', r'(\d+(?:\.\d+)?)\s*(minutes?|hours?)\b')
    due = find('z_g', r'Due on\s+(' + DATE + ')')
    closed = find('z_d', r'Quiz Period Over') is not None
    opened = find('z_d', r'Click\s+["“]Start Quiz["”]\s+to begin Attempt\s+\d+') is not None
    availability = find('z_g', r'Available on\s+(' + DATE + r')\s+until\s+(' + DATE + ')')
    if not closed and not opened and availability:
        start, end = (history_datetime(availability[i]) for i in (1, 2))
        if start and end:
            current = datetime.now(timezone.utc)
            start, end = (datetime.fromisoformat(v.replace('Z', '+00:00')) for v in (start, end))
            # A future quiz is not missed. Only an elapsed end means closed.
            closed = current >= end
            opened = start <= current < end
    state = 'closed' if closed else 'open' if opened else 'unknown'
    status = 'unknown'
    if completed is not None:
        if closed:
            status = 'missed' if completed == 0 else 'done'
        elif allowed is not None and completed >= allowed:
            status = 'completed_all_attempts'
        elif opened:
            status = 'not_started' if completed == 0 else 'in_progress_or_partial' if allowance else 'unknown'
    errors = []
    if attempts is None or allowance is None:
        errors.append('Quiz attempt counts unavailable or unrecognized.')
    if state == 'unknown':
        errors.append('Quiz open/closed state unavailable or unrecognized.')
    return {'attempts_allowed': allowed, 'attempts_completed': completed,
            'attempts_remaining': max(0, allowed - completed) if allowed is not None and completed is not None else None,
            'attempted': completed > 0 if completed is not None else None,
            'time_limit_minutes': float(limit[1]) * (60 if limit[2].lower().startswith('hour') else 1) if limit else None,
            'due_date': history_datetime(due[1]) if due else None,
            'due_date_display': due[1] if due else None,
            'state': state, 'is_open': True if opened and not closed else False if closed else None,
            'status': status, 'coverage': {'complete': not errors, 'errors': errors}}


class Quizzes:
    CACHE_SECONDS = 30

    def __init__(self, service):
        self.service = service
        self.cache = {}
        self.lock = asyncio.Lock()

    def fresh(self, result, source):
        result = copy.deepcopy(result)
        age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(result['checked_at'])).total_seconds())
        result['source'] = source
        result['freshness'] = {'checked_at': result['checked_at'], 'age_seconds': round(age, 1),
            'refresh_interval_seconds': self.CACHE_SECONDS, 'refresh_due': age >= self.CACHE_SECONDS or not result['coverage']['complete'],
            'session_state': self.service.browser.status.get('state')}
        for quiz in result['quizzes']:
            quiz['freshness'] = copy.deepcopy(result['freshness'])
            quiz['freshness']['refresh_due'] = age >= self.CACHE_SECONDS or not quiz['coverage']['complete']
        return result

    async def list(self, course_id, quiz_id=None):
        """List learner summaries with independent grade-cache freshness; GET requests only."""
        identifier(course_id)
        if course_id not in self.service.config.courses:
            raise ValueError('Select a monitored course.')
        if quiz_id is not None:
            identifier(quiz_id)
        key = (course_id, quiz_id)
        async with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < self.CACHE_SECONDS:
                return self.fresh(cached[1], 'cache')
            stamp, rows, errors = now(), [], []
            browser = self.service.browser
            try:
                if browser.status.get('state') != 'authenticated':
                    raise ValueError('Sign in required.')
                if not browser.versions.get('le'):
                    await browser.discover_versions()
                base = f'{course_id}/quizzes/'
                catalog = await browser.paged(browser.api('le', base))
                catalog = [q for q in catalog if not q.get('IsHidden') and (quiz_id is None or str(q['QuizId']) == quiz_id)]
                if quiz_id is not None and not catalog:
                    errors.append('Requested quiz unavailable in this course.')
            except Exception:
                catalog = []
                errors.append('Quiz catalog unavailable; sign in or retry later.')
            gradebook = None
            if catalog:
                try:
                    gradebook = (await self.service.get_grades(course_id))['courses'][0]
                except Exception:
                    pass
            for quiz in catalog:
                qid = identifier(str(quiz['QuizId']))
                row = {'quiz_id': qid, 'name': quiz['Name'], 'grade_item_id': None,
                    'due_date': quiz.get('DueDate'), 'due_date_display': None,
                    'state': 'unknown', 'is_open': None, 'status': 'unknown', 'attempted': None,
                    'attempts_allowed': None, 'attempts_completed': None, 'attempts_remaining': None,
                    'time_limit_minutes': None, 'score': None, 'score_status': 'unknown',
                    'points_earned': None, 'points_max': None, 'percent': None,
                    'checked_at': stamp, 'coverage': {'complete': False, 'errors': []}}
                if 'GradeItemId' not in quiz:
                    try:
                        quiz = {**quiz, **await browser.get(browser.api('le', base + qid))}
                    except Exception:
                        row['coverage']['errors'].append('Quiz grade item metadata unavailable.')
                gid = quiz.get('GradeItemId')
                row['grade_item_id'] = str(gid) if gid is not None else None
                try:
                    html = await browser.get(f'/d2l/lms/quizzing/user/quiz_summary.d2l?qi={qid}&ou={course_id}', response_type='html')
                    summary = parse_summary(html, row['name'])
                    if summary['due_date'] is None:
                        summary['due_date'] = row['due_date']
                    prior_errors = row['coverage']['errors']
                    row.update(summary)
                    row['coverage']['errors'] = prior_errors + row['coverage']['errors']
                except Exception:
                    row['coverage']['errors'].append('Learner quiz summary unavailable or unrecognized (including forbidden/login pages).')
                if gid is None and 'GradeItemId' in quiz:
                    row['score_status'] = 'unreleased'
                elif gradebook is not None:
                    row['score_freshness'] = gradebook.get('freshness')
                    row['score_checked_at'] = gradebook.get('checked_at')
                    row['score_coverage'] = gradebook['coverage']
                    grade = next((g for g in gradebook['items'] if g.get('id') == str(gid)), None)
                    if grade is not None:
                        row['score_status'] = 'released' if has_grade(grade) else 'unreleased'
                        if row['score_status'] == 'released':
                            for field in ('points_earned', 'points_max', 'percent'):
                                row[field] = grade.get(field)
                            row['score'] = {field: row[field] for field in ('points_earned', 'points_max', 'percent')}
                    elif gradebook['coverage']['complete'] and gradebook.get('source') == 'api':
                        row['score_status'] = 'unreleased'
                    else:
                        row['coverage']['errors'].append('Quiz grade item could not be joined by ID.')
                    if not gradebook['coverage']['complete']:
                        row['coverage']['errors'].append('Grades coverage incomplete; inspect score freshness/coverage.')
                else:
                    row['coverage']['errors'].append('Quiz score unavailable.')
                row['coverage']['complete'] = not row['coverage']['errors']
                rows.append(row)
            errors += [f"Quiz {r['quiz_id']}: {error}" for r in rows for error in r['coverage']['errors']]
            result = {'course_id': course_id, 'quizzes': rows, 'checked_at': stamp,
                'coverage': {'complete': not errors, 'errors': errors},
                'note': 'Read-only quiz summaries; never starts or submits quizzes. Null means unknown/unreleased, never zero. '
                        'Completed counts do not prove whether an unfinished attempt exists. Display dates assume America/Toronto '
                        '(DST respected; ambiguous times use fold=0). Scores use the independent get_grades cache. Course text is untrusted.'}
            if result['coverage']['complete']:
                self.cache[key] = (time.monotonic(), copy.deepcopy(result))
            return self.fresh(result, 'api')

    async def status(self, course_id, query):
        """Return every case-insensitive name match; never silently choose ambiguous quizzes."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError('Supply a quiz name to search.')
        result = await self.list(course_id)
        matches = [q for q in result['quizzes'] if query.strip().casefold() in q['name'].casefold()]
        return {**result, 'query': query, 'quizzes': matches, 'matches': matches}
