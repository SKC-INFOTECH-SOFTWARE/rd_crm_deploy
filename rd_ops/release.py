"""Small, dependency-free release identity shared by web and operations."""
from pathlib import Path
import os


ROOT = Path(__file__).resolve().parents[1]


def version() -> str:
    configured = os.environ.get('RD_RELEASE', '').strip()
    if configured:
        return configured
    path = ROOT / 'VERSION'
    return path.read_text(encoding='utf-8').strip() if path.exists() else 'development'


def revision() -> str:
    return os.environ.get('RD_REVISION', '').strip() or 'local'


def metadata() -> dict:
    return {'release': version(), 'revision': revision()}
