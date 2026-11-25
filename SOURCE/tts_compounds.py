"""TTS-only spelling hints for known Russian compounds.

Keep sentence dashes and the OCR result untouched. OCR may add spaces around a
hyphen; this module only changes words whose two halves form a known compound.
"""
import re


# An en dash often separates clauses, so it is deliberately excluded here.
HYPHEN = r'[-‐‑‒]'
PREFIXES = 'что|кто|где|когда|куда|откуда|зачем|почему|как|какой|чей|сколько|кое'
SUFFIXES = 'то|нибудь|либо'
SUFFIXED = re.compile(
    rf'(?<!\w)({PREFIXES})\s*{HYPHEN}\s*({SUFFIXES})(?!\w)', re.IGNORECASE
)
SPECIFIC = re.compile(
    rf'(?<!\w)(по)\s*{HYPHEN}\s*'
    r'(моему|твоему|нашему|вашему|своему|прежнему)(?!\w)', re.IGNORECASE
)
TAKI = re.compile(rf'(?<!\w)(всё|все)\s*{HYPHEN}\s*(таки)(?!\w)', re.IGNORECASE)
KOE = re.compile(
    rf'(?<!\w)(кое)\s*{HYPHEN}\s*(что|кто|где|как)(?!\w)', re.IGNORECASE
)
PREPOSITIONS = re.compile(
    rf'(?<!\w)(из)\s*{HYPHEN}\s*(за|под)(?!\w)', re.IGNORECASE
)


def fix_compound_hyphens(text: str) -> str:
    for pattern in (SUFFIXED, SPECIFIC, TAKI, KOE, PREPOSITIONS):
        text = pattern.sub(lambda match: match.group(1) + match.group(2), text)
    return text
