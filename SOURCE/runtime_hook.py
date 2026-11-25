"""Locate only the CUDA runtime libraries bundled for Qwen Native."""
import os
from pathlib import Path
import sys

if getattr(sys, 'frozen', False):
    base = Path(sys._MEIPASS)
    paths = [base / 'nvidia' / name / 'bin'
             for name in ('cuda_runtime', 'cublas', 'cuda_nvrtc')]
    paths += [base / 'realtimetts_qwen_native.libs', base / 'qwentts_cpp/lib', base]
    _lyona_dll_handles = [os.add_dll_directory(str(path)) for path in paths if path.is_dir()]
    present = [str(path) for path in paths if path.is_dir()]
    if present:
        os.environ['QWENTTS_CPP_CUDA_LIBRARY_PATH'] = os.pathsep.join(present)
