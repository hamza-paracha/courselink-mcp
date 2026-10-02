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
