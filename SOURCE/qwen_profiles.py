"""Two original ICL profiles with text-generated VoiceDesign references."""
from __future__ import annotations

import hashlib
from public_paths import APP_DIR, MODEL_DIR, VOICE_DIR

WARM = '1.7-designed-warm-icl'
LOW = '1.7-designed-medium-low-icl'
PROFILES = {
    WARM: ('Qwen 1.7B — Мягкий и тёплый (ICL)', 'designed-warm',
           'Привет. Я рада тебя видеть. Давай немного отдохнём, а потом вместе решим, куда идти дальше. Всё будет хорошо.'),
    LOW: ('Qwen 1.7B — Умеренно низкий (ICL)', 'designed-medium-low',
          'Привет. Я уже здесь. Давай спокойно разберёмся, что случилось, а потом решим, куда идти дальше. Я помогу тебе.'),
}
REFERENCE_SHA256 = {
    WARM: '0721fbdebbe799e907c81a9b88fa3edd7ce238a6761087394fac79ede4607bd7',
    LOW: 'c4149a8a98cdbcbe36d7e0ace5ad684dcf281a2dc6d28917db7f841243dcaee0',
}


def reference_ready(model: str) -> bool:
    if model not in PROFILES:
        return False
    sample = APP_DIR / 'voice_refs' / f'{PROFILES[model][1]}.wav'
    return sample.is_file() and hashlib.sha256(sample.read_bytes()).hexdigest() == REFERENCE_SHA256[model]


def voice_profile(_folder: str, model: str = LOW) -> dict:
    if model not in PROFILES:
        raise ValueError('Неизвестный профиль Qwen: ' + model)
    label, name, reference_text = PROFILES[model]
    sample = APP_DIR / 'voice_refs' / f'{name}.wav'
    if not reference_ready(model):
        raise ValueError('Референс голоса отсутствует или повреждён: ' + str(sample))
    talker = MODEL_DIR / 'qwen-talker-1.7b-base-Q8_0.gguf'
    codec = MODEL_DIR / 'qwen-tokenizer-12hz-Q8_0.gguf'
    for path in (sample, talker, codec):
        if not path.is_file():
            raise FileNotFoundError('Не найден файл для Qwen: ' + str(path))
    return {
        'sample': sample,
        'name': name,
        'ref_text': reference_text,
        'engine': {
            'model_id': 'Qwen/Qwen3-TTS-12Hz-1.7B-Base',
            'quant': 'Q8_0',
            'talker_path': talker,
            'codec_path': codec,
            'local_files_only': True,
            'voice_cache_dir': VOICE_DIR / 'cache',
            'seed': 42,
            'temperature': 0.72,
            'top_k': 35,
        },
    }
