"""Select hardware automatically and construct the separate CPU GGUF backend."""
import ctypes
import os
from pathlib import Path
import sys


def select_backend():
    if os.environ.get('LYONA_QWEN_CPU') == '1':
        return 'CPU', 'восстановление после ошибки CUDA'
    try:
        cuda = ctypes.WinDLL('nvcuda.dll')
        if cuda.cuInit(0) != 0:
            return 'CPU', 'CUDA не инициализировалась'
        count = ctypes.c_int()
        if cuda.cuDeviceGetCount(ctypes.byref(count)) != 0:
            return 'CPU', 'не удалось перечислить CUDA GPU'
        for ordinal in range(count.value):
            device = ctypes.c_int()
            major, minor = ctypes.c_int(), ctypes.c_int()
            if cuda.cuDeviceGet(ctypes.byref(device), ordinal) != 0:
                continue
            if cuda.cuDeviceComputeCapability(ctypes.byref(major), ctypes.byref(minor), device) == 0:
                if (major.value, minor.value) >= (7, 5):
                    # The installed backend uses the default CUDA device.
                    if ordinal == 0:
                        return 'CUDA', f'compute capability {major.value}.{minor.value}'
        return 'CPU', 'совместимый GPU по умолчанию не обнаружен'
    except OSError:
        return 'CPU', 'драйвер CUDA недоступен'


def cpu_factory(**options):
    if not getattr(sys, 'frozen', False):
        sys.path.insert(0, str(Path(__file__).parent / 'cpu_backend'))
    import qwentts_cpp_cpu
    if qwentts_cpp_cpu.QT_ABI_VERSION != 5:
        raise RuntimeError('CPU backend требует ABI 5')
    # Avoid oversubscribing the CPU, leaving cores for OCR and the game.
    threads = min(8, max(1, (os.cpu_count() or 2) // 2))
    return qwentts_cpp_cpu.QwenTTS(
        options['talker_path'], options['codec_path'],
        use_fa=options['use_fa'], clamp_fp16=options['clamp_fp16'],
        max_batch=options['max_batch'], codec_chunk_sec=options['codec_chunk_sec'],
        cpu_threads=threads,
    )
