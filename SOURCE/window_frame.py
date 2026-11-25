"""Capture a single HWND with Windows Graphics Capture."""
from threading import Condition, RLock, Timer
from time import monotonic

from PIL import Image


def request_borderless_access():
    """Request Windows consent in this process, not in a helper executable."""
    import ctypes as c
    import uuid
    from time import sleep
    api = c.WinDLL('combase')
    api.RoInitialize.argtypes = [c.c_uint]
    api.RoInitialize.restype = c.c_long
    api.WindowsCreateString.argtypes = [c.c_wchar_p, c.c_uint, c.POINTER(c.c_void_p)]
    api.WindowsCreateString.restype = c.c_long
    api.WindowsDeleteString.argtypes = [c.c_void_p]
    api.RoGetActivationFactory.argtypes = [c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]
    api.RoGetActivationFactory.restype = c.c_long
    def check(hr):
        if hr < 0:
            raise OSError(f'WinRT HRESULT 0x{hr & 0xffffffff:08X}')
    def method(ptr, slot, result, *args):
        table = c.cast(ptr, c.POINTER(c.POINTER(c.c_void_p))).contents
        return c.WINFUNCTYPE(result, c.c_void_p, *args)(table[slot])
    def guid(value):
        return (c.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)
    initialized = api.RoInitialize(1)
    if initialized < 0 and initialized != -2147417850:  # Existing STA is valid.
        check(initialized)
    name = c.c_void_p()
    factory, operation, info = c.c_void_p(), c.c_void_p(), c.c_void_p()
    try:
        cls = 'Windows.Graphics.Capture.GraphicsCaptureAccess'
        check(api.WindowsCreateString(cls, len(cls), c.byref(name)))
        iid = guid('743ed370-06ec-5040-a58a-901f0f757095')
        check(api.RoGetActivationFactory(name, iid, c.byref(factory)))
        # GraphicsCaptureAccessKind.Borderless = 0.
        check(method(factory, 6, c.c_long, c.c_int, c.POINTER(c.c_void_p))(
            factory, 0, c.byref(operation)))
        iid_async = guid('00000036-0000-0000-c000-000000000046')
        check(method(operation, 0, c.c_long, c.c_void_p, c.POINTER(c.c_void_p))(
            operation, iid_async, c.byref(info)))
        deadline = monotonic()+120
        status = c.c_int()
        while True:
            check(method(info, 7, c.c_long, c.POINTER(c.c_int))(info, c.byref(status)))
            if status.value != 0:
                break
            if monotonic() >= deadline:
                method(info, 9, c.c_long)(info)
                raise TimeoutError('Ожидание разрешения Windows превысило 120 секунд')
            sleep(0.05)
        result = c.c_int()
        check(method(operation, 8, c.c_long, c.POINTER(c.c_int))(operation, c.byref(result)))
        # AppCapabilityAccessStatus.Allowed = 4.
        return result.value == 4, result.value
    finally:
        for pointer in (info, operation, factory):
            if pointer:
                method(pointer, 2, c.c_ulong)(pointer)
        if name:
            api.WindowsDeleteString(name)
        if initialized >= 0:
            api.RoUninitialize()


class WindowFrame:
    def __init__(self):
        self.border_checked = False
        self.borderless = False
        self.border_notice = ""
        self.lock = RLock()
        self.last_request = 0.0
        self.timer = None
        self.hwnd = None
        self.control = None
        self.changed = Condition()
        self.frame = None
        self.closed = False

    def close(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        control, self.control = self.control, None
        if control is not None and not control.is_finished():
            control.stop()
        with self.changed:
            self.hwnd = self.frame = None
            self.closed = False

    def snapshot(self, hwnd):
        return self._snapshot(('window', hwnd))

    def snapshot_monitor(self, index):
        return self._snapshot(('monitor', index))

    def _expire(self):
        with self.lock:
            if monotonic()-self.last_request >= 3:
                self.close()
            elif self.control is not None:
                self._schedule_expiry()

    def _schedule_expiry(self):
        self.timer = Timer(1.0, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _snapshot(self, target):
        with self.lock:
            self.last_request = monotonic()
            if self.hwnd != target or self.closed:
                self.close()
                self._start(target)
                self._schedule_expiry()
            with self.changed:
                ready = self.changed.wait_for(lambda: self.frame is not None or self.closed, timeout=2)
                if not ready or self.closed:
                    raise RuntimeError('WGC не получил кадр за 2 секунды или источник закрылся.')
                if self.control.is_finished():
                    raise RuntimeError('Сеанс WGC завершился. Обнови источник.')
                return self.frame[0]

    def _start(self, target):
        try:
            import sys
            from pathlib import Path
            if not getattr(sys, 'frozen', False):
                sys.path.insert(0, str(Path(__file__).parent.parent/'capture_deps'))
            from windows_capture import WindowsCapture
        except ImportError as exc:
            raise RuntimeError('В этой сборке отсутствует библиотека Windows Graphics Capture.') from exc

        try:
            if not self.border_checked:
                try:
                    self.borderless, status = request_borderless_access()
                    self.border_notice = ('; WGC без рамки: разрешено Windows' if self.borderless else
                        f'; Скрытие рамки запрошено; решение Windows: {status}')
                except (OSError, AttributeError, TimeoutError) as exc:
                    self.border_notice = '; Скрытие рамки запрошено; доступ Windows: ' + str(exc)
                self.border_checked = True
            # Windows honors IsBorderRequired=false only when policy permits it.
            # Set it even after denial so a later permission change can take effect.
            capture = WindowsCapture(cursor_capture=False,
                draw_border=False if sys.getwindowsversion().build >= 20348 else None, minimum_update_interval=150,
                **({'window_hwnd': target[1]} if target[0]=='window' else {'monitor_index': target[1]}))

            @capture.event
            def on_frame_arrived(frame, capture_control):
                now = monotonic()
                if self.frame is not None and now - self.frame[1] < 0.14:
                    return
                # The buffer belongs to the callback, so copy it before returning.
                pixels = frame.frame_buffer
                image = Image.frombytes('RGB', (frame.width, frame.height),
                                        pixels.tobytes(), 'raw', 'BGRX')
                with self.changed:
                    self.frame = image, now
                    self.changed.notify_all()

            @capture.event
            def on_closed():
                with self.changed:
                    self.closed = True
                    self.changed.notify_all()

            self.control = capture.start_free_threaded()
            self.hwnd = target
        except Exception as exc:
            raise RuntimeError(f'WGC не открыл окно: {exc}. Проверь захват из обычного сеанса Windows и доступность окна.') from exc
