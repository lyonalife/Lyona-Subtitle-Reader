"""Persistent Windows.Media.Ocr ru worker for cropped OBS frames."""
from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import threading

from PIL import Image


WORKER = Path(__file__).with_name('windows_ocr_worker.ps1')
POWERSHELL = Path(r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe')
PREFIX = 'LYONA_OCR::'


class WindowsOCR:
    def __init__(self):
        if sys.platform != 'win32' or not POWERSHELL.is_file() or not WORKER.is_file():
            raise RuntimeError('Windows OCR недоступен: не найден Windows PowerShell или скрипт OCR')
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        self.proc = subprocess.Popen(
            [str(POWERSHELL), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(WORKER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', bufsize=1, creationflags=flags,
        )
        self.replies: Queue = Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        reply = self._reply(6.0)
        if not reply.get('ready'):
            self.close()
            raise RuntimeError('Windows OCR не запустился')

    def _read(self):
        try:
            for line in self.proc.stdout:
                if line.startswith(PREFIX):
                    self.replies.put(json.loads(line[len(PREFIX):]))
        except (OSError, ValueError) as exc:
            self.replies.put({'error': str(exc)})
        finally:
            self.replies.put({'error': 'Процесс Windows OCR завершился'})

    def _reply(self, timeout: float) -> dict:
        try:
            reply = self.replies.get(timeout=timeout)
        except Empty as exc:
            raise RuntimeError('Windows OCR не ответил вовремя') from exc
        if 'error' in reply:
            raise RuntimeError('Windows OCR: ' + str(reply['error'])[:180])
        return reply

    def recognize(self, image: Image.Image) -> str:
        if self.proc.poll() is not None:
            raise RuntimeError('Процесс Windows OCR остановился')
        encoded = BytesIO()
        image.save(encoded, format='PNG')
        self.next_id += 1
        request = json.dumps({'id': self.next_id,
                              'png': base64.b64encode(encoded.getvalue()).decode('ascii')})
        try:
            self.proc.stdin.write(request + '\n')
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError('Не удалось отправить кадр Windows OCR') from exc
        reply = self._reply(2.0)
        if reply.get('id') != self.next_id:
            raise RuntimeError('Windows OCR вернул ответ не для текущего кадра')
        return str(reply.get('text', ''))

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write('{"cmd":"exit"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=1)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait(timeout=1)
        if self.proc.stdin:
            self.proc.stdin.close()
        if self.proc.stdout:
            self.proc.stdout.close()
