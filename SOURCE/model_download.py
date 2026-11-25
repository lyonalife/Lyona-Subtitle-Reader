"""Pinned, resumable first-run model download with SHA-256 verification."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.request import Request, urlopen

from public_paths import MODEL_DIR

REVISION = 'b7ee2e8c7459c3bea99da23e3d178125a7d1713c'
REPOSITORY = 'Serveurperso/Qwen3-TTS-GGUF'
MODELS = {
    'qwen-talker-1.7b-base-Q8_0.gguf': (2079448256, '4b9a33a236908dd9435a42f7a396e38038329d053b704342a6413c08544c4fda'),
    'qwen-tokenizer-12hz-Q8_0.gguf': (291150624, '1883beeed99348fc35e23dd225e9082f93f6f8c109330a33d935baa8acdbfd94'),
}


class DownloadCancelled(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def valid(path: Path, filename: str) -> bool:
    size, expected = MODELS[filename]
    return path.is_file() and path.stat().st_size == size and sha256(path) == expected


def missing() -> list[str]:
    return [name for name in MODELS if not valid(MODEL_DIR / name, name)]


def import_existing(folder: Path, progress=lambda *_: None, cancel=None) -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name in MODELS:
        source = folder / name
        if not valid(source, name):
            raise ValueError('Отсутствует совместимый файл или не совпадает SHA-256: ' + name)
        target = MODEL_DIR / name
        if valid(target, name):
            continue
        part = target.with_name(target.name + '.part')
        with source.open('rb') as incoming, part.open('wb') as output:
            copied = 0
            for data in iter(lambda: incoming.read(4 * 1024 * 1024), b''):
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled('Копирование отменено.')
                output.write(data)
                copied += len(data)
                progress(name, copied, MODELS[name][0])
        if not valid(part, name):
            raise ValueError('Контрольная сумма скопированной модели не совпала: ' + name)
        os.replace(part, target)


def download(progress=lambda *_: None, cancel=None) -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, (size, _expected) in MODELS.items():
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled('Загрузка отменена.')
        target = MODEL_DIR / name
        if valid(target, name):
            progress(name, size, size)
            continue
        part = target.with_name(target.name + '.part')
        offset = part.stat().st_size if part.is_file() else 0
        if offset > size:
            part.unlink()
            offset = 0
        url = f'https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}'
        headers = {'User-Agent': 'LyonaSubtitleReader-Public/1.0'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        with urlopen(Request(url, headers=headers), timeout=45) as response:
            status = getattr(response, 'status', None)
            if offset and status != 206:
                # A proxy ignored Range. Restart rather than append duplicate bytes.
                offset = 0
            with part.open('ab' if offset else 'wb') as output:
                done = offset
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled('Загрузка отменена. Уже полученные данные сохранены для продолжения.')
                    data = response.read(1024 * 1024)
                    if not data:
                        break
                    output.write(data)
                    done += len(data)
                    if done > size:
                        raise ValueError('Сервер прислал файл больше ожидаемого: ' + name)
                    progress(name, done, size)
        if not valid(part, name):
            raise ValueError('Файл не прошёл проверку размера или SHA-256: ' + name)
        os.replace(part, target)
