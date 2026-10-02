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
