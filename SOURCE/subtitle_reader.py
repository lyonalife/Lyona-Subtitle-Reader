"""Lyona public Windows reader: OBS, capture card or desktop → local Qwen."""
from __future__ import annotations

import base64
import csv
import ctypes
import hashlib
import os
from io import BytesIO
from collections import defaultdict
import re
import json
from math import log10
import subprocess
from pathlib import Path
from queue import Empty, Full, Queue
import shutil
import sys
import threading
import time
import traceback
from urllib.request import urlopen
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk
import pytesseract
import obsws_python

from subtitle_logic import SubtitleTracker, tidy
from subtitle_ocr import recognize, compare, white_mask, MODES as OCR_MODES, WHITE, AUTO, NORMAL
from windows_ocr import WindowsOCR
from qwen_bridge import QwenBridge, validate_voice_folder
from qwen_profiles import PROFILES, LOW, voice_profile, reference_ready
from qwen_audio import MAX_GAIN, MAX_GAIN_DB
from public_paths import APP_DIR, DATA_DIR, MODEL_DIR, VOICE_DIR
from capture_sources import (LocalCapture, OBS, CAMERA, WINDOW, SCREEN, STREAM, VIDEO_MODES,
                             MODES as CAPTURE_MODES, list_windows, list_screens, named_video_devices)
import model_download


WINDOWS_OCR = 'Windows OCR'
TESSERACT_OCR = 'Tesseract'
OCR_ENGINES = (WINDOWS_OCR, TESSERACT_OCR)
CONFIG_FILE = DATA_DIR / 'settings.json'
OCR_TIMING_FILE = DATA_DIR / 'ocr_timing.csv'
TESS_LOCAL = DATA_DIR / 'tessdata'
TRAINING_DATA_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/rus.traineddata"
COLORS = {
    "bg": "#10191b", "panel": "#172629", "panel2": "#203135",
    "fg": "#f5f7f2", "muted": "#adbdba", "green": "#a8ee7f", "orange": "#f6a766",
    "border": "#344749", "black": "#0c1112", "danger": "#ffc2b7",
}
VOICE_OPTIONS = {label: ('qwen', key) for key, (label, _, _) in PROFILES.items() if key == LOW}
DEFAULT_VOICE = PROFILES[LOW][0]
TEMPO_LABELS = {"Обычный (1.00×)": 1.0,
                "Чуть быстрее (+5%)": 1.05,
                "Побыстрее (+8%)": 1.08,
                "Быстро (+10%)": 1.10,
                "Ещё быстрее (+12%)": 1.12,
                "Быстрее (1.15×)": 1.15,
                "Быстро (1.25×)": 1.25,
                "Максимум (1.35×)": 1.35}
TEMPO_DEFAULT = "Быстрее (1.15×)"
SCAN_PROFILES = {
    'Обычный': (0.55, 2, 'png', 0.0),
    'Быстрый': (0.25, 2, 'jpg', 0.30),
    'Максимально быстрый': (0.15, 2, 'jpg', 0.25),
}
CUSTOM_SCAN_PROFILE = 'Настроено вручную'


def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def load_settings() -> dict:
    try:
        obj = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def find_tesseract(configured: str = "") -> str | None:
    options = [
        configured,
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for path in options:
        if path and Path(path).is_file():
            return str(Path(path))
    return None


def language_config() -> tuple[str, str]:
    """Return (Tesseract CLI arguments, language code) for installed models."""
    custom = TESS_LOCAL / "rus.traineddata"
    if custom.is_file():
        return f'--tessdata-dir "{TESS_LOCAL}"', "rus"
    langs = set(pytesseract.get_languages(config=""))
    if "rus" not in langs:
        raise RuntimeError("Нет русского словаря Tesseract. Нажми «Скачать русский словарь».")
    return "", "rus"


# Unlike v2.2, do not blindly send raw OCR to the speech service.
# Rebuild lines only from words with acceptable OCR confidence and predominantly
# Cyrillic text. Layout/choice prompts must be excluded by selecting a tight crop.
CYR_LETTERS = re.compile(r"[А-Яа-яЁё]")
LAT_LETTERS = re.compile(r"[A-Za-z]")
BUTTON_WORDS = {"выбрать", "назад", "пропустить", "выйти", "подтвердить"}


def filter_ocr_data(data: dict, min_confidence: float = 43) -> str:
    rows = defaultdict(list)
    n = len(data.get('text', []))
    for i in range(n):
        word = str(data['text'][i]).strip()
        if not word:
            continue
        try:
            conf = float(data['conf'][i])
        except (ValueError, TypeError):
            continue
        # Discard low-confidence words; separate punctuation can be joined to
        # neighboring words later, but symbols on their own are not dialogue.
        if conf < min_confidence or not (CYR_LETTERS.search(word) or LAT_LETTERS.search(word)):
            continue
        # OCR occasionally hallucinates endless punctuation and Latin garbage.
        letters = len(CYR_LETTERS.findall(word)) + len(LAT_LETTERS.findall(word))
        if letters * 2 < len(word):
            continue
        group = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
        rows[group].append((word, conf))
    result = []
    for group, parts in rows.items():
        text = ' '.join(w for w, _ in parts)
        cy = len(CYR_LETTERS.findall(text))
        la = len(LAT_LETTERS.findall(text))
        if cy < 2 or cy / max(1, cy + la) < .58:
            continue
        toks = [w.casefold().strip('.,:()[]') for w, _ in parts]
        # Typical controller prompt: 'Выбрать' / 'Назад' — not spoken dialogue.
        if len(set(toks) & BUTTON_WORDS) >= 2 or set(toks).issubset(BUTTON_WORDS):
            continue
        if sum(conf for _, conf in parts) / len(parts) < 51:
            continue
        result.append(text)
    return tidy('\n'.join(result[:3]))


def extract_subtitles(img: Image.Image, contrast: bool, tess_config: str, language: str, mode: str = WHITE) -> str:
    # The checkbox is kept for backwards compatibility; the newer mode
    # selector determines the actual preprocessing method.
    return recognize(img, tess_config, language, mode).text


def playback_devices() -> list[str]:
    try:
        os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
        import pygame
        # SDL2 enumerates devices only after the audio subsystem is running.
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        from pygame._sdl2.audio import get_audio_device_names
        names = list(get_audio_device_names(False))
        return ["По умолчанию"] + [str(n) for n in names]
    except Exception:
        return ["По умолчанию"]


# OBS-WebSocket v5 is bundled with OBS Studio 28+.
# The source screenshot is requested *from OBS*, never from the Elgato device.
# It remains available while the OBS window is minimized (source active in OBS).


def decode_obs_frame(data: str) -> Image.Image:
    """Read obs-websocket's base64 data-URI (jpeg/png) to a PIL image."""
    raw = base64.b64decode(data.split(',', 1)[-1], validate=True)
    with Image.open(BytesIO(raw)) as im:
        return im.convert('RGB')


def append_ocr_timing(row: dict):
    """Keep timing data locally without recording captured text or frames."""
    try:
        if OCR_TIMING_FILE.exists() and OCR_TIMING_FILE.stat().st_size > 2_000_000:
            OCR_TIMING_FILE.rename(OCR_TIMING_FILE.with_name(
                f'ocr_timing.{time.time_ns()}.csv'))
        new_file = not OCR_TIMING_FILE.exists()
        with OCR_TIMING_FILE.open('a', encoding='utf-8', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=list(row))
            if new_file:
                writer.writeheader()
            writer.writerow(row)
    except OSError:
        # Diagnostics must never prevent speech.
        pass


def crop_fractional(frame: Image.Image, region: dict) -> Image.Image:
    """Crop to the selected *source-relative* subtitle location, regardless of preview size."""
    x1 = int(max(0.0, min(1.0, region['x'])) * frame.width)
    y1 = int(max(0.0, min(1.0, region['y'])) * frame.height)
    x2 = int(max(0.0, min(1.0, region['x'] + region['w'])) * frame.width)
    y2 = int(max(0.0, min(1.0, region['y'] + region['h'])) * frame.height)
    if x2 - x1 < 10 or y2 - y1 < 10:
        raise ValueError('Область слишком маленькая. Выбери субтитры заново.')
    return frame.crop((x1, y1, x2, y2))


class SubtitleSelector:
    """Freeze a snapshot obtained from OBS, then select subtitles on that image."""
    def __init__(self, app: 'SubtitleApp', frame: Image.Image):
        self.app = app
        self.frame = frame
        self.start = None
        self.rect_id = None
        self.win = tk.Toplevel(app.root)
        self.win.title('Выдели область субтитров — источник из OBS')
        self.win.configure(bg=COLORS['bg'])
        max_w, max_h = 1040, 630
        display_width = frame.width * frame.info.get('sample_aspect_ratio', 1)
        scale = min(max_w / display_width, max_h / frame.height, 1.0)
        self.width = int(display_width * scale)
        self.height = int(frame.height * scale)
        self.scale = scale
        img = frame.resize((self.width, self.height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(img)
        tk.Label(self.win, text='Мышкой обведи субтитры внизу картинки (ESC — отмена)',
                 bg=COLORS['bg'], fg=COLORS['green'], font=('Segoe UI', 12, 'bold'),
                 pady=10).pack(fill='x')
        self.canvas = tk.Canvas(self.win, width=self.width, height=self.height,
                                highlightthickness=0, cursor='crosshair')
        self.canvas.pack(padx=12, pady=(0, 12))
        self.canvas.create_image(0, 0, image=self.photo, anchor='nw')
        if app.region:
            r = app.region
            self.canvas.create_rectangle(
                r['x'] * self.width, r['y'] * self.height,
                (r['x'] + r['w']) * self.width, (r['y'] + r['h']) * self.height,
                outline=COLORS['orange'], dash=(6, 4), width=2,
            )
        self.canvas.bind('<ButtonPress-1>', self.mouse_down)
        self.canvas.bind('<B1-Motion>', self.mouse_move)
        self.canvas.bind('<ButtonRelease-1>', self.mouse_up)
        self.win.bind('<Escape>', lambda event: self.win.destroy())
        self.win.transient(app.root)
        self.win.grab_set()
        self.win.focus_force()

    def mouse_down(self, event):
        self.start = (event.x, event.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                     outline=COLORS['green'], width=3)

    def mouse_move(self, event):
        if self.rect_id and self.start:
            self.canvas.coords(self.rect_id, self.start[0], self.start[1], event.x, event.y)

    def mouse_up(self, event):
        if not self.start:
            return
        xa, xb = sorted((max(0, min(self.width, self.start[0])),
                         max(0, min(self.width, event.x))))
        ya, yb = sorted((max(0, min(self.height, self.start[1])),
                         max(0, min(self.height, event.y))))
        if xb - xa < 80 or yb - ya < 18:
            messagebox.showwarning('Маленькая область', 'Обведи текст чуть шире.', parent=self.win)
            return
        self.app.region = {
            'x': round(xa / self.width, 6), 'y': round(ya / self.height, 6),
            'w': round((xb - xa) / self.width, 6), 'h': round((yb - ya) / self.height, 6),
        }
        self.app.display_region()
        self.app.persist()
        self.app.log('Область сохранена относительно источника OBS. Теперь нажми «Проверить текст».')
        self.win.grab_release()
        self.win.destroy()


class SubtitleApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.saved = load_settings()
        self.setup_complete = bool(self.saved.get('setup_complete', False))
        self.region = self.saved.get('obs_region')
        self.obs = None
        self.obs_lock = threading.RLock()
        self.speech_lock = threading.RLock()
        self.events: Queue = Queue()
        self.qwen = QwenBridge(self.events)
        self.local_capture = LocalCapture()
        self.window_sources = {}
        self.screen_sources = {}
        self.capture_mode_var = tk.StringVar(value=self.saved.get('capture_mode', OBS))
        if self.capture_mode_var.get() in ('Игровой процесс · хук OBS', 'Игровой процесс'):
            self.capture_mode_var.set(SCREEN)
        self.video_resolution_var = tk.StringVar(value=self.saved.get('video_resolution', 'Авто'))
        self.video_fps_var = tk.StringVar(value=self.saved.get('video_fps', 'Авто'))
        self.video_aspect_var = tk.StringVar(value=self.saved.get('video_aspect', 'Авто'))
        self.video_aspects = dict(self.saved.get('video_aspects', {}))
        if self.saved.get('obs_source') and self.saved.get('video_aspect'):
            self.video_aspects.setdefault(self.saved['obs_source'], self.saved['video_aspect'])
        self._last_capture_info = ''
        if self.capture_mode_var.get() not in CAPTURE_MODES:
            self.capture_mode_var.set(OBS)
        self.voice_choice_var = tk.StringVar(value=self.saved.get('voice_choice', DEFAULT_VOICE))
        if self.voice_choice_var.get() not in VOICE_OPTIONS:
            self.voice_choice_var.set(DEFAULT_VOICE)
        self.scan_flag = threading.Event()
        self.tess_path = find_tesseract(self.saved.get('tesseract', ''))
        if self.tess_path:
            pytesseract.pytesseract.tesseract_cmd = self.tess_path
        self.password_var = tk.StringVar()
        self.port_var = tk.StringVar(value=str(self.saved.get('obs_port', '4455')))
        self.source_var = tk.StringVar(value=self.saved.get('obs_source', ''))
        self.voice_folder_var = tk.StringVar(value=str(DATA_DIR))
        old_tempo = float(self.saved.get('qwen_tempo', 1.15))
        label = min(TEMPO_LABELS, key=lambda x: abs(TEMPO_LABELS[x] - old_tempo))
        self.qwen_tempo_var = tk.StringVar(value=label)
        self.live_qwen_tempo = TEMPO_LABELS[label]
        old_gain = max(1.0, min(MAX_GAIN, float(self.saved.get('qwen_gain', 1.0))))
        self.qwen_gain_db_var = tk.DoubleVar(value=20 * log10(old_gain))
        self.qwen_normalize_var = tk.BooleanVar(value=self.saved.get('qwen_normalize', True))
        self.qwen_levels = old_gain, self.qwen_normalize_var.get()
        self.qwen_volume_var = tk.DoubleVar(value=max(0, min(100, float(self.saved.get('qwen_volume', 100)))))
        self.qwen_volume = self.qwen_volume_var.get() / 100.0
        self.pronunciation_var = tk.BooleanVar(value=self.saved.get('pronunciation_enabled', True))
        self.device_var = tk.StringVar(value=self.saved.get('device', 'По умолчанию'))
        self.contrast_var = tk.BooleanVar(value=self.saved.get('contrast', False))
        self.ocr_mode_var = tk.StringVar(value=self.saved.get('ocr_mode', WHITE))
        saved_ocr_engine = self.saved.get('ocr_engine', WINDOWS_OCR)
        if saved_ocr_engine not in OCR_ENGINES:
            saved_ocr_engine = WINDOWS_OCR
        self.ocr_engine_var = tk.StringVar(value=saved_ocr_engine)
        self.active_ocr_engine = saved_ocr_engine
        self.interval_var = tk.StringVar(value=self.saved.get('interval', '0.15'))
        self.frames_var = tk.StringVar(value=str(self.saved.get('stability_frames', '2')))
        saved_profile = self.saved.get('scan_profile', 'Максимально быстрый')
        if saved_profile not in SCAN_PROFILES:
            saved_profile = CUSTOM_SCAN_PROFILE
        self.scan_profile_var = tk.StringVar(value=saved_profile)
        self._build()
        self.refresh_devices()
        self.refresh_sources()
        self.display_region()
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.root.after(120, self.pump_events)
        self.log('Выбери способ захвата, источник изображения и область субтитров.')
        if not all((MODEL_DIR / name).is_file() for name in model_download.MODELS):
            self.log('Перед озвучкой нажми «Проверить / загрузить Qwen», чтобы подготовить модели.')

    def panel(self, parent, title):
        frame = tk.Frame(parent, bg=COLORS['panel'], padx=13, pady=9)
        frame.pack(fill='x', pady=(0, 8))
        tk.Label(frame, text=title, bg=COLORS['panel'], fg=COLORS['orange'],
                 font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(0, 6))
        return frame

    def btn(self, parent, title, command, accent=False, large=False):
        return tk.Button(parent, text=title, command=command,
                         bg=COLORS['green'] if accent else COLORS['panel2'],
                         fg=COLORS['black'] if accent else COLORS['fg'],
                         activebackground=COLORS['orange'], activeforeground=COLORS['black'],
                         relief='flat', cursor='hand2', bd=0, padx=13, pady=8 if large else 6,
                         font=('Segoe UI', 11 if large else 10, 'bold' if accent else 'normal'))

    def field_label(self, parent, title):
        return tk.Label(parent, text=title, bg=COLORS['panel'], fg=COLORS['fg'],
                        font=('Segoe UI', 10))

    def _build(self):
        self.root.title('Lyona Subtitle Reader · Streamer')
        height = min(990, self.root.winfo_screenheight() - 110)
        self.root.geometry(f'1050x{height}')
        self.root.minsize(900, 440)
        self.root.configure(bg=COLORS['bg'])
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('TCombobox', fieldbackground=COLORS['panel2'],
                        background=COLORS['panel2'], foreground=COLORS['fg'],
                        selectbackground=COLORS['panel2'], selectforeground=COLORS['fg'], padding=5)
        style.map('TCombobox', fieldbackground=[('readonly', COLORS['panel2'])],
                  foreground=[('readonly', COLORS['fg'])])
        canvas = tk.Canvas(self.root, bg=COLORS['bg'], highlightthickness=0,
                           borderwidth=0, yscrollincrement=24)
        scrollbar = ttk.Scrollbar(self.root, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        wrap = tk.Frame(canvas, bg=COLORS['bg'], padx=18, pady=12)
        content = canvas.create_window((0, 0), window=wrap, anchor='nw')
        wrap.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox(content)))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(content, width=event.width))
        tk.Label(wrap, text='ОЗВУЧКА СУБТИТРОВ · QWEN 1.7B', bg=COLORS['bg'], fg=COLORS['green'],
                 font=('Segoe UI', 21, 'bold')).pack(anchor='w')
        tk.Label(wrap, text='Источник изображения → OCR → локальная озвучка Qwen.' ,
                 bg=COLORS['bg'], fg=COLORS['muted'], font=('Segoe UI', 10)).pack(anchor='w', pady=(0, 8))

        panel = self.panel(wrap, '1   СПОСОБ ЗАХВАТА')
        mode_row = tk.Frame(panel, bg=COLORS['panel'])
        mode_row.pack(fill='x', pady=(0, 6))
        self.field_label(mode_row, 'Получать кадры через').pack(side='left', padx=(0, 8))
        mode_combo = ttk.Combobox(mode_row, textvariable=self.capture_mode_var,
                                  values=CAPTURE_MODES, state='readonly', width=25)
        mode_combo.pack(side='left', padx=(0, 8))
        mode_combo.bind('<<ComboboxSelected>>', self.capture_mode_changed)
        self.btn(mode_row, 'Мастер настройки', self.first_run).pack(side='left')
        self.obs_panel = panel
        row = tk.Frame(panel, bg=COLORS['panel'])
        row.pack(fill='x', pady=(0, 6))
        self.field_label(row, 'Порт').pack(side='left', padx=(0, 5))
        tk.Entry(row, textvariable=self.port_var, width=6, bg=COLORS['panel2'], fg=COLORS['fg'],
                 insertbackground=COLORS['fg'], relief='flat', font=('Segoe UI', 10)).pack(side='left', padx=(0, 13))
        self.field_label(row, 'Пароль WebSocket').pack(side='left', padx=(0, 7))
        self.password_entry = tk.Entry(row, textvariable=self.password_var, show='•', width=24,
                                       bg=COLORS['panel2'], fg=COLORS['fg'],
                                       insertbackground=COLORS['fg'], relief='flat',
                                       font=('Segoe UI', 10))
        self.password_entry.pack(side='left', padx=(0, 5))
        # Tkinter's default Ctrl+V / popup menu varies with keyboard layouts.
        # Provide a reliable clipboard button and explicit keyboard shortcuts.
        self.password_entry.bind('<Control-v>', self.paste_password_event)
        self.password_entry.bind('<Control-V>', self.paste_password_event)
        self.password_entry.bind('<Shift-Insert>', self.paste_password_event)
        self.password_entry.bind('<Button-3>', self.password_context_menu)
        self.btn(row, 'Вставить', self.paste_password).pack(side='left', padx=(0, 5))
        self.show_password_button = self.btn(row, 'Показать', self.toggle_password)
        self.show_password_button.pack(side='left', padx=(0, 5))
        self.btn(row, 'Подключить к OBS', self.connect_obs, accent=True).pack(side='left')
        self.obs_status = tk.Label(panel, text='Не подключено', bg=COLORS['panel'],
                                   fg=COLORS['muted'], anchor='w')
        self.obs_status.pack(fill='x')

        panel = self.panel(wrap, '2   ИСТОЧНИК И СУБТИТРЫ')
        row = tk.Frame(panel, bg=COLORS['panel'])
        row.pack(fill='x', pady=(0, 4))
        self.field_label(row, 'Источник').pack(side='left', padx=(0, 8))
        self.source_combo = ttk.Combobox(row, textvariable=self.source_var, state='readonly', width=36)
        self.source_combo.pack(side='left', padx=(0, 8))
        self.source_combo.bind('<<ComboboxSelected>>', lambda _ev: self.source_changed())
        self.btn(row, 'Обновить устройства', self.refresh_sources).pack(side='left')
        self.video_controls = tk.Frame(panel, bg=COLORS['panel'])
        for label, variable, values in (
                ('Разрешение', self.video_resolution_var, ('Авто', '1920x1080', '2560x1440', '3840x2160', '1280x720', '640x480')),
                ('FPS', self.video_fps_var, ('Авто', '5', '10', '15', '25', '30', '50', '60')),
                ('Пропорции', self.video_aspect_var, ('Авто', '16:9', '4:3', '21:9', '9:16'))):
            self.field_label(self.video_controls, label).pack(side='left', padx=(0, 6))
            control = ttk.Combobox(self.video_controls, textvariable=variable,
                                   values=values, state='readonly', width=14)
            control.pack(side='left', padx=(0, 10))
            control.bind('<<ComboboxSelected>>', self.on_video_format_changed)
        row2 = tk.Frame(panel, bg=COLORS['panel'])
        row2.pack(fill='x', pady=(2, 4))
        self.btn(row2, 'Выделить область субтитров', self.choose_region, accent=True).pack(side='left', padx=(0, 8))
        self.btn(row2, 'Проверить текст', self.test_ocr).pack(side='left', padx=(0, 6))
        self.btn(row2, 'ПОКАЗАТЬ ВЫРЕЗКУ', self.show_crop).pack(side='left')
        self.btn(row2, 'Кадр источника', self.show_source).pack(side='left', padx=6)
        self.region_label = tk.Label(panel, text='', bg=COLORS['panel'], fg=COLORS['orange'], anchor='w')
        self.region_label.pack(fill='x')

        panel = self.panel(wrap, '3   ГОЛОС И АУДИО')
        row = tk.Frame(panel, bg=COLORS['panel'])
        row.pack(fill='x', pady=(0, 5))
        self.field_label(row, 'Голос').pack(side='left', padx=(0, 8))
        voice_combo = ttk.Combobox(row, textvariable=self.voice_choice_var,
                                   values=list(VOICE_OPTIONS), state='readonly', width=38)
        voice_combo.pack(side='left', padx=(0, 8))
        voice_combo.bind('<<ComboboxSelected>>', self.on_voice_changed)
        self.btn(row, 'Послушать', self.test_voice).pack(side='left', padx=(0, 7))
        self.btn(row, 'Подготовить голос', self.prepare_qwen).pack(side='left')
        self.btn(row, 'Повторить CUDA', self.retry_cuda).pack(side='left', padx=(7, 0))
        folder_row = tk.Frame(panel, bg=COLORS['panel'])
        folder_row.pack(fill='x', pady=(7, 7))
        self.btn(folder_row, 'Проверить / загрузить Qwen', self.ensure_models).pack(side='left')
        row = tk.Frame(panel, bg=COLORS['panel'])
        row.pack(fill='x', pady=(0, 7))
        self.field_label(row, 'Темп голоса').pack(side='left', padx=(0, 8))
        self.qwen_tempo_combo = ttk.Combobox(row, textvariable=self.qwen_tempo_var,
                                values=list(TEMPO_LABELS), state='readonly', width=23)
        self.qwen_tempo_combo.pack(side='left', padx=(0, 8))
        self.qwen_tempo_combo.bind('<<ComboboxSelected>>', self.on_qwen_tempo_changed)
        tk.Label(row, text='Без изменения высоты голоса · при тесте Qwen перезапуск не нужен',
                 bg=COLORS['panel'], fg=COLORS['muted'], font=('Segoe UI', 9)).pack(side='left')
        row = tk.Frame(panel, bg=COLORS['panel'])
        row.pack(fill='x', pady=(0, 7))
        self.field_label(row, 'Усиление Qwen').pack(side='left', padx=(0, 8))
        gain_scale = ttk.Scale(row, from_=0, to=MAX_GAIN_DB, variable=self.qwen_gain_db_var, length=210)
        gain_scale.pack(side='left', padx=(0, 8))
        self.qwen_gain_label = tk.StringVar(value=f'+{self.qwen_gain_db_var.get():.1f} дБ')
        tk.Label(row, textvariable=self.qwen_gain_label, width=9, anchor='w',
                 bg=COLORS['panel'], fg=COLORS['fg']).pack(side='left')
        self.qwen_gain_db_var.trace_add('write', self.on_qwen_levels_changed)
        tk.Checkbutton(row, text='Автонормализация',
                       variable=self.qwen_normalize_var, command=self.on_qwen_levels_changed,
                       bg=COLORS['panel'], fg=COLORS['fg'], selectcolor=COLORS['panel2'],
                       activebackground=COLORS['panel'], activeforeground=COLORS['green'],
                       highlightthickness=0).pack(side='left')
        row = tk.Frame(panel, bg=COLORS['panel'])
        row.pack(fill='x', pady=(0, 7))
        self.field_label(row, 'Громкость Qwen').pack(side='left', padx=(0, 8))
        ttk.Scale(row, from_=0, to=100, variable=self.qwen_volume_var, length=210).pack(side='left', padx=(0, 8))
        self.qwen_volume_label = tk.StringVar(value=f'{self.qwen_volume_var.get():.0f}%')
        tk.Label(row, textvariable=self.qwen_volume_label, width=5, anchor='w',
                 bg=COLORS['panel'], fg=COLORS['fg']).pack(side='left')
        tk.Label(row, text='Только голос Lyona · громкость игры и Windows не меняется',
                 bg=COLORS['panel'], fg=COLORS['muted'], font=('Segoe UI', 9)).pack(side='left')
        self.qwen_volume_var.trace_add('write', self.on_qwen_volume_changed)
        row = tk.Frame(panel, bg=COLORS['panel'])
        row.pack(fill='x', pady=(0, 4))
        self.field_label(row, 'Куда выводить').pack(side='left', padx=(0, 8))
        self.device_combo = ttk.Combobox(row, textvariable=self.device_var,
                                         state='readonly', width=39)
        self.device_combo.pack(side='left', padx=(0, 7))
        self.device_combo.bind('<<ComboboxSelected>>', self.on_voice_changed)
        self.btn(row, 'Обновить', self.refresh_devices).pack(side='left')
        pronunciation_row = tk.Frame(panel, bg=COLORS['panel'])
        pronunciation_row.pack(fill='x', pady=(4, 0))
        tk.Checkbutton(pronunciation_row, text='Исправлять произношение (ё + словарь)',
                       variable=self.pronunciation_var, command=self.on_pronunciation_changed,
                       bg=COLORS['panel'], fg=COLORS['fg'], selectcolor=COLORS['panel2'],
                       activebackground=COLORS['panel'], activeforeground=COLORS['green'],
                       font=('Segoe UI', 10)).pack(side='left', padx=(0, 8))
        self.btn(pronunciation_row, 'Открыть словарь', self.open_pronunciation_dictionary).pack(side='left')
        tk.Label(pronunciation_row, text='Не меняет модель и образец голоса',
                 bg=COLORS['panel'], fg=COLORS['muted'],
                 font=('Segoe UI', 9)).pack(side='left', padx=(10, 0))

        panel = self.panel(wrap, '4   РАСПОЗНАВАНИЕ')
        engine_row = tk.Frame(panel, bg=COLORS['panel'])
        engine_row.pack(fill='x', pady=(0, 5))
        self.field_label(engine_row, 'Движок OCR').pack(side='left', padx=(0, 8))
        self.ocr_engine_combo = ttk.Combobox(engine_row, textvariable=self.ocr_engine_var,
                                            values=OCR_ENGINES, state='readonly', width=18)
        self.ocr_engine_combo.pack(side='left')
        self.ocr_engine_combo.bind('<<ComboboxSelected>>', self.on_ocr_engine_changed)
        self.field_label(engine_row, 'Переключается на следующем кадре').pack(side='left', padx=(10, 0))
        profile_row = tk.Frame(panel, bg=COLORS['panel'])
        profile_row.pack(fill='x', pady=(0, 5))
        self.field_label(profile_row, 'Скорость распознавания').pack(side='left', padx=(0, 8))
        self.scan_profile_combo = ttk.Combobox(profile_row, textvariable=self.scan_profile_var,
            values=[*SCAN_PROFILES, CUSTOM_SCAN_PROFILE], state='readonly', width=23)
        self.scan_profile_combo.pack(side='left')
        self.scan_profile_combo.bind('<<ComboboxSelected>>', self.on_scan_profile_changed)
        row = tk.Frame(panel, bg=COLORS['panel'])
        row.pack(fill='x')
        self.field_label(row, 'Частота проверки:').pack(side='left', padx=(0, 8))
        self.field_label(row, 'Каждые (сек)').pack(side='left', padx=(16, 7))
        interval_combo = ttk.Combobox(row, textvariable=self.interval_var,
            values=['0.15','0.20','0.25','0.35','0.45','0.55','0.7','0.8','1.0'],
            state='readonly', width=6)
        interval_combo.pack(side='left', padx=(0, 8))
        interval_combo.bind('<<ComboboxSelected>>', self.on_scan_manual_changed)
        self.field_label(row, 'Подтверждение (кадров)').pack(side='left', padx=(3, 6))
        frames_combo = ttk.Combobox(row, textvariable=self.frames_var, values=['1','2'],
            state='readonly', width=3)
        frames_combo.pack(side='left', padx=(0, 8))
        frames_combo.bind('<<ComboboxSelected>>', self.on_scan_manual_changed)
        tools_row = tk.Frame(panel, bg=COLORS['panel'])
        tools_row.pack(fill='x', pady=(5, 0))
        self.btn(tools_row, 'Путь к Tesseract', self.choose_tesseract).pack(side='left')
        self.btn(tools_row, 'Русский словарь', self.download_russian).pack(side='left', padx=(5, 0))
        mode_row = tk.Frame(panel, bg=COLORS['panel'])
        mode_row.pack(fill='x', pady=(8, 2))
        self.field_label(mode_row, 'Как читать титры').pack(side='left', padx=(0, 7))
        ttk.Combobox(mode_row, textvariable=self.ocr_mode_var, values=OCR_MODES,
                     state='readonly', width=30).pack(side='left', padx=(0, 7))
        self.btn(mode_row, 'СРАВНИТЬ НА КАДРЕ', self.compare_ocr).pack(side='left')
        tk.Label(panel, text='Рамку оставляй широкой: режим белого текста сам убирает фон внутри неё.',
                 bg=COLORS['panel'], fg=COLORS['muted'], anchor='w',
                 font=('Segoe UI', 9)).pack(fill='x')

        row = tk.Frame(wrap, bg=COLORS['bg'])
        row.pack(fill='x', pady=(5, 8))
        self.start_button = self.btn(row, '▶  НАЧАТЬ ОЗВУЧКУ', self.start, accent=True, large=True)
        self.start_button.pack(side='left', padx=(0, 8))
        self.stop_button = self.btn(row, '■  ОСТАНОВИТЬ', self.stop, large=True)
        self.stop_button.pack(side='left')
        self.stop_button.config(state='disabled')
        self.status = tk.Label(row, text='●  Не запущено', bg=COLORS['bg'], fg=COLORS['muted'],
                               font=('Segoe UI', 11, 'bold'))
        self.status.pack(side='right')

        box = tk.Frame(wrap, bg=COLORS['panel'], padx=12, pady=9)
        box.pack(fill='both', expand=True)
        tk.Label(box, text='ПОСЛЕДНИЙ ТЕКСТ', bg=COLORS['panel'], fg=COLORS['green'],
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        self.last_text = tk.Label(box, text='Здесь появятся распознанные субтитры.',
                                  bg=COLORS['panel'], fg=COLORS['fg'], justify='left', anchor='nw',
                                  font=('Segoe UI', 11), wraplength=740)
        self.last_text.pack(fill='both', expand=True, pady=(4, 5))
        tk.Label(box, text='СОБЫТИЯ', bg=COLORS['panel'], fg=COLORS['muted'],
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        self.logs = tk.Text(box, height=3, bg=COLORS['black'], fg=COLORS['muted'],
                            wrap='word', state='disabled', padx=8, pady=6,
                            font=('Consolas', 9), relief='flat')
        self.logs.pack(fill='x', pady=(4, 0))

        def scroll_window(event):
            if event.delta:
                units = max(1, abs(event.delta) // 120) * 3
                canvas.yview_scroll(-units if event.delta > 0 else units, 'units')
            return 'break'

        def bind_scroll(widget):
            widget.bindtags((scroll_tag, *widget.bindtags()))
            for child in widget.winfo_children():
                bind_scroll(child)

        scroll_tag = 'LyonaWindowScroll'
        self.root.bind_class(scroll_tag, '<MouseWheel>', scroll_window)
        bind_scroll(self.root)

    def log(self, message):
        self.logs.config(state='normal')
        self.logs.insert('end', '• ' + str(message) + '\n')
        self.logs.see('end')
        self.logs.config(state='disabled')

    def display_region(self):
        if not self.region:
            self.region_label.config(text='Область субтитров ещё не выбрана', fg=COLORS['orange'])
        else:
            r = self.region
            self.region_label.config(text=f'Выделено: X {r["x"]*100:.0f}%  Y {r["y"]*100:.0f}%   '
                                          f'{r["w"]*100:.0f}% × {r["h"]*100:.0f}% кадра',
                                     fg=COLORS['green'])

    def _set_running(self, is_running):
        self.start_button.config(state='disabled' if is_running else 'normal')
        self.stop_button.config(state='normal' if is_running else 'disabled')
        self.status.config(text='● Читаю субтитры' if is_running else '● Не запущено',
                           fg=COLORS['green'] if is_running else COLORS['muted'])

    def pump_events(self):
        while True:
            try:
                kind, message = self.events.get_nowait()
            except Empty:
                break
            if kind == 'log':
                self.log(message)
            elif kind == 'read':
                self.last_text.config(text=message)
                self.log('Подтверждено OCR: ' + message.replace('\n', ' ')[:95])
            elif kind == 'text':
                self.last_text.config(text=message or 'Текст не найден. Дождись диалога и проверь область.')
            elif kind == 'error':
                self.log(message)
                messagebox.showerror('Ошибка', message, parent=self.root)
            elif kind == 'ocr_fallback':
                self.ocr_engine_var.set(TESSERACT_OCR)
                self.persist()
                self.log('Windows OCR недоступен; включён Tesseract. ' + message)
            elif kind == 'model_progress':
                name, done, total = message
                if getattr(self, 'model_progress', None) and self.model_progress.winfo_exists():
                    self.model_status.set(f'{name}: {done / 1_000_000:.0f} / {total / 1_000_000:.0f} МБ')
                    self.model_bar['value'] = 100 * done / total
            elif kind in ('model_done', 'model_error', 'model_cancelled'):
                if getattr(self, 'model_progress', None) and self.model_progress.winfo_exists():
                    self.model_progress.destroy()
                self.log(message)
                if kind == 'model_done':
                    messagebox.showinfo('Qwen', message, parent=self.root)
                    self.finish_first_run()
                elif kind == 'model_cancelled':
                    messagebox.showinfo('Qwen', message + '\nМастер настройки остаётся открытым.', parent=self.root)
                else:
                    messagebox.showerror('Qwen', message + '\n\nПовторный запуск продолжит загрузку.', parent=self.root)
            elif kind == 'crop':
                self.open_crop_preview(message)
            elif kind == 'select_region':
                mode, source, frame = message
                if mode == self.capture_mode_var.get() and source == self.source_var.get() and not self.scan_flag.is_set():
                    SubtitleSelector(self, frame)
            elif kind == 'source_frame':
                self.open_source_frame(message)
            elif kind == 'ocr_compare':
                self.show_ocr_comparison(message)
            elif kind == 'stopped' and message is self.scan_flag:
                self._set_running(False)
        try:
            self.root.after(120, self.pump_events)
        except tk.TclError:
            pass

    def paste_password(self):
        """Paste the OBS credential from the Windows clipboard without logging it."""
        try:
            pasted = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showwarning('Не удалось вставить',
                                   'Сначала нажми «Копировать» рядом с паролем в OBS, '
                                   'потом вернись сюда и нажми «Вставить».',
                                   parent=self.root)
            return
        if not pasted:
            messagebox.showwarning('Буфер обмена пуст',
                                   'Сначала скопируй пароль WebSocket из OBS.',
                                   parent=self.root)
            return
        # OBS-generated passwords do not include spaces, but preserve user-entered spaces.
        self.password_var.set(pasted.strip('\r\n'))
        self.password_entry.focus_set()
        self.password_entry.icursor('end')

    def paste_password_event(self, _event):
        self.paste_password()
        return 'break'

    def toggle_password(self):
        if self.password_entry.cget('show'):
            self.password_entry.configure(show='')
            self.show_password_button.configure(text='Скрыть')
        else:
            self.password_entry.configure(show='•')
            self.show_password_button.configure(text='Показать')

    def password_context_menu(self, event):
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label='Вставить пароль из буфера', command=self.paste_password)
        menu.add_command(label='Выделить всё',
                         command=lambda: (self.password_entry.focus_set(),
                                          self.password_entry.select_range(0, tk.END)))
        menu.add_command(label='Очистить', command=lambda: self.password_var.set(''))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def connect_obs(self):
        if self.scan_flag.is_set():
            messagebox.showinfo('Останови озвучку', 'Сначала нажми «Остановить».')
            return
        try:
            port = int(self.port_var.get())
            if not 1 <= port <= 65535:
                raise ValueError('Неверный порт')
            # Localhost only: OBS WebSocket must not be exposed to the public internet.
            with self.obs_lock:
                self.obs = obsws_python.ReqClient(host='127.0.0.1', port=port,
                                                  password=self.password_var.get(), timeout=6)
                version = self.obs.get_version().obs_version
            self.obs_status.configure(text=f'Подключено к OBS {version} ✓', fg=COLORS['green'])
            self.log('OBS подключён. Выбери источник с субтитрами.')
            self.refresh_sources()
            self.persist()
        except Exception as exc:
            self.obs = None
            self.obs_status.configure(text='Не подключено', fg=COLORS['danger'])
            messagebox.showerror('Не удалось подключиться к OBS',
                                 'В OBS открой: Инструменты → Настройки сервера WebSocket.\n'
                                 'Включи сервер. Скопируй пароль и проверь порт 4455.\n\n'
                                 f'Техническая ошибка: {exc}', parent=self.root)

    def refresh_sources(self):
        mode = self.capture_mode_var.get()
        self.source_combo.config(state='normal' if mode == STREAM else 'readonly')
        if mode in VIDEO_MODES:
            self.video_controls.pack(fill='x', pady=4)
        else:
            self.video_controls.pack_forget()
        if mode == WINDOW:
            self.window_sources = list_windows()
            names = list(self.window_sources)
        elif mode == SCREEN:
            self.screen_sources = list_screens()
            names = list(self.screen_sources)
        elif mode == STREAM:
            self.source_combo['values'] = ()
            self.log('Укажи HTTP/RTSP URL потока приложения телефона. Телефон и ПК должны быть доступны друг другу по сети.')
            return
        elif mode in VIDEO_MODES:
            if self.scan_flag.is_set():
                self.stop()
            self.local_capture.close()
            try:
                self.video_sources = named_video_devices()
            except (OSError, RuntimeError) as exc:
                self.video_sources = {}
                self.log(str(exc))
            names = list(self.video_sources)
            if not names:
                self.log('Windows не обнаружила видеокамеры. Включи режим веб-камеры телефона или его виртуальную камеру.')
        elif not self.obs:
            self.source_combo['values'] = ()
            self.log('Для режима OBS сначала нажми «Подключить к OBS».')
            return
        else:
            try:
                with self.obs_lock:
                    inputs = self.obs.get_input_list().inputs
                names = [item['inputName'] for item in inputs if item.get('inputName')]
                names.sort(key=str.casefold)
            except Exception as exc:
                self.events.put(('error', f'Не удалось получить источники OBS: {exc}'))
                return
        self.source_combo['values'] = names
        current = self.source_var.get()
        if current not in names:
            self.source_var.set(names[0] if names else '')
            self.video_aspect_var.set(self.video_aspects.get(self.source_var.get(), 'Авто'))
            if current:
                self.region = None
                self.display_region()
        self.log(f'Доступно источников ({mode}): {len(names)}. Выбери источник с субтитрами.')

    def capture_mode_changed(self, _event=None):
        if self.scan_flag.is_set():
            self.stop()
        self.local_capture.close()
        self.source_var.set('')
        self.region = None
        self.display_region()
        self.refresh_sources()
        self.persist()

    def source_changed(self):
        if self.scan_flag.is_set():
            self.stop()
        self.local_capture.close()
        self.video_aspect_var.set(self.video_aspects.get(self.source_var.get(), 'Авто'))
        self.region = None
        self.display_region()
        self.persist()
        self.log('Источник изменён. Выдели субтитры заново.')

    def screenshot(self, source: str, image_format: str = 'png') -> Image.Image:
        mode = self.capture_mode_var.get()
        if mode != OBS:
            frame = self.local_capture.snapshot(mode, source, self.window_sources, self.screen_sources,
                getattr(self, 'video_sources', {}),
                self.video_resolution_var.get(), self.video_fps_var.get())
            if mode in VIDEO_MODES and self.video_aspect_var.get() != 'Авто':
                numerator, denominator = map(float, self.video_aspect_var.get().split(':'))
                frame.info['sample_aspect_ratio'] = (numerator/denominator)/(frame.width/frame.height)
            info = f'{mode}: кадр {frame.width}×{frame.height}'
            if mode in (*VIDEO_MODES, WINDOW, SCREEN):
                info += '; ' + self.local_capture.info
            if info != self._last_capture_info:
                self._last_capture_info = info
                self.events.put(('log', info))
            return frame
        if self.obs is None:
            raise RuntimeError('Сначала подключись к OBS.')
        if not source:
            raise RuntimeError('Выбери источник в списке OBS.')
        with self.obs_lock:
            result = self.obs.send('GetSourceScreenshot',
                                   {'sourceName': source, 'imageFormat': 'png'}, raw=True)
        return decode_obs_frame(result['imageData'])

    def choose_region(self):
        if self.scan_flag.is_set():
            messagebox.showinfo('Останови озвучку', 'Сначала нажми «Остановить».')
            return
        self.capture_preview(select_region=True)

    def on_video_format_changed(self, _event=None):
        if self.scan_flag.is_set():
            self.stop()
        self.local_capture.close()
        if self.capture_mode_var.get() in VIDEO_MODES and self.capture_mode_var.get() != STREAM:
            self.video_aspects[self.source_var.get()] = self.video_aspect_var.get()
        self.persist()
        self.log('Видеоформат изменён. Проверь исходный кадр перед выделением области.')

    def show_source(self):
        self.capture_preview()

    def capture_preview(self, select_region=False):
        source, mode = self.source_var.get(), self.capture_mode_var.get()
        if not source:
            messagebox.showinfo('Источник', 'Выбери источник изображения.', parent=self.root)
            return
        def work():
            try:
                frame = self.screenshot(source)
                self.events.put(('select_region', (mode, source, frame)) if select_region
                                else ('source_frame', frame))
            except Exception as exc:
                self.events.put(('error', f'Не удалось получить кадр: {exc}'))
        threading.Thread(target=work, daemon=True).start()

    def open_source_frame(self, frame):
        popup = tk.Toplevel(self.root)
        popup.title(f'Исходный кадр {frame.width}×{frame.height}')
        display_width = frame.width * frame.info.get('sample_aspect_ratio', 1)
        size = min(1000/display_width, 650/frame.height, 1)
        preview = frame.resize((max(1, round(display_width*size)), max(1, round(frame.height*size))),
                               Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(preview)
        label = tk.Label(popup, image=photo)
        label.image = photo
        label.pack()

    def refresh_devices(self):
        choices = playback_devices()
        old = self.device_var.get()
        self.device_combo['values'] = choices
        self.device_var.set(old if old in choices else 'По умолчанию')

    def check_voice_refs(self):
        missing = [PROFILES[key][0] for key in PROFILES if not reference_ready(key)]
        if missing:
            messagebox.showerror('Голосовые образцы',
                'Не найдены или повреждены референсы:\n' + '\n'.join(missing), parent=self.root)
        else:
            messagebox.showinfo('Голосовые образцы',
                'Оба оригинальных VoiceDesign WAV прошли SHA-256.', parent=self.root)

    def ensure_models(self):
        try:
            absent = model_download.missing()
        except OSError as exc:
            messagebox.showerror('Qwen', str(exc), parent=self.root)
            return
        if not absent:
            messagebox.showinfo('Qwen', 'Оба файла Qwen 1.7B уже есть и прошли SHA-256.', parent=self.root)
            self.finish_first_run()
            return
        option = messagebox.askyesnocancel(
            'Файлы Qwen 1.7B',
            'Не хватает совместимых файлов Qwen.\n\n'
            'Да — загрузить около 2,37 ГБ из Serveurperso/Qwen3-TTS-GGUF на Hugging Face.\n'
            'Нет — выбрать папку с уже имеющимися файлами.\n'
            'Отмена — ничего не делать.\n\n'
            'Файлы проверяются по SHA-256. Незавершённую загрузку можно продолжить.',
            parent=self.root)
        if option is None:
            messagebox.showinfo('Qwen',
                'Загрузка отменена. Мастер настройки остаётся открытым; модель можно подготовить позже.',
                parent=self.root)
            return
        folder = None if option else filedialog.askdirectory(parent=self.root,
            title='Папка с двумя GGUF Qwen 1.7B Base')
        if option is False and not folder:
            messagebox.showinfo('Qwen',
                'Выбор папки отменён. Мастер настройки остаётся открытым.', parent=self.root)
            return
        self.model_progress = tk.Toplevel(self.root)
        self.model_progress.title('Подготовка Qwen 1.7B')
        self.model_progress.geometry('570x130')
        self.model_progress.transient(self.root)
        self.model_status = tk.StringVar(value='Проверяю файлы…')
        tk.Label(self.model_progress, textvariable=self.model_status,
                 wraplength=530).pack(fill='x', padx=15, pady=(16, 8))
        self.model_bar = ttk.Progressbar(self.model_progress, mode='determinate', maximum=100)
        self.model_bar.pack(fill='x', padx=15)
        self.model_cancel = threading.Event()
        def cancel_download():
            self.model_cancel.set()
            self.model_status.set('Останавливаю загрузку…')
        self.model_progress.protocol('WM_DELETE_WINDOW', cancel_download)
        self.btn(self.model_progress, 'Отменить', cancel_download).pack(anchor='e', padx=15, pady=8)
        def report(name, done, total):
            self.events.put(('model_progress', (name, done, total)))
        def work():
            try:
                if option:
                    model_download.download(report, self.model_cancel)
                else:
                    model_download.import_existing(Path(folder), report, self.model_cancel)
                self.events.put(('model_done', 'Qwen 1.7B готов: оба файла прошли SHA-256.'))
            except model_download.DownloadCancelled as exc:
                self.events.put(('model_cancelled', str(exc)))
            except Exception as exc:
                self.events.put(('model_error', str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def check_ocr(self):
        if self.ocr_engine_var.get() == WINDOWS_OCR:
            try:
                worker = WindowsOCR()
                worker.close()
                messagebox.showinfo('Windows OCR', 'Windows OCR и русский язык доступны.', parent=self.root)
            except Exception as exc:
                messagebox.showerror('Windows OCR', str(exc), parent=self.root)
        else:
            ready = self._tess_ready(show_error=False)
            messagebox.showinfo('Tesseract', 'Русский язык доступен.' if ready else
                'Не найден Tesseract или русский словарь. Выбери путь и установи rus.traineddata.', parent=self.root)

    def first_run(self):
        previous = getattr(self, 'setup_window', None)
        if previous is not None and previous.winfo_exists():
            previous.lift()
            return
        win = tk.Toplevel(self.root)
        self.setup_window = win
        win.title('Первоначальная настройка Lyona')
        win.geometry('650x490')
        win.configure(bg=COLORS['bg'])
        win.transient(self.root)
        tk.Label(win, text='Первоначальная настройка', bg=COLORS['bg'], fg=COLORS['green'],
                 font=('Segoe UI', 18, 'bold')).pack(anchor='w', padx=20, pady=(18, 8))
        steps = [
            ('1. Выбери OBS, видеокарту захвата, окно или экран в основном окне.',
             lambda: self.refresh_sources()),
            ('2. Для OBS подключи WebSocket. Выбери источник и выдели область.',
             lambda: self.choose_region()),
            ('3. Выбери Windows OCR или Tesseract; проверь русский язык.', self.check_ocr),
            ('4. Выбери женский или мужской голос; проверь референсы.', self.check_voice_refs),
            ('5. Проверь имеющиеся GGUF или согласись на загрузку.', self.ensure_models),
            ('6. Выбери аудиоустройство в основном окне и нажми «Послушать».', self.test_voice),
        ]
        for text, command in steps:
            row = tk.Frame(win, bg=COLORS['panel'], pady=7, padx=10)
            row.pack(fill='x', padx=18, pady=3)
            tk.Label(row, text=text, bg=COLORS['panel'], fg=COLORS['fg'],
                     wraplength=470, justify='left').pack(side='left', fill='x', expand=True)
            self.btn(row, 'Проверить', command).pack(side='right')
        self.btn(win, 'Готово', self.finish_first_run, accent=True).pack(side='right', padx=20, pady=12)

    def finish_first_run(self):
        win = getattr(self, 'setup_window', None)
        parent = win if win is not None and win.winfo_exists() else self.root
        try:
            absent = model_download.missing()
        except OSError as exc:
            messagebox.showerror('Файлы Qwen', str(exc), parent=parent)
            return False
        if absent:
            messagebox.showwarning('Qwen ещё не готов',
                'Сначала загрузи модели или укажи папку с уже скачанными GGUF.\n'
                'Загрузка начнётся только после твоего согласия.', parent=parent)
            return False
        if not all(reference_ready(key) for key in PROFILES):
            messagebox.showerror('Голосовые образцы',
                'Не найдены исходные референсы. Распакуй архив полностью.', parent=parent)
            return False
        self.setup_complete = True
        self.persist()
        if win is not None and win.winfo_exists():
            win.destroy()
        self.setup_window = None
        self.root.lift()
        return True

    def choose_tesseract(self):
        path = filedialog.askopenfilename(title='Укажи tesseract.exe',
                                          filetypes=[('Tesseract', 'tesseract.exe'), ('EXE', '*.exe')])
        if path:
            self.tess_path = path
            pytesseract.pytesseract.tesseract_cmd = path
            self.persist()
            self.log('Tesseract найден.')

    def _tess_ready(self, show_error=True):
        self.tess_path = find_tesseract(self.tess_path or '')
        if not self.tess_path:
            if show_error:
                messagebox.showerror('Tesseract не найден',
                                     'В разделе «Распознавание» нажми «Путь к Tesseract».')
            return None
        pytesseract.pytesseract.tesseract_cmd = self.tess_path
        try:
            return language_config()
        except Exception as exc:
            if show_error:
                messagebox.showerror('Русский язык', str(exc))
            return None

    def download_russian(self):
        if not self.tess_path:
            messagebox.showerror('Нет Tesseract', 'Сначала укажи tesseract.exe.')
            return
        if (TESS_LOCAL / 'rus.traineddata').exists():
            messagebox.showinfo('Русский словарь', 'Он уже установлен.')
            return
        self.log('Загружаю русский словарь…')
        def work():
            try:
                TESS_LOCAL.mkdir(parents=True, exist_ok=True)
                with urlopen(TRAINING_DATA_URL, timeout=30) as response:
                    data = response.read(15_000_000)
                if len(data) < 100_000:
                    raise ValueError('Получен неполный словарь.')
                (TESS_LOCAL / 'rus.traineddata').write_bytes(data)
                self.events.put(('log', 'Словарь загружен.'))
            except Exception as exc:
                self.events.put(('error', f'Ошибка загрузки словаря: {exc}'))
        threading.Thread(target=work, daemon=True).start()

    def settings_snapshot(self):
        try:
            interval = float(self.interval_var.get())
        except ValueError:
            interval = 0.55
        voice_choice = self.voice_choice_var.get()
        backend, variant = VOICE_OPTIONS[voice_choice]
        return {
            'capture_mode': self.capture_mode_var.get(),
            'video_resolution': self.video_resolution_var.get(),
            'video_fps': self.video_fps_var.get(),
            'video_aspect': self.video_aspect_var.get(),
            'video_aspects': dict(self.video_aspects),
            'setup_complete': bool(getattr(self, 'setup_complete', False)),
            'obs_region': dict(self.region) if self.region else None,
            'obs_source': self.source_var.get(),
            'obs_port': self.port_var.get(),
            'voice': backend,
            'voice_name': voice_choice,
            'voice_choice': voice_choice,
            'qwen_model': variant,
            'qwen_tempo': TEMPO_LABELS.get(self.qwen_tempo_var.get(), 1.05),
            'qwen_gain': self.qwen_levels[0],
            'qwen_volume': round(self.qwen_volume_var.get()),
            'qwen_normalize': self.qwen_levels[1],
            'pronunciation_enabled': self.pronunciation_var.get(),
            'device': self.device_var.get(),
            'contrast': self.contrast_var.get(),
            'ocr_mode': self.ocr_mode_var.get(),
            'ocr_engine': self.ocr_engine_var.get(),
            'scan_profile': self.scan_profile_var.get(),
            'interval': max(0.15, min(3.0, interval)),
            'stability_frames': int(self.frames_var.get()) if self.frames_var.get() in ('1', '2') else 2,
            'tesseract': self.tess_path,
        }

    def persist(self):
        try:
            # The OBS password is intentionally never stored in settings.json.
            settings = self.settings_snapshot()
            if settings['capture_mode'] == STREAM:
                from urllib.parse import urlsplit
                address = urlsplit(settings['obs_source'])
                if address.username or address.password or address.query:
                    settings['obs_source'] = ''
            CONFIG_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                                   encoding='utf-8')
        except OSError as exc:
            self.log(f'Не получилось сохранить настройки: {exc}')

    def on_voice_changed(self, _event=None):
        if self.scan_flag.is_set():
            self.stop()
            self.log('Голос переключён. Нажми «НАЧАТЬ ОЗВУЧКУ» повторно.')
        else:
            self.qwen.stop()
        self.persist()
        self.log('Голос переключён. Нажми «Послушать».')

    def retry_cuda(self):
        cfg = self.settings_snapshot()
        def work():
            try:
                self.qwen.retry_cuda(str(DATA_DIR), cfg['device'], cfg['qwen_model'])
            except Exception as exc:
                self.events.put(('log', 'Повторная инициализация CUDA не удалась: ' + str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def prepare_qwen(self):
        cfg = self.settings_snapshot()
        try:
            validate_voice_folder(str(DATA_DIR))
            self.qwen.prepare(str(DATA_DIR), cfg['device'], cfg['qwen_model'])
            self.persist()
        except Exception as exc:
            messagebox.showerror('Qwen', str(exc), parent=self.root)

    def on_qwen_tempo_changed(self, _event=None):
        # The current phrase finishes; the next queued phrase uses this speed.
        self.live_qwen_tempo = TEMPO_LABELS[self.qwen_tempo_var.get()]
        self.persist()
        if self.scan_flag.is_set():
            self.log('Новый темп применится к следующей реплике.')

    def on_scan_profile_changed(self, _event=None):
        profile = self.scan_profile_var.get()
        if profile in SCAN_PROFILES:
            interval, frames, _format, _quiet = SCAN_PROFILES[profile]
            self.interval_var.set(f'{interval:.2f}')
            self.frames_var.set(str(frames))
        self.persist()
        if self.scan_flag.is_set():
            self.log('Режим распознавания применится после остановки и нового запуска озвучки.')

    def on_ocr_engine_changed(self, _event=None):
        if self.ocr_engine_var.get() == TESSERACT_OCR and not self._tess_ready():
            self.ocr_engine_var.set(WINDOWS_OCR)
            return
        self.active_ocr_engine = self.ocr_engine_var.get()
        self.persist()
        if self.scan_flag.is_set():
            self.log('Движок переключится на следующем кадре: ' + self.active_ocr_engine)

    def on_scan_manual_changed(self, _event=None):
        self.scan_profile_var.set(CUSTOM_SCAN_PROFILE)
        self.persist()
        if self.scan_flag.is_set():
            self.log('Новые параметры OCR применятся после остановки и нового запуска озвучки.')

    def on_qwen_levels_changed(self, *_args):
        db = self.qwen_gain_db_var.get()
        self.qwen_gain_label.set(f'+{db:.1f} дБ')
        self.qwen_levels = 10 ** (db / 20), self.qwen_normalize_var.get()
        if getattr(self, '_save_qwen_levels', None) is not None:
            self.root.after_cancel(self._save_qwen_levels)
        self._save_qwen_levels = self.root.after(300, self.persist)

    def on_qwen_volume_changed(self, *_args):
        value = max(0.0, min(100.0, self.qwen_volume_var.get()))
        self.qwen_volume = value / 100.0
        self.qwen_volume_label.set(f'{value:.0f}%')
        if getattr(self, '_save_qwen_volume', None) is not None:
            self.root.after_cancel(self._save_qwen_volume)
        self._save_qwen_volume = self.root.after(300, self.persist)

    def on_pronunciation_changed(self):
        self.persist()
        if self.scan_flag.is_set():
            self.log('Обработку произношения применим при следующем запуске озвучки.')

    def open_pronunciation_dictionary(self):
        path = DATA_DIR / 'pronunciation_dictionary.json'
        if not path.is_file():
            messagebox.showerror('Словарь', 'Не найден pronunciation_dictionary.json. '
                                 'Запусти программу заново.', parent=self.root)
            return
        try:
            if sys.platform == 'win32':
                subprocess.Popen(['notepad.exe', str(path)])
            else:
                messagebox.showinfo('Словарь', f'Открой файл:\n{path}', parent=self.root)
        except OSError as exc:
            messagebox.showerror('Словарь', str(exc), parent=self.root)

    def deliver_speech(self, text, cfg, flag=None, preview=False):
        with self.speech_lock:
            if flag is not None and (flag is not self.scan_flag or not flag.is_set()):
                return
            self._deliver_speech(text, cfg, preview=preview)

    def _deliver_speech(self, text, cfg, preview=False):
        try:
            gain, normalize = self.qwen_levels
            self.qwen.say(text, str(DATA_DIR), cfg['device'], self.live_qwen_tempo,
                          gain * self.qwen_volume,
                          cfg['pronunciation_enabled'], model=cfg['qwen_model'],
                          normalize=normalize, preview=preview)
        except Exception as exc:
            self.qwen._event('Ошибка передачи текста в голосовой движок:\n' + traceback.format_exc())

    def test_voice(self):
        cfg = self.settings_snapshot()
        try:
            voice_profile(str(DATA_DIR), cfg['qwen_model'])
        except (FileNotFoundError, ValueError) as exc:
            messagebox.showerror('Голос не готов', str(exc), parent=self.root)
            return
        self.persist()
        self.deliver_speech('Привет! Что здесь происходит? Погоди, кажется, начинается самое интересное!',
                            cfg, preview=True)
        self.log(f'Проверяю голос: {cfg["voice_name"]}.')

    def show_crop(self):
        if not self.source_var.get() or not self.region:
            messagebox.showinfo('Вырезка', 'Выбери источник и область субтитров.')
            return
        cfg = self.settings_snapshot()
        def work():
            try:
                frame = self.screenshot(cfg['obs_source'])
                cropped = crop_fractional(frame, cfg['obs_region'])
                self.events.put(('crop', cropped))
            except Exception as exc:
                self.events.put(('error', f'Не удалось показать вырезку: {exc}'))
        threading.Thread(target=work, daemon=True).start()

    def open_crop_preview(self, cropped):
        popup = tk.Toplevel(self.root)
        popup.title('Только эта часть картинки распознаётся')
        popup.configure(bg=COLORS['bg'])
        tk.Label(popup, text='Широкую рамку оставляем. Новый OCR сам отделяет белые буквы от фона.',
                 fg=COLORS['green'], bg=COLORS['bg'],
                 font=('Segoe UI', 11, 'bold')).pack(padx=12, pady=12)
        display_width = cropped.width * cropped.info.get('sample_aspect_ratio', 1)
        scale = min(3.0, 850 / max(1, display_width), 650 / max(1, cropped.height))
        img = cropped.resize((int(display_width * scale), int(cropped.height * scale)),
                             Image.Resampling.NEAREST)
        photo = ImageTk.PhotoImage(img)
        label = tk.Label(popup, image=photo, bg=COLORS['panel'])
        label.image = photo
        label.pack(padx=12, pady=(0, 12))
        tk.Button(popup, text='Закрыть', command=popup.destroy).pack(pady=(0, 12))

    def compare_ocr(self):
        """Compare both filters on exactly one identical OBS frame."""
        if not self.region:
            messagebox.showinfo('Область', 'Сначала выдели область субтитров.')
            return
        ready = self._tess_ready()
        if not ready:
            return
        cfg = self.settings_snapshot()
        self.persist()
        self.log('Сравниваю оба способа на одном кадре из OBS…')
        def work():
            try:
                frame = self.screenshot(cfg['obs_source'])
                cropped = crop_fractional(frame, cfg['obs_region'])
                self.events.put(('ocr_compare', compare(cropped, *ready)))
            except Exception as exc:
                self.events.put(('error', f'Ошибка сравнения OCR: {exc}'))
        threading.Thread(target=work, daemon=True).start()

    def show_ocr_comparison(self, results):
        win = tk.Toplevel(self.root)
        win.title('Сравнение распознавания — один кадр OBS')
        win.configure(bg=COLORS['bg'])
        photos = []
        for item in results:
            panel = tk.Frame(win, bg=COLORS['panel'], padx=12, pady=8)
            panel.pack(fill='x', padx=12, pady=6)
            tk.Label(panel, text=f'{item.method}  |  {item.elapsed:.2f} сек  |  уверенность {item.confidence:.0f}%',
                     bg=COLORS['panel'], fg=COLORS['green'], anchor='w').pack(fill='x')
            tk.Label(panel, text=item.text or 'Текст не найден', bg=COLORS['panel'],
                     fg=COLORS['fg'], justify='left', anchor='w', wraplength=780).pack(fill='x', pady=4)
            if item.method == WHITE:
                preview = item.preview
                ratio = min(1.0, 760 / max(1, preview.width))
                img = preview.resize((max(1, int(preview.width*ratio)),
                                      max(1, int(preview.height*ratio))), Image.Resampling.NEAREST)
                photo = ImageTk.PhotoImage(img)
                photos.append(photo)
                tk.Label(panel, image=photo, bg=COLORS['black']).pack(anchor='w', pady=6)
        win._photo_refs = photos
        tk.Button(win, text='Закрыть', command=win.destroy).pack(pady=(0, 10))

    def test_ocr(self):
        if not self.region:
            messagebox.showinfo('Область', 'Сначала выдели область субтитров на кадре из OBS.')
            return
        cfg = self.settings_snapshot()
        ready = self._tess_ready() if cfg['ocr_engine'] == TESSERACT_OCR else None
        if cfg['ocr_engine'] == TESSERACT_OCR and not ready:
            return
        self.persist()
        self.log('Считываю кадр из OBS и распознаю субтитры…')
        def work():
            try:
                frame = self.screenshot(cfg['obs_source'])
                cropped = crop_fractional(frame, cfg['obs_region'])
                if cfg['ocr_engine'] == WINDOWS_OCR:
                    worker = WindowsOCR()
                    try:
                        found = worker.recognize(cropped)
                    finally:
                        worker.close()
                else:
                    found = extract_subtitles(cropped, cfg['contrast'], *ready, mode=cfg['ocr_mode'])
                self.events.put(('text', found))
                self.events.put(('log', 'Проверка закончена.'))
            except Exception as exc:
                self.events.put(('error', f'Ошибка проверки субтитров: {exc}'))
        threading.Thread(target=work, daemon=True).start()

    def start(self):
        if self.scan_flag.is_set():
            return
        if ((self.capture_mode_var.get() == OBS and not self.obs)
                or not self.source_var.get() or not self.region):
            messagebox.showinfo('Настройка',
                                'Выбери источник изображения и выдели область субтитров.')
            return
        ready = self._tess_ready() if self.ocr_engine_var.get() == TESSERACT_OCR else None
        if self.ocr_engine_var.get() == TESSERACT_OCR and not ready:
            return
        cfg = self.settings_snapshot()
        try:
            voice_profile(str(DATA_DIR), cfg['qwen_model'])
            self.qwen.prepare(str(DATA_DIR), cfg['device'], cfg['qwen_model'])
        except Exception as exc:
            messagebox.showerror('Голос не готов', str(exc), parent=self.root)
            return
        flag = threading.Event()
        flag.set()
        self.scan_flag = flag
        self.persist()
        self._set_running(True)
        self.log(f'Распознавание: {self.active_ocr_engine}, '
                 f'подтверждение {cfg["stability_frames"]} кадр(а).')
        threading.Thread(target=self.scan, args=(cfg, ready, flag), daemon=True).start()

    def scan(self, cfg, ready, flag):
        settle = SCAN_PROFILES.get(cfg.get('scan_profile'), (0, 0, 'png', 0.25))[3]
        tracker = SubtitleTracker(frames=cfg['stability_frames'], repeat_seconds=12.0,
                                  settle_seconds=settle)
        frames: Queue = Queue(maxsize=1)
        capture_done = threading.Event()
        dropped = [0]
        image_format = SCAN_PROFILES.get(cfg.get('scan_profile'), (0, 0, 'png', 0.25))[2]
        interval = cfg['interval']
        last_text = None
        last_filter_notice = 0.0
        last_mask = None
        last_ocr = ''
        windows_worker = None

        def capture_loop():
            errors = 0
            capture_format = image_format
            try:
                while flag.is_set():
                    started = time.monotonic()
                    try:
                        frame = self.screenshot(cfg['obs_source'], capture_format)
                        captured = time.monotonic()
                        cropped = crop_fractional(frame, cfg['obs_region'])
                        errors = 0
                    except Exception as exc:
                        if cfg['capture_mode'] == OBS and capture_format == 'jpg':
                            capture_format = 'png'
                            self.events.put(('log',
                                'Быстрый формат кадра недоступен; продолжаю в PNG.'))
                            continue
                        errors += 1
                        if errors <= 3:
                            self.events.put(('log', 'Ошибка получения кадра: ' + str(exc)[:220]))
                        if errors >= 6:
                            self.events.put(('error', 'Не получается получить кадр. Проверь источник изображения.'))
                            flag.clear()
                            break
                        time.sleep(0.8)
                        continue
                    if not flag.is_set():
                        break
                    item = (cropped, started, captured, capture_format)
                    try:
                        frames.put_nowait(item)
                    except Full:
                        try:
                            frames.get_nowait()
                            dropped[0] += 1
                        except Empty:
                            pass
                        frames.put_nowait(item)
                    remaining = interval - (time.monotonic() - started)
                    if remaining > 0:
                        deadline = time.monotonic() + remaining
                        while flag.is_set() and time.monotonic() < deadline:
                            time.sleep(min(0.03, max(0.005, deadline-time.monotonic())))
                    else:
                        time.sleep(0.01)
            finally:
                capture_done.set()

        threading.Thread(target=capture_loop, daemon=True).start()
        ocr_errors = 0
        try:
            while flag.is_set() or not capture_done.is_set():
                try:
                    cropped, started, captured, capture_format = frames.get(timeout=0.15)
                except Empty:
                    continue
                if not flag.is_set():
                    break
                ocr_started = time.monotonic()
                cached = False
                selected_engine = getattr(self, 'active_ocr_engine',
                                          cfg.get('ocr_engine', TESSERACT_OCR))
                try:
                    if selected_engine == TESSERACT_OCR and ready is None:
                        ready = self._tess_ready(show_error=False)
                        if ready is None:
                            raise RuntimeError('Tesseract или русский словарь не найден')
                    if selected_engine == WINDOWS_OCR:
                        if windows_worker is None:
                            windows_worker = WindowsOCR()
                        found = windows_worker.recognize(cropped)
                    elif cfg['ocr_mode'] == WHITE:
                        prepared = white_mask(cropped)
                        signature = hashlib.blake2b(prepared.tobytes(), digest_size=12).digest()
                        if signature == last_mask:
                            found = last_ocr
                            cached = True
                        else:
                            found = recognize(cropped, *ready, mode=WHITE,
                                              white_preview=prepared).text
                            last_mask, last_ocr = signature, found
                    else:
                        found = recognize(cropped, *ready, mode=cfg['ocr_mode']).text
                except Exception as exc:
                    if selected_engine == WINDOWS_OCR:
                        ready = self._tess_ready(show_error=False)
                        if ready:
                            self.active_ocr_engine = TESSERACT_OCR
                            self.events.put(('ocr_fallback', str(exc)[:160]))
                            continue
                        self.events.put(('error', 'Windows OCR недоступен, а Tesseract не установлен: ' + str(exc)[:160]))
                        flag.clear()
                        break
                    self.events.put(('log', 'Ошибка OCR: ' + str(exc)[:220]))
                    ocr_errors += 1
                    if ocr_errors >= 6:
                        self.events.put(('error', 'Tesseract не распознаёт кадры. '
                                                   'Проверь путь и русский словарь.'))
                        flag.clear()
                    continue
                ocr_errors = 0
                recognized = time.monotonic()
                if selected_engine != getattr(self, 'active_ocr_engine', selected_engine):
                    continue
                if not flag.is_set():
                    break
                if found != last_text:
                    self.events.put(('text', found))
                    last_text = found
                spoken = tracker.observe(found, recognized)
                if found and not spoken and recognized-last_filter_notice >= 5:
                    last_filter_notice = recognized
                    self.qwen._event(f'OCR есть, в TTS не отправлено: '
                        f'{len(found)} знаков; подтверждение {tracker.count}/{tracker.frames}; '
                        f'Qwen ready={self.qwen.ready}, busy={self.qwen.busy}. '
                        'Ожидание стабилизации или фильтр повторов.')
                confirmed = time.monotonic()
                dispatch_ms = 0.0
                confirm_ms = 0.0
                if spoken:
                    self.events.put(('read', spoken))
                    confirm_ms = (confirmed - tracker.last_detection_at) * 1000
                    self.deliver_speech(spoken, cfg, flag)
                    dispatch_ms = (time.monotonic() - confirmed) * 1000
                    self.events.put(('log',
                        f'Задержка: OBS {(captured-started)*1000:.0f} мс; '
                        f'OCR {(recognized-ocr_started)*1000:.0f} мс'
                        f'{" (кэш)" if cached else ""}; '
                        f'подтверждение {confirm_ms:.0f} мс; '
                        f'передача голосу {dispatch_ms:.1f} мс.'))
                append_ocr_timing({
                    'unix_time': round(time.time(), 3),
                    'profile': cfg.get('scan_profile', CUSTOM_SCAN_PROFILE),
                    'image_format': capture_format,
                    'obs_ms': round((captured-started)*1000, 2),
                    'frame_queue_ms': round((ocr_started-captured)*1000, 2),
                    'ocr_ms': round((recognized-ocr_started)*1000, 2),
                    'ocr_cached': int(cached),
                    'nonempty': int(bool(found)),
                    'accepted': int(bool(spoken)),
                    'confirm_ms': round(confirm_ms, 2),
                    'dispatch_ms': round(dispatch_ms, 2),
                    'dropped_frames': dropped[0],
                })
        finally:
            if windows_worker is not None:
                try:
                    windows_worker.close()
                except OSError:
                    pass
            flag.clear()
            if self.scan_flag is flag:
                self.events.put(('stopped', flag))

    def stop(self):
        with self.speech_lock:
            self.scan_flag.clear()
            self.qwen.stop()
            self.local_capture.close()
        self._set_running(False)
        self.persist()
        self.log('Остановлено.')

    def on_close(self):
        with self.speech_lock:
            self.scan_flag.clear()
            self.qwen.shutdown()
            self.local_capture.close()
        self.persist()
        self.root.destroy()


def main():
    if '--qwen-diagnostics' in sys.argv:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        from qwen_native_startup import diagnose
        rows, failures = diagnose()
        print('\n'.join(rows), flush=True)
        sys.exit(1 if failures else 0)
    if len(sys.argv) > 1 and sys.argv[1] == '--qwen-worker':
        from qwen_obs_worker import main as worker_main, emit
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        try:
            worker_main()
        except Exception as exc:
            import traceback
            emit('error', message='Ошибка запуска Qwen: ' + str(exc), details=traceback.format_exc())
            sys.exit(1)
        return
    enable_dpi_awareness()
    root = tk.Tk()
    SubtitleApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
