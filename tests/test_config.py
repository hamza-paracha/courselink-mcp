import json
import stat

from courselink_mcp.config import Config


def test_new_installation_has_no_courses_and_keeps_downloads_in_private_state(tmp_path, monkeypatch):
    state = tmp_path / 'private-state'
    monkeypatch.setenv('COURSELINK_STATE_DIR', str(state))
    config = Config.load()
    assert config.courses == {}
    assert config.school == state / 'downloads'
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / 'config.json').stat().st_mode) == 0o600
    token = config.token()
    assert len(token) >= 32
    assert config.token() == token
    assert stat.S_IMODE((state / 'server-token').stat().st_mode) == 0o600


def test_existing_user_configuration_survives_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv('COURSELINK_STATE_DIR', str(tmp_path))
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'school': str(tmp_path / 'custom-downloads'),
                                'courses': {'123456': 'example-course'}}))
    original = path.read_bytes()
    config = Config.load()
    assert config.courses == {'123456': 'example-course'}
    assert config.school == tmp_path / 'custom-downloads'
    assert path.read_bytes() == original


def test_course_configuration_is_atomic_and_preserves_settings(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('COURSELINK_STATE_DIR', str(tmp_path))
    config = Config.load()
    config.update_courses({'123456': 'example-course'})
    original = (tmp_path / 'config.json').read_bytes()
    with pytest.raises(ValueError):
        config.update_courses({'123457': '../outside'})
    assert (tmp_path / 'config.json').read_bytes() == original
    config.update_courses({'123457': 'second-course'})
    saved = Config.load()
    assert saved.courses == {'123456': 'example-course', '123457': 'second-course'}
    assert saved.school == config.school and saved.poll_seconds == config.poll_seconds
    assert stat.S_IMODE((tmp_path / 'config.json').stat().st_mode) == 0o600


def test_existing_private_file_modes_are_repaired_without_replacing_contents(tmp_path, monkeypatch):
    monkeypatch.setenv('COURSELINK_STATE_DIR', str(tmp_path))
    config = Config.load()
    token = config.token()
    auth = tmp_path / 'browser-auth.json'
    auth.write_text('{"cookies": []}')
    files = [tmp_path / 'config.json', tmp_path / 'server-token', auth]
    original = [p.read_bytes() for p in files]
    for path in files:
        path.chmod(0o644)
    assert Config.load().token() == token
    assert [p.read_bytes() for p in files] == original
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)


def test_private_file_symlinks_are_rejected_before_writing(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('COURSELINK_STATE_DIR', str(tmp_path))
    target = tmp_path / 'must-not-create'
    (tmp_path / 'config.json').symlink_to(target)
    with pytest.raises(ValueError, match='regular file'):
        Config.load()
    assert not target.exists()


def test_invalid_persisted_values_fail_at_load(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('COURSELINK_STATE_DIR', str(tmp_path))
    config = Config.load()
    path = tmp_path / 'config.json'
    original = json.loads(path.read_text())
    for field, value in [('headless', 'false'), ('max_file_bytes', -1), ('port', True),
                         ('poll_seconds', '300'), ('keepalive_seconds', 30.5),
                         ('file_check_seconds', 0), ('download_concurrency', 5)]:
        path.write_text(json.dumps({**original, field: value}))
        with pytest.raises(ValueError):
            Config.load()
    for invalid in [[], {**original, 'school': None}, {**original, 'unknown_setting': 1},
                    {**original, 'courses': {'1': 'same', '2': 'SAME'}}]:
        path.write_text(json.dumps(invalid))
        with pytest.raises(ValueError):
            Config.load()
    path.write_text(json.dumps(original))
    (tmp_path / 'server-token').write_text('')
    with pytest.raises(ValueError, match='empty'):
        config.token()
