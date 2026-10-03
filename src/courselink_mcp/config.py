from dataclasses import dataclass, field, fields, replace
from pathlib import Path
import json
import os
import secrets
import stat
import tempfile


def state_dir() -> Path:
    return Path(os.environ.get('COURSELINK_STATE_DIR',
        '~/Library/Application Support/CourseLink MCP')).expanduser()


def protect_private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f'{path.name} must be a regular file owned by the current user.')
    path.chmod(0o600)


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
    download_concurrency: int = 3
    courses: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls):
        state = state_dir()
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        path = state / 'config.json'
        if not path.exists() and not path.is_symlink():
            settings = {
                'school': str(state / 'downloads'),
                'courses': {},
                'poll_seconds': 300, 'keepalive_seconds': 60,
                'file_check_seconds': 3600, 'headless': False, 'port': 8765,
            }
            path.write_text(json.dumps(settings, indent=2) + '\n')
            path.chmod(0o600)
        protect_private_file(path)
        auth = state / 'browser-auth.json'
        if auth.exists() or auth.is_symlink():
            protect_private_file(auth)
        settings = json.loads(path.read_text())
        if not isinstance(settings, dict):
            raise ValueError('Configuration must be a JSON object.')
        allowed = {f.name for f in fields(cls)} - {'state'}
        if set(settings) - allowed:
            raise ValueError('Configuration contains unsupported fields.')
        if not isinstance(settings.get('school'), str) or not settings['school'].strip():
            raise ValueError('school must be a nonempty directory path.')
        settings['school'] = Path(settings['school']).expanduser().resolve()
        config = cls(state=state, **settings)
        return config.validate()

    def validate(self):
        for name in ('port', 'poll_seconds', 'keepalive_seconds', 'file_check_seconds',
                     'max_file_bytes', 'download_concurrency'):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 2**63 - 1:
                raise ValueError(f'{name} must be a positive integer within the supported range.')
        if type(self.headless) is not bool:
            raise ValueError('headless must be true or false.')
        if self.base_url != 'https://courselink.uoguelph.ca':
            raise ValueError('This integration is restricted to Guelph CourseLink.')
        if self.poll_seconds < 60 or self.keepalive_seconds < 30:
            raise ValueError('Poll at least 60 seconds apart; keepalive at least 30 seconds.')
        if self.file_check_seconds < self.poll_seconds:
            raise ValueError('File checks must be no more frequent than metadata polling.')
        if not 1024 <= self.port <= 65535:
            raise ValueError('Invalid port')
        if type(self.download_concurrency) is not int or not 1 <= self.download_concurrency <= 4:
            raise ValueError('Download concurrency must be between 1 and 4.')
        if not isinstance(self.courses, dict):
            raise ValueError('Courses must map numeric IDs to folder names.')
        for course, folder in self.courses.items():
            if not isinstance(course, str) or not course.isdigit() or not isinstance(folder, str) or not folder or not all(c.isalnum() or c in '-_' for c in folder):
                raise ValueError('Course IDs must be numeric and folder names simple.')
        if len({folder.casefold() for folder in self.courses.values()}) != len(self.courses):
            raise ValueError('Each course needs a unique folder name.')
        return self

    def update_courses(self, courses):
        """Merge course selections without exposing or replacing unrelated settings."""
        candidate = replace(self, courses={**self.courses, **courses}).validate()
        path = self.state / 'config.json'
        settings = json.loads(path.read_text())
        settings['courses'] = candidate.courses
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.state, suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                temporary.chmod(0o600)
                handle.write(json.dumps(settings, indent=2) + '\n')
            temporary.replace(path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        self.courses = candidate.courses

    def token(self) -> str:
        path = self.state / 'server-token'
        if not path.exists() and not path.is_symlink():
            try:
                with path.open('x') as handle:
                    path.chmod(0o600)
                    handle.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
        protect_private_file(path)
        token = path.read_text().strip()
        if not token:
            raise ValueError('server-token is empty; restore a valid private token.')
        return token
