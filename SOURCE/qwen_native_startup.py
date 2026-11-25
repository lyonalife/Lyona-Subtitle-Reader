"""Native loader setup and diagnostics for the portable Streamer build."""
import ctypes
import os
from pathlib import Path
import platform
import sys
import json

_directories = []
_libraries = []


def library_root():
    return Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).parent / '.buildvenv/Lib/site-packages'


def configure():
    # The two fixed ICL references plus the unchanged 420-token ceiling fit
    # 1024 positions. F32 precision is retained; only unused capacity shrinks.
    os.environ.setdefault('LYONA_QWEN_CONTEXT', '1024')
    root = library_root()
    folders = [root, root / 'realtimetts_qwen_native.libs', root / 'qwentts_cpp/lib']
    folders += [root / 'nvidia' / name / 'bin' for name in ('cuda_runtime', 'cublas', 'cuda_nvrtc')]
    for folder in folders:
        if folder.is_dir() and hasattr(os, 'add_dll_directory'):
            _directories.append(os.add_dll_directory(str(folder)))
    os.environ['QWENTTS_CPP_CUDA_LIBRARY_PATH'] = os.pathsep.join(
        str(folder) for folder in folders[3:] if folder.is_dir())


def diagnose():
    import pefile
    configure()
    root = library_root()
    folders = [root, root / 'realtimetts_qwen_native.libs', root / 'qwentts_cpp/lib']
    folders += [root / 'nvidia' / name / 'bin' for name in ('cuda_runtime', 'cublas', 'cuda_nvrtc')]
    targets = ('VCRUNTIME140.dll', 'VCRUNTIME140_1.dll', 'MSVCP140.dll', 'VCOMP140.DLL',
               'nvcuda.dll', 'cudart64_12.dll', 'cublasLt64_12.dll', 'cublas64_12.dll',
               'ggml-base.dll', 'ggml-cpu.dll', 'ggml-cuda.dll', 'ggml.dll', 'qwen.dll')
    rows = [f'Windows {platform.win32_ver()[0]} {platform.win32_ver()[1]}; {platform.machine()}; Python {platform.architecture()[0]}']
    manifest = root / 'qwentts_cpp/lib/build-info.json'
    if manifest.is_file():
        try:
            info = json.loads(manifest.read_text(encoding='utf-8'))
            rows.append(f'Qwen native ABI {info.get("abi")}; architectures: {info.get("architectures")}; '
                        f'PTX: {info.get("ptx")}; source: {info.get("qwentts_commit")}')
        except (OSError, ValueError) as exc:
            rows.append('Cannot read native build information: ' + str(exc))
    failures = []
    checked = set()

    def check(name):
        key = name.lower()
        if key in checked or key.startswith(('api-ms-', 'ext-ms-')):
            return
        checked.add(key)
        file = next((folder / name for folder in folders if (folder / name).is_file()), None)
        if file:
            pe = pefile.PE(str(file), fast_load=True)
            try:
                pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
                for dependency in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', []):
                    check(dependency.dll.decode('ascii'))
            finally:
                pe.close()
        try:
            _libraries.append(ctypes.WinDLL(str(file) if file else name))
            rows.append(f'OK {name}' + (' [bundled]' if file else ' [Windows/driver]'))
        except OSError as exc:
            code = getattr(exc, 'winerror', None)
            rows.append(f'FAIL {name}: Windows error {code}')
            failures.append(name)

    for name in targets:
        check(name)
    try:
        cuda = ctypes.WinDLL('nvcuda.dll')
        version = ctypes.c_int()
        status = cuda.cuInit(0)
        cuda.cuDriverGetVersion(ctypes.byref(version))
        count = ctypes.c_int()
        count_status = cuda.cuDeviceGetCount(ctypes.byref(count)) if status == 0 else status
        rows.append(f'CUDA driver API {version.value}; cuInit={status}; GPU count={count.value}; count status={count_status}')
        if status == 0 and count_status == 0:
            for ordinal in range(count.value):
                device, major, minor = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
                name = ctypes.create_string_buffer(256)
                if cuda.cuDeviceGet(ctypes.byref(device), ordinal) == 0:
                    cuda.cuDeviceGetName(name, len(name), device)
                    code = cuda.cuDeviceComputeCapability(ctypes.byref(major), ctypes.byref(minor), device)
                    rows.append(f'GPU {ordinal}: {name.value.decode("utf-8", errors="replace")}; '
                                f'compute capability={major.value}.{minor.value}; query status={code}')
        if status or count_status or count.value == 0:
            failures.append('CUDA driver/GPU')
    except OSError:
        pass
    rows.append('Коды Windows: 126 — DLL или её зависимость отсутствует; 127 — отсутствует функция; 193 — неверная архитектура; 1114 — сбой инициализации.')
    rows.append('nvcuda.dll предоставляет драйвер NVIDIA. При её отсутствии проверьте NVIDIA GPU и драйвер на сервере.')
    return rows, failures
