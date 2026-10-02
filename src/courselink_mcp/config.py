from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import secrets


def state_dir() -> Path:
    return Path(os.environ.get('COURSELINK_STATE_DIR',
        '~/Library/Application Support/CourseLink MCP')).expanduser()


@dataclass
class Config:
    state: Path
    school: Path
    base_url: str = 'https://courselink.uoguelph.ca'
    port: int = 8765
    poll_seconds: int = 300
    keepalive_seconds: int = 60
    file_check_seconds: int = 3600
    max_file_bytes: int = 100 * 1024 * 1024
    headless: bool = False
    courses: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls):
        state = state_dir()
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        path = state / 'config.json'
        if not path.exists():
            settings = {
                'school': str(state / 'downloads'),
                'courses': {},
                'poll_seconds': 300, 'keepalive_seconds': 60,
                'file_check_seconds': 3600, 'headless': False, 'port': 8765,
            }
            path.write_text(json.dumps(settings, indent=2) + '\n')
            path.chmod(0o600)
        settings = json.loads(path.read_text())
        settings['school'] = Path(settings['school']).expanduser().resolve()
        config = cls(state=state, **settings)
        if config.base_url != 'https://courselink.uoguelph.ca':
            raise ValueError('This integration is restricted to Guelph CourseLink.')
        if config.poll_seconds < 60 or config.keepalive_seconds < 30:
            raise ValueError('Poll at least 60 seconds apart; keepalive at least 30 seconds.')
        if config.file_check_seconds < config.poll_seconds:
            raise ValueError('File checks must be no more frequent than metadata polling.')
        if not 1024 <= config.port <= 65535:
            raise ValueError('Invalid port')
        for course, folder in config.courses.items():
            if not course.isdigit() or not folder or not all(c.isalnum() or c in '-_' for c in folder):
                raise ValueError('Course IDs must be numeric and folder names simple.')
        return config

    def token(self) -> str:
        path = self.state / 'server-token'
        if not path.exists():
            try:
                with path.open('x') as handle:
                    path.chmod(0o600)
                    handle.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
        return path.read_text().strip()
