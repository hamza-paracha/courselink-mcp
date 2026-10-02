"""Bounded document extraction. Runs in a separate process, never executes course files."""
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 300_000
EXTRACTOR_VERSION = 3


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        if tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)


def extract_bytes(data, filename, member=None):
    if len(data) > MAX_BYTES:
        return {'text': '', 'status': 'too_large_for_text', 'warning': 'Retrieve the original file for inspection.'}
    suffix = Path(filename).suffix.lower()
    warnings, text = [], ''
    result = {'status': 'ok', 'format': suffix.lstrip('.')}
    if suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(''):
            return {'text': '', 'status': 'encrypted', 'warning': 'PDF requires a password.'}
        total = len(reader.pages)
        result['page_count'] = total
        empty_pages, parts = [], []
        count = 0
        for index, page in enumerate(reader.pages[:200]):
            page_text = page.extract_text() or ''
            if not page_text.strip():
                empty_pages.append(index + 1)
            parts.append(f'\n--- Page {index + 1} ---\n' + page_text)
            count += len(parts[-1])
            if count > MAX_TEXT:
                warnings.append('Text limit reached; inspect the original for the remaining pages.')
                break
        if total > 200:
            warnings.append('Only the first 200 PDF pages were extracted.')
        if empty_pages:
            result['pages_without_text'] = empty_pages
            warnings.append('Some pages have no extractable text; visual inspection or OCR may be needed.')
        text = ''.join(parts)
    elif suffix in ('.docx', '.pptx'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = ['word/document.xml'] if suffix == '.docx' else sorted(
                n for n in archive.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml'))
            parts = []
            for name in names[:200]:
                info = archive.getinfo(name)
                if info.file_size > MAX_BYTES:
                    raise ValueError('Office document entry exceeds size limit.')
                root = ET.fromstring(archive.read(info))
                parts.append('\n'.join(node.text or '' for node in root.iter() if node.tag.endswith('}t')))
            text = '\n'.join(parts)
    elif suffix == '.xlsx':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            def xml(name):
                if archive.getinfo(name).file_size > MAX_BYTES:
                    raise ValueError('Spreadsheet XML exceeds the extraction limit.')
                return ET.fromstring(archive.read(name))
            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings = [''.join(node.text or '' for node in entry.iter() if node.tag.endswith('}t'))
                           for entry in xml('xl/sharedStrings.xml')]
            parts, cells = [], 0
            for name in sorted(n for n in archive.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'))[:100]:
                parts.append('\n--- ' + name + ' ---')
                for cell in xml(name).iter():
                    if not cell.tag.endswith('}c'):
                        continue
                    value, formula = '', ''
                    for node in cell.iter():
                        if node.tag.endswith('}v'):
                            value = node.text or ''
                        elif node.tag.endswith('}f'):
                            formula = node.text or ''
                        elif cell.get('t') == 'inlineStr' and node.tag.endswith('}t'):
                            value += node.text or ''
                    if cell.get('t') == 's' and value:
                        value = strings[int(value)]
                    parts.append(cell.get('r', '') + ': ' + (('=' + formula + ' => ') if formula else '') + value)
                    cells += 1
                    if cells >= 20000:
                        warnings.append('Spreadsheet limited to 20,000 cells.')
                        break
                if cells >= 20000:
                    break
            text = '\n'.join(parts)
            warnings.append('Raw cell values only. Formatting, charts and images need visual review; formulas use stored values and are not recalculated.')
    elif suffix == '.zip':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir() and not entry.filename.startswith('__MACOSX/')]
            result['entries'] = [{'name': e.filename, 'bytes': e.file_size} for e in entries[:500]]
            if len(entries) > 500:
                warnings.append('Archive listing limited to 500 entries.')
            if member:
                entry = archive.getinfo(member)
                if entry.file_size > MAX_BYTES or entry.file_size > max(entry.compress_size, 1) * 200:
                    raise ValueError('Archive entry is too large or too highly compressed.')
                if member.lower().endswith('.zip'):
                    raise ValueError('Nested ZIP extraction is not supported.')
                child = extract_bytes(archive.read(entry), member)
                child['archive_member'] = member
                return child
            parts = ['Archive files:\n' + '\n'.join(e.filename for e in entries[:500])]
            total_bytes, total_chars, indexed = 0, len(parts[0]), 0
            # Put likely instructions before starter code when an archive exceeds extraction limits.
            priority = lambda e: (not any(w in e.filename.lower() for w in ('readme', 'instruct', 'rubric', 'grading')), e.filename)
            for entry in sorted(entries, key=priority)[:50]:
                if entry.file_size > MAX_BYTES or entry.file_size > max(entry.compress_size, 1) * 200:
                    warnings.append(f'Skipped oversized entry: {entry.filename}')
                    continue
                if total_bytes + entry.file_size > MAX_BYTES or total_chars >= MAX_TEXT:
                    warnings.append('Archive text limit reached; inspect individual entries as needed.')
                    break
                if entry.filename.lower().endswith('.zip'):
                    continue
                child = extract_bytes(archive.read(entry), entry.filename)
                total_bytes += entry.file_size
                if child.get('text'):
                    part = '\n--- ' + entry.filename + ' ---\n' + child['text']
                    parts.append(part)
                    total_chars += len(part)
                    indexed += 1
                if child.get('warnings'):
                    warnings.extend(f'{entry.filename}: {warning}' for warning in child['warnings'])
            text = '\n'.join(parts)
            result['status'] = 'archive_summary'
            result['entries_with_text'] = indexed
            warnings.append('Use archive_member with an exact entry name to inspect a specific file. Binary and nested-archive contents are not indexed.')
            if len(entries) > 50:
                warnings.append('At most 50 archive entries were considered for text extraction.')
    elif suffix in ('.html', '.htm'):
        parser = HTMLText()
        parser.feed(data.decode('utf-8', errors='replace'))
        text = ''.join(parser.parts)
    elif suffix in ('.txt', '.md', '.csv', '.json', '.xml', '.java', '.c', '.h', '.cpp', '.py', '.js', '.ts', '.sh', '.yaml', '.yml', '.x68', '.asm', '.s') or filename.rsplit('/', 1)[-1] in ('Makefile', 'README'):
        text = data.decode('utf-8-sig', errors='replace')
    else:
        result['status'] = 'unsupported_format'
        warnings.append('Retrieve the original file for inspection; no text extractor is available for this format.')
    result['text'] = text[:MAX_TEXT]
    result['truncated'] = len(text) > MAX_TEXT
    result['warnings'] = warnings
    return result


def main():
    if sys.platform.startswith('linux'):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    path = Path(sys.argv[1])
    try:
        if path.stat().st_size > MAX_BYTES:
            result = {'status': 'too_large_for_text', 'text': '', 'warning': 'Retrieve the original file.'}
        else:
            result = extract_bytes(path.read_bytes(), path.name, sys.argv[2] if len(sys.argv) > 2 else None)
    except Exception as exc:
        result = {'status': 'extraction_error', 'text': '', 'warning': str(exc)[:300]}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
