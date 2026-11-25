"""Crash-isolated bridge to the bundled Qwen Native worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from queue import Queue
from qwen_profiles import voice_profile
from qwen_audio import MAX_GAIN
from public_paths import DATA_DIR
from qwen_profiles import LOW

PREFIX = 'LYONA_EVENT::'
BASE = Path(__file__).resolve().parent
WORKER = BASE / 'qwen_obs_worker.py'
LOGFILE = DATA_DIR / 'qwen_obs_log.txt'
MAX_PENDING_AGE = 6.0
FIRST_AUDIO_TIMEOUT = 30.0
STREAM_STALL_TIMEOUT = 45.0
LOAD_TIMEOUT = 180.0


def gpu_snapshot():
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.used,memory.total,utilization.gpu',
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True,
                                timeout=2, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.stdout.strip() if result.returncode == 0 else 'nvidia-smi недоступен'
    except (OSError, subprocess.TimeoutExpired):
        return 'нет данных GPU'


def validate_voice_folder(folder: str) -> tuple[Path, Path]:
    if getattr(sys, 'frozen', False):
        worker = Path(sys.executable).with_name('LyonaWorker.exe')
        if not worker.is_file():
            raise RuntimeError('В сборке отсутствует LyonaWorker.exe.')
        return DATA_DIR, worker
    if not WORKER.is_file():
        raise RuntimeError('Отсутствует голосовой модуль Qwen.')
    return DATA_DIR, Path(sys.executable)


class QwenBridge:
    def __init__(self, events: Queue):
        self.events = events
        self.lock = threading.RLock()
        self.proc = None
        self.session = 0
        self.ready = False
        self.busy = False
        self.pending = None
        self.request_id = 0
        self.folder = ''
        self.device = ''
        self.model = ''
        self._alive = True
        self.force_cpu = False
        self.compute = None
        self.active_request = None
        self.audio_started = False
        self.retry_options = None
        self.retry_override = False
        self.last_progress = 0.0
        self.cuda_graphs_disabled = False

    def _event(self, message: str):
        self.events.put(('log', 'Qwen: ' + str(message)))
        try:
            with LOGFILE.open('a', encoding='utf-8') as output:
                output.write(time.strftime('%Y-%m-%d %H:%M:%S')+' GUI: '+str(message)+'\n')
        except OSError:
            pass

    def prepare(self, folder: str, device: str, model: str):
        """Launch process with model preloading. Called from UI or scan thread."""
        root, python = validate_voice_folder(folder)
        voice_profile(str(root), model)
        with self.lock:
            if not self._alive:
                return
            if self.proc is not None and self.proc.poll() is None:
                if self.folder == str(root) and self.device == device and self.model == model:
                    return
                self._stop_locked()
            self.session += 1
            tag = self.session
            self.folder, self.device, self.model = str(root), device, model
            self.compute = None
            self.ready = False
            self.busy = False
            self.pending = None
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            environment = os.environ.copy()
            if self.retry_override:
                environment.pop('LYONA_QWEN_CPU', None)
            if self.force_cpu:
                environment['LYONA_QWEN_CPU'] = '1'
            if self.cuda_graphs_disabled:
                environment['GGML_CUDA_DISABLE_GRAPHS'] = '1'
            try:
                command = ([str(python), '--qwen-worker', str(root), device, model]
                           if getattr(sys, 'frozen', False) else
                           [str(python), '-B', '-X', 'utf8', '-u', str(WORKER), str(root), device, model])
                self.proc = subprocess.Popen(
                    command,
                    cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, encoding='utf-8', errors='replace',
                    bufsize=1, creationflags=flags,
                    env=environment,
                )
            except Exception:
                self.proc = None
                raise
            thread = threading.Thread(target=self._read_events, args=(tag, self.proc), daemon=True)
            thread.start()
            threading.Thread(target=self._watch_speech, args=(tag, self.proc), daemon=True).start()
            threading.Thread(target=self._loading_notice, args=(tag, self.proc), daemon=True).start()
        self._event('Загружаю модель. Первый запуск может быть заметно дольше следующих.')

    def _watch_speech(self, session, proc):
        # Model loading/JIT is NOT subject to this speech-request watchdog.
        while True:
            time.sleep(2)
            with self.lock:
                if session != self.session or self.proc is not proc or not self._alive:
                    return
                if not self.busy:
                    continue
                elapsed = time.monotonic()-self.last_progress
                if elapsed >= 10 and int(elapsed)//10 != getattr(self, '_last_stall_notice', -1):
                    self._last_stall_notice = int(elapsed)//10
                    self._event(f'Ожидание запроса {self.request_id}: {elapsed:.0f} с без прогресса; '
                                f'ожидающая реплика={self.pending is not None}; GPU used/total MiB, util %: '+gpu_snapshot())
                limit = STREAM_STALL_TIMEOUT if self.audio_started else FIRST_AUDIO_TIMEOUT
                if elapsed < limit:
                    continue
                self._event(f'Нет прогресса Qwen {elapsed:.0f} с, запрос {self.request_id}, '
                            f'backend={self.compute}, звук начат={self.audio_started}. '
                            'Освобождаю зависший worker. Это тайм-аут, а не доказательство нехватки VRAM.')
                if self.compute == 'CUDA' and not self.force_cpu:
                    self._recover_cuda_stall()
                else:
                    # Bound CPU recovery: report the failed request and leave
                    # a clean state; only a new caption/manual action retries.
                    self._stop_locked()
                return

    def _loading_notice(self, session, proc):
        started = time.monotonic()
        while True:
            time.sleep(10)
            with self.lock:
                if session != self.session or proc is not self.proc or self.ready or not self._alive:
                    return
                elapsed = int(time.monotonic()-started)
                self._event(f'Загрузка {self.compute or "Qwen"}: {elapsed} с. '
                            'Первичная JIT-компиляция и прогрев могут занять несколько минут; '
                            f'предел ожидания {LOAD_TIMEOUT:.0f} с. GPU used/total MiB, util %: '+gpu_snapshot())
                if elapsed >= LOAD_TIMEOUT:
                    self._event('Инициализация worker не завершилась за 180 с; восстанавливаю озвучку.')
                    if self.compute != 'CPU' and not self.force_cpu:
                        self._fallback_cpu()
                    else:
                        self._stop_locked()
                    return

    def retry_cuda(self, folder, device, model):
        with self.lock:
            if not self._alive:
                return
            if self.busy:
                self.retry_options = (folder, device, model)
                self._event('Повторная инициализация CUDA запланирована после текущей реплики.')
                return
            self._retry_cuda_locked((folder, device, model))

    def _retry_cuda_locked(self, options):
        pending = self.pending
        self.retry_options = None
        self._stop_locked()
        self.force_cpu = False
        self.cuda_graphs_disabled = False
        self.retry_override = True
        self.compute = None
        self._event('Повторно проверяю CUDA в новом голосовом процессе.')
        self.prepare(*options)
        self.pending = pending

    def say(self, text: str, folder: str, device: str, tempo: float = 1.0, gain: float = 1.0,
            pronunciation: bool = True, model: str = LOW, normalize: bool = False,
            preview: bool = False):
        if not text.strip():
            return
        with self.lock:
            if not self._alive:
                return
            self.prepare(folder, device, model)
            if self.pending is not None:
                self._event(f'Ожидающая реплика заменена более свежей: {len(self.pending[0])} знаков.')
            self.pending = (text, time.monotonic(),
                            max(1.0, min(1.35, float(tempo))),
                            max(0.0, min(MAX_GAIN, float(gain))), bool(pronunciation), bool(normalize),
                            bool(preview))
            self._dispatch_locked()

    def _dispatch_locked(self):
        if not self.ready or self.busy or self.pending is None:
            return
        text, added, tempo, gain, pronunciation, normalize, preview = self.pending
        self.active_request = self.pending
        self.audio_started = False
        self.pending = None
        if not preview and time.monotonic() - added > MAX_PENDING_AGE:
            self.active_request = None
            self._event('Пропущена устаревшая реплика (не накапливаю задержку).')
            return
        self.request_id += 1
        self.busy = True
        self.last_progress = time.monotonic()
        self._last_stall_notice = -1
        self._event(f'Отправка запроса {self.request_id}: {len(text)} знаков; backend={self.compute}; '
                    f'ожидание {self.last_progress-added:.2f} с; preview={preview}.')
        try:
            cmd = json.dumps({'cmd': 'generate', 'id': self.request_id, 'text': text, 'tempo': tempo, 'gain': gain,
                              'pronunciation': pronunciation, 'normalize': normalize,
                              'queued_at': added}, ensure_ascii=True)
            self.proc.stdin.write(cmd + '\n')
            self.proc.stdin.flush()
        except Exception as exc:
            self.busy = False
            self._event('Не удалось отправить реплику: ' + str(exc))

    def _read_events(self, session: int, proc):
        recent_native_output = deque(maxlen=80)
        # Keep logs bounded so long streaming sessions do not fill the disk.
        try:
            if LOGFILE.exists() and LOGFILE.stat().st_size > 2_000_000:
                archive = LOGFILE.with_name('qwen_obs_log.' + str(time.time_ns()) + '.txt')
                LOGFILE.rename(archive)
        except OSError:
            pass
        try:
            # Logging failure must never stop consumption of worker events.
            try:
                logfile = LOGFILE.open('a', encoding='utf-8')
            except OSError as exc:
                logfile = None
                self._event('Не удалось открыть журнал, озвучка продолжится: ' + str(exc))
            def write_log(line):
                nonlocal logfile
                if logfile is not None:
                    try:
                        logfile.write(line)
                        logfile.flush()
                    except OSError as exc:
                        self._event('Ошибка записи журнала, озвучка продолжится: ' + str(exc))
                        try:
                            logfile.close()
                        except OSError:
                            pass
                        logfile = None
            write_log('\n=== Qwen worker started ' + time.strftime('%Y-%m-%d %H:%M:%S') + ' ===\n')
            for line in proc.stdout:
                # Persist the full event BEFORE handling it: fallback may
                # terminate this process before it can print its traceback.
                write_log(line)
                if PREFIX in line:
                    try:
                        ev = json.loads(line.split(PREFIX, 1)[1].strip())
                        self._handle_worker_event(session, ev)
                    except (ValueError, TypeError) as exc:
                        self._event('Некорректное событие worker: ' + str(exc))
                        write_log('Bad event: ' + str(exc) + '\n')
                else:
                    recent_native_output.append(line)
            if logfile is not None:
                logfile.close()
        except Exception as exc:
            self._event('Ошибка чтения ответов голосового движка:\n' + traceback.format_exc())
        finally:
            with self.lock:
                if session == self.session and self.proc is proc and self._alive:
                    code = proc.poll()
                    if self.compute == 'CUDA' and not self.force_cpu:
                        self._event(f'CUDA worker завершился, код {code}. Полный вывод: {LOGFILE.name}\n'
                                    + ''.join(recent_native_output))
                        self._fallback_cpu()
                        return
                    # A broken reader must not leave an untracked native
                    # process consuming CPU/VRAM behind a seemingly idle UI.
                    self._stop_locked()
                    self._event(f'Голосовой процесс завершился ({code}). Подробности: qwen_obs_log.txt')

    def _handle_worker_event(self, session: int, ev: dict):
        with self.lock:
            if session != self.session or not self._alive:
                return
            kind = ev.get('type')
            if kind == 'backend':
                self.compute = ev.get('backend')
                self._event(f'Устройство {self.compute}: {ev.get("reason", "")}')
            elif kind == 'backend_error' and ev.get('backend') == 'CUDA':
                self._event('Ошибка инициализации CUDA:\n' + str(ev.get('details') or ev.get('message', ev)))
                self._fallback_cpu()
            elif kind == 'ready':
                self.ready = True
                self._event(f'Модель загружена за {ev.get("load_seconds", "?")} с. Голос готов к субтитрам!')
                if self.pending is not None:
                    # The latest pending caption waited for initialization,
                    # not for speech. Do not expire it solely due to first JIT.
                    self.pending = (self.pending[0], time.monotonic(), *self.pending[2:])
                self._dispatch_locked()
            elif kind == 'first_generated':
                self.last_progress = time.monotonic()
                self._event(f'Первый готовый блок Qwen через {float(ev.get("seconds", 0)):.2f} сек.')
            elif kind == 'first_chunk':
                self.last_progress = time.monotonic()
                self.audio_started = True
                self._event(f'Первый звуковой блок через {float(ev.get("seconds", 0)):.2f} сек. '
                            f'(подготовка текста {float(ev.get("text_prep", 0)):.3f} с; '
                            f'ожидание модели/очереди {float(ev.get("queue_wait", 0)):.3f} с).')
            elif kind == 'progress':
                self.last_progress = time.monotonic()
            elif kind == 'request_started':
                self.last_progress = time.monotonic()
                self._event(f'Worker принял запрос {ev.get("id")}: {ev.get("chars")} знаков. '
                            'При замедлении будут записаны VRAM и загрузка GPU.')
            elif kind == 'done':
                self.active_request = None
                self._event(f'{ev.get("backend", self.compute)}: реплика с воспроизведением '
                            f'{ev.get("elapsed", 0)} с; аудио {ev.get("audio_seconds", 0)} с; '
                            f'RTF с воспроизведением {ev.get("playback_rtf", 0)}; '
                            f'{ev.get("chars_per_second", 0)} знаков/с.')
                self.busy = False
                if self.retry_options is not None:
                    self._retry_cuda_locked(self.retry_options)
                    return
                self._dispatch_locked()
            elif kind == 'error':
                self._event(str(ev.get('details') or ev.get('message', 'Ошибка голоса')))
                if ev.get('cuda_failure') and not self.force_cpu:
                    self._fallback_cpu()
                    return
                self.busy = False
                self.active_request = None
                if self.retry_options is not None:
                    self._retry_cuda_locked(self.retry_options)
                    return
                self._dispatch_locked()
            elif kind == 'status':
                self._event(str(ev.get('message', ''))[:200])

    def _recover_cuda_stall(self):
        if self.cuda_graphs_disabled:
            self._fallback_cpu()
            return
        pending = self.pending or (self.active_request if not self.audio_started else None)
        options = (self.folder, self.device, self.model)
        self.cuda_graphs_disabled = True
        self._event('Однократное восстановление CUDA без CUDA Graphs: '
                    'меньше временной памяти и отдельные запуски ядер. '
                    'Модель, ICL и параметры генерации сохранены. При повторном зависании — CPU.')
        self._stop_locked()
        self.prepare(*options)
        self.pending = pending

    def _fallback_cpu(self):
        pending = self.pending or (self.active_request if not self.audio_started else None)
        folder, device, model = self.folder, self.device, self.model
        self.force_cpu = True
        self._event('CUDA завершилась с ошибкой. Перезапускаю голос на CPU.')
        self._stop_locked()
        self.prepare(folder, device, model)
        self.pending = pending

    def _stop_locked(self):
        self.session += 1
        proc = self.proc
        self.proc = None
        self.ready = False
        self.busy = False
        self.pending = None
        self.active_request = None
        self.audio_started = False
        if proc is not None:
            try:
                if proc.poll() is None:
                    # Never call Qwen's native stop() across threads.
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass

    def stop(self):
        with self.lock:
            self.retry_options = None
            self._stop_locked()

    def shutdown(self):
        with self.lock:
            self._alive = False
            self._stop_locked()
