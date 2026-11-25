"""Enumerate Windows video sources in the same Media Foundation order as OpenCV MSMF.

MFEnumDeviceSources reports real friendly names and stable symbolic links. OpenCV's
CAP_MSMF integer camera input enumerates the same source list and takes its index.
Neither API reliably distinguishes a USB capture card from a webcam.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import sys
import uuid


@dataclass(frozen=True)
class VideoDevice:
    name: str
    symbolic_link: str
    index: int
    backend: str = 'msmf'
    fallback_index: int | None = None


class GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
                ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]

    def __init__(self, value: str):
        super().__init__()
        raw = uuid.UUID(value).bytes_le
        ctypes.memmove(ctypes.byref(self), raw, 16)


SOURCE_TYPE = GUID('c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3')
VIDEO_TYPE = GUID('8ac3587a-4ae7-42d8-99e0-0a6013eef90f')
FRIENDLY_NAME = GUID('60d0e559-52f8-4fa2-bbce-acdb34a8ec01')
SYMBOLIC_LINK = GUID('58f0aad8-22bf-4f8a-bb3d-d2c4978c6e2f')


def _check(hr: int, action: str):
    if hr < 0:
        raise OSError(f'{action}: Windows Media Foundation 0x{hr & 0xffffffff:08X}')


def _method(obj: ctypes.c_void_p, index: int, *args):
    address = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[index]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *args)(address)


def _release(obj: ctypes.c_void_p):
    if obj:
        _method(obj, 2)(obj)


def _get_string(obj: ctypes.c_void_p, key: GUID, ole32) -> str:
    value = ctypes.c_void_p()
    length = ctypes.c_uint32()
    getter = _method(obj, 13, ctypes.POINTER(GUID),
                     ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32))
    _check(getter(obj, ctypes.byref(key), ctypes.byref(value), ctypes.byref(length)),
           'Имя видеоустройства')
    try:
        return ctypes.wstring_at(value, length.value)
    finally:
        ole32.CoTaskMemFree(value)


def _list_media_foundation_devices() -> list[VideoDevice]:
    if sys.platform != 'win32':
        return []
    ole32 = ctypes.WinDLL('ole32')
    mfplat = ctypes.WinDLL('mfplat')
    mf = ctypes.WinDLL('mf')
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    mfplat.MFStartup.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    mfplat.MFStartup.restype = ctypes.c_long
    mfplat.MFCreateAttributes.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
    mfplat.MFCreateAttributes.restype = ctypes.c_long
    mf.MFEnumDeviceSources.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                        ctypes.POINTER(ctypes.c_uint32)]
    mf.MFEnumDeviceSources.restype = ctypes.c_long
    hr = ole32.CoInitializeEx(None, 0)
    initialized_com = hr in (0, 1)
    if hr < 0 and (hr & 0xffffffff) != 0x80010106:  # already initialized as STA
        _check(hr, 'Инициализация COM')
    attrs = ctypes.c_void_p()
    array = ctypes.c_void_p()
    count = ctypes.c_uint32()
    started = False
    try:
        _check(mfplat.MFStartup(0x20070, 0), 'Запуск Media Foundation')
        started = True
        _check(mfplat.MFCreateAttributes(ctypes.byref(attrs), 1), 'Атрибуты видеоустройств')
        setter = _method(attrs, 24, ctypes.POINTER(GUID), ctypes.POINTER(GUID))
        _check(setter(attrs, ctypes.byref(SOURCE_TYPE), ctypes.byref(VIDEO_TYPE)),
               'Выбор видеоисточников')
        _check(mf.MFEnumDeviceSources(attrs, ctypes.byref(array), ctypes.byref(count)),
               'Поиск видеоустройств')
        result = []
        sources = ctypes.cast(array, ctypes.POINTER(ctypes.c_void_p)) if array else ()
        for index in range(count.value):
            device = ctypes.c_void_p(sources[index])
            try:
                name = _get_string(device, FRIENDLY_NAME, ole32).strip()
                link = _get_string(device, SYMBOLIC_LINK, ole32)
                if name and link:
                    result.append(VideoDevice(name, link, index))
            finally:
                _release(device)
        return result
    finally:
        if array:
            ole32.CoTaskMemFree(array)
        _release(attrs)
        if started:
            mfplat.MFShutdown()
        if initialized_com:
            ole32.CoUninitialize()


# DirectShow supplies additional virtual cameras that Media Foundation omits.
# OpenCV CAP_DSHOW uses this same category and IEnumMoniker order.
VIDEO_INPUT_CATEGORY = GUID('860bb310-5d01-11d0-bd3b-00a0c911ce86')
SYSTEM_DEVICE_ENUM = GUID('62be5d10-60eb-11d0-bd3b-00a0c911ce86')
CREATE_DEVICE_ENUM_IID = GUID('29840822-5b84-11d0-bd3b-00a0c911ce86')
PROPERTY_BAG_IID = GUID('55272a00-42cb-11ce-8135-00aa004bb851')


def _property_string(bag: ctypes.c_void_p, name: str) -> str:
    # VARIANT is 24 bytes on 64-bit Windows, with the BSTR pointer at offset 8.
    variant = ctypes.create_string_buffer(24)
    reader = _method(bag, 3, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p)
    hr = reader(bag, name, ctypes.byref(variant), None)
    if hr < 0:
        return ''
    try:
        value_type = ctypes.c_uint16.from_buffer(variant).value
        if value_type != 8:  # VT_BSTR
            return ''
        address = ctypes.c_void_p.from_buffer(variant, 8).value
        return ctypes.wstring_at(address) if address else ''
    finally:
        ctypes.WinDLL('oleaut32').VariantClear(ctypes.byref(variant))


def _list_directshow_devices() -> list[VideoDevice]:
    if sys.platform != 'win32':
        return []
    ole32 = ctypes.WinDLL('ole32')
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                        ctypes.c_uint32, ctypes.POINTER(GUID),
                                        ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    hr = ole32.CoInitializeEx(None, 0)
    initialized_com = hr in (0, 1)
    if hr < 0 and (hr & 0xffffffff) != 0x80010106:
        _check(hr, 'Инициализация DirectShow')
    enum_object = ctypes.c_void_p()
    monikers = ctypes.c_void_p()
    try:
        _check(ole32.CoCreateInstance(ctypes.byref(SYSTEM_DEVICE_ENUM), None, 1,
                                      ctypes.byref(CREATE_DEVICE_ENUM_IID),
                                      ctypes.byref(enum_object)), 'Поиск камер DirectShow')
        enumerator = _method(enum_object, 3, ctypes.POINTER(GUID),
                             ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32)
        result_code = enumerator(enum_object, ctypes.byref(VIDEO_INPUT_CATEGORY),
                                 ctypes.byref(monikers), 0)
        if result_code == 1:  # S_FALSE: no video sources
            return []
        _check(result_code, 'Список камер DirectShow')
        result = []
        while True:
            moniker = ctypes.c_void_p()
            fetched = ctypes.c_uint32()
            next_one = _method(monikers, 3, ctypes.c_uint32,
                               ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32))
            if next_one(monikers, 1, ctypes.byref(moniker), ctypes.byref(fetched)) != 0:
                break
            bag = ctypes.c_void_p()
            try:
                bind = _method(moniker, 9, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
                _check(bind(moniker, None, None, ctypes.byref(PROPERTY_BAG_IID),
                            ctypes.byref(bag)), 'Свойства камеры DirectShow')
                name = _property_string(bag, 'FriendlyName').strip()
                link = _property_string(bag, 'DevicePath')
                if not link:
                    display = ctypes.c_void_p()
                    get_display = _method(moniker, 20, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_void_p))
                    if get_display(moniker, None, None, ctypes.byref(display)) >= 0:
                        try:
                            link = ctypes.wstring_at(display)
                        finally:
                            ole32.CoTaskMemFree(display)
                if name and link:
                    result.append(VideoDevice(name, link, len(result), 'dshow'))
            finally:
                _release(bag)
                _release(moniker)
        return result
    finally:
        _release(monikers)
        _release(enum_object)
        if initialized_com:
            ole32.CoUninitialize()


def _physical_id(link: str) -> str:
    value = link.casefold().removeprefix('@device_pnp_')
    if value.startswith('@device_sw_'):
        return value
    return value.split('#{', 1)[0] if '#{' in value else value


def list_video_devices() -> list[VideoDevice]:
    """Combine MF and DirectShow without claiming a device is a card or camera."""
    mf_error = ds_error = None
    try:
        media = _list_media_foundation_devices()
    except OSError as exc:
        mf_error, media = exc, []
    try:
        directshow = _list_directshow_devices()
    except OSError as exc:
        ds_error, directshow = exc, []
    if mf_error and ds_error:
        raise RuntimeError(f'Не удалось получить список видеоустройств: {mf_error}; {ds_error}')
    by_id = {_physical_id(device.symbolic_link): device for device in directshow}
    result = []
    seen = set()
    for device in media:
        key = _physical_id(device.symbolic_link)
        match = by_id.get(key)
        result.append(VideoDevice(device.name, device.symbolic_link, device.index,
                                  'msmf', match.index if match else None))
        seen.add(key)
    result.extend(device for device in directshow if _physical_id(device.symbolic_link) not in seen)
    return result
