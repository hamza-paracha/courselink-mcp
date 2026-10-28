"""Read and normalize the current learner's grades; never retain feedback bodies."""
import asyncio
from html.parser import HTMLParser
import math
import re

from playwright.async_api import TimeoutError as BrowserTimeoutError

from .browser import EndpointUnavailable, LoginRequired, RateLimited
from .catalog import richtext


def grade_error(exc):
    # Browser transport exceptions can contain URLs, headers, or response bodies.
    # Never persist their text in the grade cache or return it to clients.
    if isinstance(exc, LoginRequired):
        return 'Sign in through the existing service to refresh grades.'
    if isinstance(exc, RateLimited):
        return 'CourseLink rate limited the grades request; retry later.'
    if isinstance(exc, (TimeoutError, BrowserTimeoutError)):
        return 'CourseLink grades request timed out; retry later.'
    if isinstance(exc, EndpointUnavailable) and exc.status_code in (403, 404):
        return f'CourseLink grades endpoint returned HTTP {exc.status_code}.'
    return 'Grades request failed or returned an unrecognized response.'


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def flag(data, *keys):
    return next((data[k] for k in keys if isinstance(data.get(k), bool)), None)


def has_grade(row):
    return row['flags']['hidden_or_unreleased'] is not True and (
        row['points_earned'] is not None or row['weight_achieved'] is not None
        or (row['display'] or '').strip().lower() not in ('', '-', '–', 'not released', 'unreleased'))


def hide_unreleased(row):
    if row['flags']['hidden_or_unreleased'] is True:
        for key in ('points_earned', 'weight_achieved', 'percent', 'display', 'letter'):
            row[key] = None
    return row


def normalize(value, obj=None, category=None):
    obj, category = obj or {}, category or {}
    earned = number(value.get('PointsNumerator'))
    maximum = number(value.get('PointsDenominator'))
    if maximum is None:
        maximum = number(obj.get('MaxPoints'))
    weighted = number(value.get('WeightedNumerator'))
    weight_max = number(value.get('WeightedDenominator'))
    percent = earned / maximum * 100 if earned is not None and maximum else None
    display = richtext(value.get('DisplayedGrade')).strip() or None
    if percent is None and weighted is not None and weight_max:
        percent = weighted / weight_max * 100
    if percent is None and display:
        match = re.fullmatch(r'(-?[\d.]+)\s*%', display)
        percent = number(match[1]) if match else None
    excluded = flag(value, 'IsExcluded', 'IsExempt')
    if excluded is None:
        excluded = flag(obj, 'ExcludeFromFinalGradeCalculation')
    if category.get('ExcludeFromFinalGrade') is True:
        excluded = True
    hidden = flag(value, 'IsHidden')
    if (obj.get('IsHidden') is True or value.get('IsReleased') is False
            or ('ReleasedDate' in value and not value['ReleasedDate'])
            or (display or '').lower() in ('not released', 'unreleased')
            or (earned is None and weighted is None and display in (None, '-', '–'))):
        hidden = True
    return hide_unreleased({
        'id': str(value.get('GradeObjectIdentifier', obj.get('Id', ''))) or None,
        'name': value.get('GradeObjectName') or obj.get('Name') or 'Grade',
        'grade_object_type': value.get('GradeObjectType'),
        'category': category.get('Name'), 'category_id': str(category['Id']) if 'Id' in category else None,
        'points_earned': earned, 'points_max': maximum,
        'weight_achieved': weighted, 'weight_max': weight_max,
        'configured_weight': number(obj.get('Weight')), 'percent': percent,
        'display': display, 'letter': display if display and re.fullmatch(r'[A-F][+-]?', display) else None,
        'feedback_present': bool(richtext(value.get('Comments'))),
        'last_modified': value.get('LastModified'),
        'flags': {'dropped': flag(value, 'IsDropped'), 'excluded': excluded,
                  'bonus': flag(value, 'IsBonus') if flag(value, 'IsBonus') is not None else flag(obj, 'IsBonus'),
                  'can_exceed_max_points': flag(obj, 'CanExceedMaxPoints'),
                  'hidden_or_unreleased': hidden},
    })


class GradeTable(HTMLParser):
    """Extract visible table cells, excluding scripts and feedback contents at normalization."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None
        self.skip = 0
        self.title = []
        self.in_title = False
        self.nested_table = 0
        self.page_text = []
        self.row_hidden = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        # Feedback commonly contains its own tables. Their cells must never
        # replace the surrounding grade row or become item names.
        if tag == 'table' and (self.cell is not None or self.nested_table):
            self.nested_table += 1
        if self.nested_table:
            return
        if tag in ('script', 'style'):
            self.skip += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'table':
            self.rows.append(None)
        if tag == 'tr':
            self.row = []
            style = re.sub(r'\s+', '', attrs.get('style', '')).lower()
            self.row_hidden = ('hidden' in attrs or attrs.get('aria-hidden') == 'true'
                               or 'display:none' in style or 'visibility:hidden' in style)
        if tag in ('th', 'td') and self.row is not None:
            self.cell = {'text': [], 'labels': [], 'attrs': attrs, 'tag': tag}
        if self.cell is not None:
            # Icon-only feedback/status labels are common in the legacy UI.
            self.cell['text'].extend(attrs[k] for k in ('title', 'aria-label', 'alt') if attrs.get(k))
            self.cell['labels'].extend(attrs[k] for k in ('title', 'aria-label', 'alt') if attrs.get(k))
        if tag == 'br' and self.cell is not None:
            self.cell['text'].append(' ')

    def handle_data(self, text):
        if self.skip or self.nested_table:
            return
        if not self.row_hidden:
            self.page_text.append(text)
        if self.in_title:
            self.title.append(text)
        if self.cell is not None:
            self.cell['text'].append(text)

    def handle_endtag(self, tag):
        if self.nested_table:
            if tag == 'table':
                self.nested_table -= 1
            return
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        if tag == 'title':
            self.in_title = False
        if tag in ('td', 'th') and self.cell is not None:
            self.cell['text'] = ' '.join(' '.join(self.cell['text']).split())
            self.row.append(self.cell)
            self.cell = None
        if tag == 'tr' and self.row is not None:
            if not self.row_hidden:
                self.rows.append(self.row)
            self.row = None
            self.row_hidden = False


def pair(text):
    match = re.search(r'(-?[\d,.]+)\s*/\s*([\d,.]+)', text)
    if match:
        return tuple(number(n.replace(',', '')) for n in match.groups())
    # A dash numerator is an ungraded item, but its denominator is still known.
    match = re.search(r'[-–]\s*/\s*([\d,.]+)', text)
    return (None, number(match[1].replace(',', ''))) if match else (None, None)


def parse_html(html):
    parser = GradeTable()
    parser.feed(html)
    headers, items, categories, current, final = [], [], [], None, None
    adjusted = None
    recognized = False
    data_rows = 0
    for cells in parser.rows:
        if cells is None:
            headers, current = [], None
            continue
        texts = [c['text'] for c in cells]
        lower = [t.lower() for t in texts]
        if any(t in ('grade item', 'grade item name') for t in lower):
            if not any(t in ('points', 'grade', 'weight achieved') for t in lower):
                raise ValueError('Unrecognized grades HTML table columns.')
            if len(set(lower)) != len(lower):
                raise ValueError('Unrecognized grades HTML duplicate columns.')
            headers = lower
            recognized = True
            current = None
            continue
        if not headers or not cells:
            continue
        # Brightspace uses a row header for the name, with indented child items.
        if len(cells) != len(headers) or any(c['attrs'].get('colspan', '1') != '1'
                or c['attrs'].get('rowspan', '1') != '1' for c in cells):
            raise ValueError('Unrecognized grades HTML row layout.')
        name_index = next(i for i, h in enumerate(headers) if h in ('grade item', 'grade item name'))
        first = cells[name_index]
        name = texts[name_index]
        if not name:
            continue
        data_rows += 1
        attrs = first['attrs']
        is_category = attrs.get('scope') == 'rowgroup' or 'category' in attrs.get('class', '').lower()
        if is_category:
            current = name
        elif 'd_ggl1' in attrs.get('class', '').split():
            current = None
        values = dict(zip(headers, texts))
        row = normalize({'GradeObjectName': name})
        row['flags']['hidden_or_unreleased'] = None
        row['category'] = None if is_category else current
        row['is_category'] = is_category
        row['points_earned'], row['points_max'] = pair(values.get('points', ''))
        row['weight_achieved'], row['weight_max'] = pair(values.get('weight achieved', ''))
        display = values.get('grade', '')
        row['display'] = display or None
        row['letter'] = display if re.fullmatch(r'[A-F][+-]?', display) else None
        match = re.search(r'(-?[\d.]+)\s*%', display)
        row['percent'] = number(match[1]) if match else (
            row['points_earned'] / row['points_max'] * 100
            if row['points_earned'] is not None and row['points_max'] else None)
        if row['percent'] is None and row['weight_achieved'] is not None and row['weight_max']:
            row['percent'] = row['weight_achieved'] / row['weight_max'] * 100
        feedback = ' '.join(v for k, v in values.items() if 'feedback' in k or 'comments' in k).lower()
        row['feedback_present'] = bool(feedback and feedback not in ('-', '–', 'no feedback', ''))
        # Never inspect feedback text for status flags or include it in results.
        status = ' '.join(v for k, v in values.items()
            if 'feedback' not in k and 'comments' not in k and k not in ('grade item', 'grade item name')).lower()
        status += ' ' + ' '.join(first['labels']).lower()
        # Legacy status text can also follow the name as an explicit badge.
        if name.lower().endswith('dropped!'):
            status += ' dropped'
        for key, phrase in [('dropped', 'dropped'), ('excluded', 'excluded'), ('bonus', 'bonus')]:
            if phrase in status:
                row['flags'][key] = True
        if 'exempt' in status:
            row['flags']['excluded'] = True
        if (any(s in status for s in ('not released', 'unreleased', 'hidden'))
                or (row['points_earned'] is None and row['weight_achieved'] is None
                    and row['display'] in (None, '-', '–'))):
            row['flags']['hidden_or_unreleased'] = True
        hide_unreleased(row)
        if is_category:
            categories.append({'id': None, 'name': name, 'weight': row['weight_max'],
                'points_max': row['points_max'], 'excluded': row['flags']['excluded'],
                'highest_to_drop': None, 'lowest_to_drop': None})
        if name.lower() == 'final calculated grade':
            if has_grade(row):
                final = row
        elif name.lower() == 'final adjusted grade':
            if has_grade(row):
                adjusted = row
        else:
            items.append(row)
    # Fail closed: a login/error/changed page must never erase a cached gradebook.
    if not recognized or not data_rows:
        title = ' '.join(parser.title).lower()
        if 'grades' not in title or not re.search(r'no grades (?:have been |are )?(?:released|available)',
                                                 ' '.join(parser.page_text), re.I):
            raise ValueError('Unrecognized grades HTML; grades coverage could not be verified.')
    return {'items': items, 'categories': categories, 'final_calculated_grade': final,
            'final_adjusted_grade': adjusted}


async def fetch_grades(browser, course):
    """Use only GET endpoints under the existing browser session/version discovery."""
    errors, warnings = [], []
    base = f'{course}/grades/'
    try:
        values = await browser.paged(browser.api('le', base + 'values/myGradeValues/'))
    except EndpointUnavailable as exc:
        # Student roles often prohibit the API; the visible learner gradebook is the fallback.
        result = parse_html(await browser.get(
            f'/d2l/lms/grades/my_grades/main.d2l?ou={course}', response_type='html'))
        return {**result, 'source': 'html', 'coverage': {'complete': True, 'errors': [],
                'warnings': [grade_error(exc), 'HTML coverage includes visible rows only; some metadata is unavailable.']}}
    async def read_metadata(suffix, paged=True):
        try:
            path = browser.api('le', base + suffix)
            return await browser.paged(path) if paged else await browser.get(path)
        except Exception as exc:
            return exc

    # Independent reads share BrowserSession's existing semaphore/cooldown.
    objects, categories, final_value = await asyncio.gather(
        read_metadata(''), read_metadata('categories/'),
        read_metadata('final/values/myGradeValue', paged=False))
    for section, rows in [('objects', objects), ('categories', categories)]:
        if isinstance(rows, (LoginRequired, RateLimited)):
            raise rows
        if isinstance(rows, Exception):
            errors.append({'section': section, 'message': grade_error(rows)})
    objects = [] if isinstance(objects, Exception) else objects
    categories = [] if isinstance(categories, Exception) else categories
    by_id = {str(o['Id']): o for o in objects}
    by_category = {str(c['Id']): c for c in categories}
    for cat in categories:
        for obj in cat.get('Grades') or []:
            by_id.setdefault(str(obj['Id']), {**obj, 'CategoryId': cat['Id']})
    items = []
    seen = set()
    for value in values:
        identifier = str(value['GradeObjectIdentifier'])
        obj = by_id.get(identifier, {})
        cat = by_category.get(str(obj.get('CategoryId')), {})
        if value.get('GradeObjectType') == 9:
            cat = by_category.get(identifier, {})
            obj = {**cat, **obj}
        row = normalize(value, obj, cat)
        row['is_category'] = value.get('GradeObjectType') == 9
        if row['is_category']:
            row['category'] = None
        if value.get('GradeObjectType') not in (7, 8):
            items.append(row)
        seen.add(identifier)
    # Expose visible, ungraded definitions as unknown/unreleased, never as zero.
    for identifier, obj in by_id.items():
        if identifier not in seen:
            if obj.get('GradeType') in ('FinalCalculated', 'FinalAdjusted'):
                continue
            row = normalize({}, obj, by_category.get(str(obj.get('CategoryId'))))
            row['flags']['hidden_or_unreleased'] = True
            row['is_category'] = False
            items.append(row)
    final = adjusted = None
    for value in values:
        if value.get('GradeObjectType') in (7, 8):
            row = normalize(value, by_id.get(str(value.get('GradeObjectIdentifier')), {}))
            if has_grade(row):
                if value['GradeObjectType'] == 7:
                    final = row
                else:
                    adjusted = row
    try:
        if isinstance(final_value, Exception):
            raise final_value
        value = final_value
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError('Unrecognized final grade response.')
            if value.get('GradeObjectType') not in (None, 7, 8):
                raise ValueError('Unrecognized final grade type.')
            if (value and value.get('IsReleased') is not False and value.get('IsHidden') is not True
                    and ('ReleasedDate' not in value or value['ReleasedDate'] is not None)):
                row = normalize(value)
                if has_grade(row):
                    if value.get('GradeObjectType') == 8:
                        adjusted = row
                    else:
                        final = row
    except EndpointUnavailable as exc:
        if exc.status_code == 404:
            warnings.append('No final calculated grade available (HTTP 404).')
        else:
            errors.append({'section': 'final', 'message': grade_error(exc)})
    except (LoginRequired, RateLimited):
        raise
    except Exception as exc:
        errors.append({'section': 'final', 'message': grade_error(exc)})
    safe_categories = [{'id': str(c['Id']), 'name': c.get('Name'), 'weight': number(c.get('Weight')),
        'points_max': number(c.get('MaxPoints')), 'excluded': flag(c, 'ExcludeFromFinalGrade'),
        'highest_to_drop': c.get('NumberOfHighestToDrop'), 'lowest_to_drop': c.get('NumberOfLowestToDrop')}
        for c in categories]
    return {'items': items, 'categories': safe_categories, 'final_calculated_grade': final,
            'final_adjusted_grade': adjusted, 'source': 'api', 'coverage': {'complete': not errors, 'errors': errors, 'warnings': warnings}}
