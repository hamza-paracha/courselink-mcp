import io
import zipfile

import pytest

from courselink_mcp.extract import extract_bytes
from courselink_mcp.store import Store


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as zip:
        for name, data in files.items():
            zip.writestr(name, data)
    return output.getvalue()


def test_zip_instructions_and_code_are_read_without_executing():
    data = archive({'instructions.md': '# Lab 1\nSubmit Friday.', 'main.c': '#include <stdio.h>', '../outside.txt': 'text'})
    listing = extract_bytes(data, 'Lab1.zip')
    assert listing['status'] == 'archive_summary'
    assert len(listing['entries']) == 3
    assert 'Submit Friday' in listing['text']
    assert 'Submit Friday' in extract_bytes(data, 'Lab1.zip', 'instructions.md')['text']
    # Even an unsafe archive path is read in-memory, never extracted onto disk.
    assert extract_bytes(data, 'Lab1.zip', '../outside.txt')['text'] == 'text'


def test_nested_zip_and_binary_files_report_limits():
    data = archive({'nested.zip': b'not a zip'})
    with pytest.raises(ValueError, match='Nested'):
        extract_bytes(data, 'outer.zip', 'nested.zip')
    assert extract_bytes(b'abc', 'video.mp4')['status'] == 'unsupported_format'


def test_docx_and_html_text():
    data = archive({'word/document.xml': '<w:document xmlns:w="test"><w:p><w:t>Assignment 1</w:t></w:p></w:document>'})
    assert extract_bytes(data, 'instructions.docx')['text'] == 'Assignment 1'
    result = extract_bytes(b'<h1>Lab 2</h1><script>bad()</script><p>Due Friday</p>', 'lab.html')
    assert 'Lab 2' in result['text'] and 'Due Friday' in result['text'] and 'bad' not in result['text']


def test_assembly_and_spreadsheet_cells():
    assert 'MOVE' in extract_bytes(b'MOVE D0,D1', 'lab.X68')['text']
    data = archive({'xl/sharedStrings.xml': '<sst xmlns="sheet"><si><t>Assignment</t></si></sst>',
                    'xl/worksheets/sheet1.xml': '<worksheet xmlns="sheet"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><f>2+2</f><v>4</v></c></row></sheetData></worksheet>'})
    result = extract_bytes(data, 'rubric.xlsx')
    assert 'A1: Assignment' in result['text']
    assert 'B1: =2+2 => 4' in result['text']
    assert result['warnings']


def test_search_only_returns_latest_available_versions(tmp_path):
    store = Store(tmp_path / 'db')
    item = {'id':'1:content:2','course_id':'1','kind':'content','title':'Lab 1','category':'lab'}
    store.reconcile('1','content',[item])
    for index, word in enumerate(['old instructions','new instructions']):
        file = tmp_path / f'temp{index}'
        file.write_text(word)
        version, _ = store.record_file(item,file,tmp_path/f'saved{index}',str(index),len(word))
        store.save_document(version['id'], {'status':'ok','text':word})
    assert not store.search_documents('old')['matches']
    assert len(store.search_documents('new')['matches']) == 1
    assert store.items(category='lab')['total'] == 1
    store.reconcile('1','content',[])
    assert not store.search_documents('new')['matches']


def test_search_coverage_matches_course_filter_and_empty_results(tmp_path):
    store = Store(tmp_path / 'db')
    for course in ('1', '2'):
        item = {'id': f'{course}:content:1', 'course_id': course, 'kind': 'content', 'title': 'Instructions'}
        store.reconcile(course, 'content', [item])
        file = tmp_path / f'temp{course}'
        file.write_text('instructions')
        version, _ = store.record_file(item, file, tmp_path / f'saved{course}', course, 12)
        if course == '1':
            store.save_document(version['id'], {'status': 'ok', 'text': 'instructions'})
    assert store.search_documents('instructions')['index_coverage'] == {'total': 2, 'processed': 1}
    assert store.search_documents('instructions', course_id='1')['index_coverage'] == {'total': 1, 'processed': 1}
    assert store.search_documents('instructions', course_id='2')['index_coverage'] == {'total': 1, 'processed': 0}
    assert store.search_documents('instructions', course_id='3')['index_coverage'] == {'total': 0, 'processed': 0}
    store.close()


async def test_cached_document_does_not_wait_for_parser_lock(tmp_path):
    import asyncio
    from courselink_mcp.config import Config
    from courselink_mcp.service import Service
    from courselink_mcp.extract import EXTRACTOR_VERSION
    service = Service(Config(state=tmp_path, school=tmp_path))
    cached = {'status': 'ok', 'text': 'Saved instructions', 'extractor_version': EXTRACTOR_VERSION}
    service.store.save_document(1, cached)
    try:
        async with service.extract_lock:
            result = await asyncio.wait_for(service.extract_document({'id': 1}), 0.5)
        assert result == cached
    finally:
        await service.close()


async def test_waiting_extraction_rechecks_cache_before_starting_worker(tmp_path, monkeypatch):
    import asyncio
    from courselink_mcp.config import Config
    from courselink_mcp.service import Service
    from courselink_mcp.extract import EXTRACTOR_VERSION
    service = Service(Config(state=tmp_path, school=tmp_path))
    cached = {'status': 'ok', 'text': 'Saved instructions', 'extractor_version': EXTRACTOR_VERSION}
    async def forbidden(*args, **kwargs):
        pytest.fail('A second worker must not parse the same cached version')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    try:
        async with service.extract_lock:
            task = asyncio.create_task(service.extract_document({'id': 1}))
            await asyncio.sleep(0)
            assert not task.done()
            service.store.save_document(1, cached)
        assert await asyncio.wait_for(task, 0.5) == cached
    finally:
        await service.close()
