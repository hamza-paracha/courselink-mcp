import base64
import hashlib
import io
from pathlib import Path
import zipfile

import pytest

from courselink_mcp.computer import Computer
from courselink_mcp.config import Config
from courselink_mcp.results import Results


def payload(member='src/main.c'):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(member, 'int main(void) { return 0; }\n')
    data = buffer.getvalue()
    return base64.b64encode(data).decode(), hashlib.sha256(data).hexdigest()


@pytest.fixture
def mailbox(tmp_path):
    config = Config(state=tmp_path, school=tmp_path / 'school', courses={'1': 'example'})
    return Results(config)


def save(mailbox, **overrides):
    data, digest = payload()
    args = dict(course_id='1', task='labs/lab4', archive_base64=data, sha256=digest,
                report='Required build and sample checks passed.', source_item_ids=['1:content:2'])
    return mailbox.save(**{**args, **overrides})


def test_immutable_result_retries_and_checked_bytes(mailbox):
    first, second = save(mailbox), save(mailbox)
    assert first == second
    assert len(mailbox.list()['results']) == 1
    data = base64.b64decode(mailbox.read(first['id'])['base64'])
    assert hashlib.sha256(data).hexdigest() == first['sha256']
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ['src/main.c']


@pytest.mark.parametrize('overrides', [
    {'course_id': '2'}, {'task': '../escape'}, {'task': 'labs/lab4/../../evil'},
    {'sha256': '0' * 64}, {'source_item_ids': ['2:content:2']}, {'report': ''},
])
def test_invalid_result_not_published(mailbox, overrides):
    with pytest.raises(ValueError):
        save(mailbox, **overrides)
    assert mailbox.list()['results'] == []


@pytest.mark.parametrize('member', ['../escape', '/absolute', 'src/../../escape', 'src\\escape'])
def test_unsafe_archive_rejected(mailbox, member):
    encoded, digest = payload(member)
    with pytest.raises(ValueError, match='Unsafe'):
        save(mailbox, archive_base64=encoded, sha256=digest)


async def test_laptop_download_is_verified_idempotent_and_preserves_edits(mailbox):
    result = save(mailbox)
    async def call(operation, args):
        return mailbox.list() if operation == 'results' else mailbox.read(**args)
    computer = Computer(mailbox.config, call)
    first = await computer.sync_results()
    assert first['complete'] and first['files'][0]['saved']
    path = Path(first['files'][0]['path'])
    assert path.parent == mailbox.config.school / 'example/CourseLink Downloads/labs/lab4/submission'
    assert (await computer.sync_results())['files'][0]['saved'] is False
    path.write_bytes(b'my edits')
    assert not (await computer.sync_results())['complete']
    assert path.read_bytes() == b'my edits'


async def test_corrupt_transfer_is_never_published(mailbox):
    save(mailbox)
    async def call(operation, args):
        if operation == 'results':
            return mailbox.list()
        return {'base64': base64.b64encode(b'wrong').decode(), 'offset': 0,
                'next_offset': 5, 'eof': True}
    result = await Computer(mailbox.config, call).sync_results()
    assert not result['complete']
    assert not list(mailbox.config.school.rglob('*.zip'))
    assert not list(mailbox.config.school.rglob('*.partial'))
