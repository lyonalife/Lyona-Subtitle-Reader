"""Local Windows capture modes. OBS capture remains in subtitle_reader.py."""
from __future__ import annotations

import ctypes
import threading
import time
from video_devices import list_video_devices
from window_frame import WindowFrame

from PIL import Image, ImageGrab

OBS = 'OBS WebSocket'
CAMERA = 'Видеокарта захвата'
WINDOW = 'Окно Windows'
SCREEN = 'Экран Windows'
PHONE = 'Камера телефона / веб-камера'
STREAM = 'Телефон / IP-видеопоток'
VIDEO_MODES = (CAMERA, PHONE, STREAM)
MODES = (OBS, SCREEN, WINDOW, CAMERA, PHONE, STREAM)


def named_video_devices():
    from hashlib import sha256
    devices = list_video_devices()
    result = {}
    for d in devices:
        label = d.name
        if sum(item.name == d.name for item in devices) > 1:
            label += ' [' + sha256(d.symbolic_link.encode()).hexdigest()[:8] + ']'
        result[label] = d
    return result


def list_windows() -> dict[str, int]:
    if not hasattr(ctypes, 'windll'):
        return {}
    user32 = ctypes.windll.user32
    result = {}
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _extra):
        if not user32.IsWindowVisible(hwnd) or not user32.GetWindowTextLengthW(hwnd):
            return True
        title = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        text = title.value.strip()
        if text:
            result[f'{text[:90]}  [0x{hwnd:X}]'] = int(hwnd)
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return dict(sorted(result.items(), key=lambda item: item[0].casefold()))


class RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


def list_screens() -> dict[str, tuple[int, int, int, int]]:
    if not hasattr(ctypes, 'windll'):
        return {}
    result = {}
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.POINTER(RECT), ctypes.c_void_p)

    def visit(_monitor, _hdc, rect, _extra):
        area = rect.contents
        box = (area.left, area.top, area.right, area.bottom)
        result[f'Экран {len(result) + 1}: {area.right-area.left}×{area.bottom-area.top}'] = box
        return True

    ctypes.windll.user32.EnumDisplayMonitors(0, 0, callback_type(visit), 0)
    return result


def window_box(hwnd: int) -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        raise RuntimeError('Окно закрыто или свёрнуто. Разверни его и обнови список.')
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError('Не удалось получить координаты окна.')
    if rect.right - rect.left < 20 or rect.bottom - rect.top < 20:
        raise RuntimeError('Окно слишком маленькое для распознавания.')
    return rect.left, rect.top, rect.right, rect.bottom


class LocalCapture:
    def __init__(self):
        self.camera = None
        self.camera_index = None
        self.lock = threading.RLock()
        self.latest = None
        self.capture_thread = None
        self.stop_event = threading.Event()
        self.info = ''
        self.window = WindowFrame()
        self.window_fallback = set()
        self.screen_fallback = set()

    def close(self):
        with self.lock:
            self.window.close()
            self.window_fallback.clear()
            self.screen_fallback.clear()
            self.stop_event.set()
            if self.capture_thread:
                self.capture_thread.join(timeout=3)
                if self.capture_thread.is_alive():
                    raise RuntimeError('Видеодрайвер не завершил чтение. Закрой приложение перед повторным открытием устройства.')
            if self.camera is not None:
                self.camera.release()
            self.camera = None
            self.camera_index = None
            self.capture_thread = None
            self.latest = None

    def snapshot(self, mode: str, choice: str, windows: dict, screens: dict,
                 videos=None, resolution='Авто', fps='Авто') -> Image.Image:
        if mode == WINDOW:
            hwnd = windows.get(choice)
            if hwnd is None:
                raise RuntimeError('Выбери доступное окно и обнови список источников.')
            box = window_box(hwnd)
            with self.lock:
                if hwnd not in self.window_fallback:
                    try:
                        frame = self.window.snapshot(hwnd)
                        if any(high > 4 for _, high in frame.getextrema()):
                            self.info = 'Windows Graphics Capture, исходное окно' + self.window.border_notice
                            return frame
                        self.info = 'WGC вернул чёрный кадр. Используется видимая область окна.'
                    except RuntimeError as exc:
                        self.info = 'Резервный видимый кадр окна: ' + str(exc)
                    self.window.close()
                    self.window_fallback.add(hwnd)
            return ImageGrab.grab(bbox=box, all_screens=True).convert('RGB')
        if mode == SCREEN:
            box = screens.get(choice)
            if box is None:
                raise RuntimeError('Выбери экран и обнови список источников.')
            if choice in self.screen_fallback:
                return ImageGrab.grab(bbox=box, all_screens=True).convert('RGB')
            try:
                frame = self.window.snapshot_monitor(list(screens).index(choice)+1)
                self.info = 'Windows Graphics Capture, исходный монитор' + self.window.border_notice
                return frame
            except RuntimeError as exc:
                self.window.close()
                self.info = 'Резервный захват экрана: ' + str(exc)
                self.screen_fallback.add(choice)
                return ImageGrab.grab(bbox=box, all_screens=True).convert('RGB')
        if mode not in VIDEO_MODES:
            raise ValueError('Неизвестный способ захвата: ' + mode)
        with self.lock:
            import cv2
            selected = (videos or {}).get(choice)
            if mode == STREAM:
                from urllib.parse import urlsplit
                if urlsplit(choice).scheme not in ('http', 'https', 'rtsp'):
                    raise RuntimeError('Укажи HTTP/HTTPS/RTSP адрес видеопотока приложения телефона.')
                identity = (choice, resolution, fps)
            else:
                if selected is None:
                    raise RuntimeError('Видеоустройство не найдено. Подключи его и обнови список.')
                identity = (selected.symbolic_link, resolution, fps)
            if self.camera is None or self.camera_index != identity:
                self.close()
                if mode == STREAM:
                    routes = [(choice, cv2.CAP_FFMPEG)]
                else:
                    current = next((d for d in list_video_devices() if d.symbolic_link == selected.symbolic_link), None)
                    if current is None:
                        raise RuntimeError('Устройство отключено. Обнови список.')
                    routes = [(current.index, cv2.CAP_MSMF if current.backend == 'msmf' else cv2.CAP_DSHOW)]
                    if current.fallback_index is not None:
                        routes.insert(0, (current.fallback_index, cv2.CAP_DSHOW))
                for index, backend in routes:
                    if mode == STREAM:
                        device = cv2.VideoCapture(index, backend,
                            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 4000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000])
                    else:
                        device = cv2.VideoCapture(index, backend)
                    if device.isOpened():
                        if resolution != 'Авто':
                            w, h = map(int, resolution.lower().split('x'))
                            device.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                            device.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                        elif mode == CAMERA and 'elgato' in selected.name.casefold():
                            # Elgato drivers often default to a 640x480 output
                            # although the HDMI input is 16:9. Negotiate HD frames.
                            device.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
                            device.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
                        device.set(cv2.CAP_PROP_FPS, float(fps) if fps != 'Авто' else 15)
                        ok, frame = device.read()
                        if ok and frame is not None:
                            self.camera, self.camera_index = device, identity
                            actual_fps = device.get(cv2.CAP_PROP_FPS)
                            if fps != 'Авто' and actual_fps > 0 and abs(actual_fps-float(fps)) > 0.2:
                                self.camera = self.camera_index = None
                                device.release()
                                raise RuntimeError(f'Драйвер выбрал {actual_fps:g} FPS вместо {fps}. Выбери доступный FPS или Авто.')
                            self.info = f'{frame.shape[1]}×{frame.shape[0]}, драйвер FPS {actual_fps:g}, {device.getBackendName()}'
                            self.latest = (frame, time.monotonic())
                            break
                    device.release()
                if self.camera is None:
                    raise RuntimeError('Нет видеокадра. Проверь сигнал, формат, разрешение Windows на камеру и занятость устройства OBS, Camo или другой программой.')
                self.stop_event = threading.Event()
                stop, device = self.stop_event, self.camera

                def read_latest():
                    while not stop.is_set():
                        ok, frame = device.read()
                        if not ok or frame is None:
                            return
                        now = time.monotonic()
                        if self.latest is None or now-self.latest[1] >= 0.15:
                            self.latest = frame, now
                        stop.wait(0.001)

                self.capture_thread = threading.Thread(target=read_latest, daemon=True)
                self.capture_thread.start()
            item = self.latest
            if item is None or time.monotonic() - item[1] > 3:
                raise RuntimeError('Видеопоток перестал выдавать свежие кадры. Проверь сигнал и подключение.')
            frame = item[0]
            if resolution != 'Авто':
                w, h = map(int, resolution.lower().split('x'))
                if (frame.shape[1], frame.shape[0]) != (w, h):
                    actual = frame.shape[1], frame.shape[0]
                    self.close()
                    raise RuntimeError(f'Драйвер вернул {actual[0]}×{actual[1]} вместо {w}×{h}. Выбери доступное разрешение или Авто.')
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            numerator = self.camera.get(cv2.CAP_PROP_SAR_NUM)
            denominator = self.camera.get(cv2.CAP_PROP_SAR_DEN)
            if numerator > 0 and denominator > 0 and 0.25 <= numerator/denominator <= 4:
                image.info['sample_aspect_ratio'] = numerator/denominator
            return image
