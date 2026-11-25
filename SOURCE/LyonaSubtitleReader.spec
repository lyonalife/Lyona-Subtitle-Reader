# -*- mode: python ; coding: utf-8 -*-
"""One-directory portable Windows application; model weights remain opt-in."""
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
site = Path(sys.prefix) / 'Lib' / 'site-packages'
datas = [
    (str(root / 'cpu_backend/qwentts_cpp_cpu/lib'), 'qwentts_cpp_cpu/lib'),
    (str(root / 'cpu_backend/realtimetts_qwen_native_cpu.libs'), 'realtimetts_qwen_native_cpu.libs'),
    (str(root / 'windows_ocr_worker.ps1'), '.'),
    (str(root / 'pronunciation_dictionary.json'), '.'),
    (str(root / 'voice_refs'), 'voice_refs'),
]
binaries = []
hiddenimports = []
for package in ('qwentts_cpp', 'RealtimeTTS', 'stream2sentence'):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
native_cuda = root / 'native_cuda'
native_names = ('qwen.dll', 'ggml.dll', 'ggml-base.dll', 'ggml-cpu.dll', 'ggml-cuda.dll')
assert all((native_cuda / name).is_file() for name in native_names), 'Build the ABI-5 multiarchitecture CUDA backend first'
def original_cuda(item):
    return item[1].replace('\\', '/').rstrip('/') == 'qwentts_cpp/lib' and Path(item[0]).suffix.lower() == '.dll'
datas = [item for item in datas if not original_cuda(item)]
binaries = [item for item in binaries if not original_cuda(item)]
binaries += [(str(native_cuda / name), 'qwentts_cpp/lib') for name in native_names]
datas.append((str(native_cuda / 'build-info.json'), 'qwentts_cpp/lib'))
for name in ('cuda_runtime', 'cublas', 'cuda_nvrtc'):
    folder = site / 'nvidia' / name / 'bin'
    if folder.is_dir():
        datas.append((str(folder), f'nvidia/{name}/bin'))

a = Analysis(
    [str(root / 'subtitle_reader.py')],
    pathex=[str(root), str(root / 'cpu_backend'), str(root / 'capture_deps')],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / 'runtime_hook.py')],
    excludes=['torch', 'tensorflow', 'google_tts', 'edge_tts'],
    noarchive=False,
)
# Dependency analysis may add a second copy at the bundle root. The native
# loader uses qwentts_cpp/lib explicitly; keep one unambiguous CUDA backend.
a.binaries = [entry for entry in a.binaries if entry[0].replace('\\', '/') != 'ggml-cuda.dll']
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name='LyonaSubtitleReader_Streamer', debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, console=False)
worker = EXE(pyz, a.scripts, [], exclude_binaries=True,
             name='LyonaWorker', debug=False, bootloader_ignore_signals=False,
             strip=False, upx=False, console=True)
coll = COLLECT(exe, worker, a.binaries, a.datas, strip=False, upx=False,
               name='LyonaSubtitleReader_Streamer')
