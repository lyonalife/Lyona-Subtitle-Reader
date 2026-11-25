# Сведения о компонентах — PUBLIC_RELEASE

Список составлен по PyInstaller Analysis-00.toc этой сборки. Тексты лицензий скопированы из установленного пакета без изменения.

| Компонент | Версия | Лицензия из метаданных |
|---|---:|---|
| anyio | 4.16.0 | MIT |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.1 | BSD-3-Clause |
| cloudpickle | 3.1.3 | BSD-3-Clause |
| colorama | 0.4.6 | см. текст лицензии |
| defusedxml | 0.7.1 | PSFL |
| emoji | 2.16.0 | см. текст лицензии |
| filelock | 4.3.0 | MIT |
| fsspec | 2026.9.0 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| hf-xet | 1.8.0 | Apache-2.0 |
| httpcore2 | 2.16.2 | BSD-3-Clause |
| httpx2 | 2.16.2 | BSD-3-Clause |
| huggingface_hub | 2.4.0 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |
| joblib | 1.6.0 | BSD-3-Clause |
| llvmlite | 0.48.0 | см. текст лицензии |
| nltk | 3.10.4 | Apache License, Version 2.0 |
| numba | 0.66.0 | BSD |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| nvidia-cublas-cu12 | 12.9.2.10 | LicenseRef-NVIDIA-Proprietary |
| nvidia-cuda-nvrtc-cu12 | 12.9.86 | LicenseRef-NVIDIA-Proprietary |
| nvidia-cuda-runtime-cu12 | 12.9.79 | LicenseRef-NVIDIA-Proprietary |
| obsws-python | 1.8.1 | GPL-3.0-only |
| opencv-python-headless | 4.14.0.94 | Apache 2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pillow | 12.3.0 | MIT-CMU |
| PyAudio | 0.2.14 | MIT |
| pycparser | 3.12 | BSD-3-Clause |
| pydub | 0.25.1 | MIT |
| pygame | 2.6.1 | LGPL |
| pyinstaller | 6.22.3 | GPLv2-or-later with a special exception which allows to use PyInstaller to build and distribute non-free programs (including commercial ones) |
| pytesseract | 0.3.13 | Apache License 2.0 |
| PyYAML | 6.0.4 | MIT |
| realtimetts | 0.8.10 | MIT |
| realtimetts-qwen-native | 0.2.0 | MIT |
| regex | 2026.11.19 | Apache-2.0 AND CNRI-Python |
| resampy | 0.4.3 | ISC |
| setuptools | 65.5.0 | см. текст лицензии |
| soundfile | 0.14.0 | BSD 3-Clause License |
| stream2sentence | 1.1.3 | Apache-2.0 |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| truststore | 0.10.5 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| websocket-client | 1.9.2 | Apache-2.0 |

Модели GGUF не включены в ZIP. При согласии пользователя они загружаются из Serveurperso/Qwen3-TTS-GGUF (Apache-2.0); исходная Qwen3-TTS Base и VoiceDesign также отмечены как Apache-2.0.
Два включённых WAV созданы локально VoiceDesign из текстовых описаний. Их происхождение и SHA-256 — в VOICE_PROVENANCE_RU.md.
Tesseract OCR как EXE не включён. Windows OCR предоставляется Windows.
CUDA DLL входят в отдельные NVIDIA-пакеты; их условия находятся в `packages/nvidia-*/.../License.txt`.

`obsws-python` имеет GPL-3.0-only. Исходный код Streamer включён в архив для соблюдения условий распространения.
