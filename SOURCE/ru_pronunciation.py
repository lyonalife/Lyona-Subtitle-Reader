# -*- coding: utf-8 -*-
"""Conservative Russian subtitle text preparation for existing Qwen3 TTS.

No external models, no audio processing and no edit to the reference WAV.
The original Qwen engine may ignore marked stresses; this is not a phoneme model.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from tts_compounds import fix_compound_hyphens
from public_paths import APP_DIR, DATA_DIR

DICTIONARY_PATH = DATA_DIR / 'pronunciation_dictionary.json'
if not DICTIONARY_PATH.exists():
    DICTIONARY_PATH.write_bytes((APP_DIR / 'pronunciation_dictionary.json').read_bytes())
# Keep original punctuation semantics; only fix obvious OCR spacing mistakes.
BAD_PUNCT_SPACING = re.compile(r'\s+([,.!?;:])')
MISSING_SPACE = re.compile(r'([,;:!?])(?=[А-Яа-яЁё])')
EXCESSIVE_QUESTION = re.compile(r'\?{3,}')
EXCESSIVE_EXCLAMATION = re.compile(r'!{3,}')
SPACES = re.compile(r'\s+')


def _case_like(match_text: str, replacement: str) -> str:
    if match_text.isupper():
        return replacement.upper()
    if match_text[:1].isupper() and match_text[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _rules_from_json(path: Path) -> list[tuple[re.Pattern[str], str]]:
    if not path.exists():
        raise ValueError('Нет pronunciation_dictionary.json рядом с программой')
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except json.JSONDecodeError as exc:
        raise ValueError('Ошибка JSON в словаре, строка ' + str(exc.lineno)) from exc
    if not isinstance(data, dict):
        raise ValueError('В словаре нужен объект JSON')
    rules = []
    for group in ('фразы', 'слова'):
        items = data.get(group, {})
        if not isinstance(items, dict):
            raise ValueError('Раздел «' + group + '» должен содержать пары слов')
        for before, after in items.items():
            if not isinstance(before, str) or not isinstance(after, str):
                raise ValueError('Обе стороны словарной записи должны быть текстом')
            if not before.strip() or not after.strip() or any(c in after for c in '\n\r\0'):
                raise ValueError('Пустое или недопустимое значение в словаре')
            before = before.strip()
            after = after.strip()
            if group == 'слова' and (' ' in before or '\t' in before):
                raise ValueError('Запись с пробелом помещай в раздел «фразы»: ' + before)
            pattern = re.compile(r'(?<!\w)' + re.escape(before) + r'(?!\w)', re.IGNORECASE | re.UNICODE)
            rules.append((pattern, after))
    # Longer forms first, so «все еще» is transformed before the individual words.
    return sorted(rules, key=lambda item: -len(item[0].pattern))


def prepare_text(text: str, dictionary_path: Path | None = None) -> str:
    """Make conservative textual rewrites. The caller may disable this entirely.

    The default unmodified model still determines stress; we cannot guarantee it
    will use an accent mark in a user-defined replacement.
    """
    if not isinstance(text, str):
        raise ValueError('Нужен текст для озвучки')
    result = SPACES.sub(' ', text).strip()
    result = BAD_PUNCT_SPACING.sub(r'\1', result)
    result = MISSING_SPACE.sub(r'\1 ', result)
    result = EXCESSIVE_QUESTION.sub('?', result)
    result = EXCESSIVE_EXCLAMATION.sub('!', result)
    for pattern, replacement in _rules_from_json(dictionary_path or DICTIONARY_PATH):
        result = pattern.sub(lambda match: _case_like(match.group(0), replacement), result)
    return fix_compound_hyphens(result)
