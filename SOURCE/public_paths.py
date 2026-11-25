"""Per-user writable files for the public, portable Windows build."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'LyonaSubtitleReader'
DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR = DATA_DIR / 'models'
VOICE_DIR = DATA_DIR / 'voices'


def resource(name: str) -> Path:
    return APP_DIR / name
