"""Game subtitle OCR: color-aware white-letter extraction + conservative fallback.

Uses only Pillow and pytesseract, already required by Lyona's OBS reader.
Never saves frames, connects to the network, or touches the capture device.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
import re
import time

from PIL import Image, ImageChops, ImageOps
import pytesseract

from subtitle_logic import tidy

CYR = re.compile(r'[А-Яа-яЁё]')
LAT = re.compile(r'[A-Za-z]')
PROMPT_WORDS = {'выбрать', 'назад', 'пропустить', 'выйти', 'подтвердить'}
WHITE = 'Белые субтитры (игры)'
AUTO = 'Авто (с запасным OCR)'
NORMAL = 'Обычный OCR'
MODES = (WHITE, AUTO, NORMAL)


@dataclass
class OCRResult:
    text: str
    confidence: float
    method: str
    elapsed: float
    preview: Image.Image


def white_mask(image: Image.Image) -> Image.Image:
    """Binary image: retain near-neutral bright pixels, not colorful game scenery.

    The broad user-selected region is preserved. This is not a narrower crop:
    the text is isolated by color inside the same entire region.
    """
    image = image.convert('RGB')
    r, g, b = image.split()
    least = ImageChops.darker(ImageChops.darker(r, g), b)
    most = ImageChops.lighter(ImageChops.lighter(r, g), b)
    chroma = ImageChops.subtract(most, least)
    bright = least.point(lambda v: 255 if v >= 165 else 0)
    near_white = chroma.point(lambda v: 255 if v <= 35 else 0)
    mask = ImageChops.multiply(bright, near_white)
    return mask.resize((mask.width * 2, mask.height * 2), Image.Resampling.BICUBIC)


def normal_image(image: Image.Image) -> Image.Image:
    gray = ImageOps.autocontrast(ImageOps.grayscale(image))
    return gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.LANCZOS)


def _read_image(image: Image.Image, config: str, language: str) -> tuple[str, float]:
    data = pytesseract.image_to_data(
        image, lang=language, output_type=pytesseract.Output.DICT,
        config=(config + ' --psm 6').strip(), timeout=4,
    )
    groups = defaultdict(list)
    for i, val in enumerate(data.get('text', [])):
        word = str(val).strip()
        if not word or not (CYR.search(word) or LAT.search(word)):
            continue
        try:
            confidence = float(data['conf'][i])
        except (ValueError, TypeError, KeyError):
            continue
        if confidence < 37:
            continue
        c = len(CYR.findall(word))
        a = len(LAT.findall(word))
        if c + a < max(1, len(word) // 3):
            continue
        line = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
        groups[line].append((word, confidence))

    result_lines = []
    sum_conf = 0.0
    count = 0
    for words in groups.values():
        line_text = ' '.join(w for w, _ in words)
        c, a = len(CYR.findall(line_text)), len(LAT.findall(line_text))
        if c < 2 or c / max(1, c + a) < 0.65:
            continue
        tokens = {w.casefold().strip('.,:()[]!?') for w, _ in words}
        if tokens and (tokens <= PROMPT_WORDS or len(tokens & PROMPT_WORDS) >= 2):
            continue
        avg = sum(cf for _, cf in words) / len(words)
        if avg < 45:
            continue
        result_lines.append(line_text)
        sum_conf += sum(cf for _, cf in words)
        count += len(words)
    return tidy('\n'.join(result_lines)), (sum_conf / count if count else 0.0)


def recognize(image: Image.Image, config: str, language: str, mode: str = WHITE,
              white_preview: Image.Image | None = None) -> OCRResult:
    white_prepare = (lambda _image: white_preview) if white_preview is not None else white_mask
    if mode == NORMAL:
        variants = [(NORMAL, normal_image)]
    elif mode == AUTO:
        variants = [(WHITE, white_prepare), (NORMAL, normal_image)]
    else:
        variants = [(WHITE, white_prepare)]
    first = None
    for name, prepare in variants:
        started = time.perf_counter()
        preview = prepare(image)
        # An empty picture should not spawn OCR work every half-second.
        if name == WHITE and preview.getbbox() is None:
            result = OCRResult('', 0., name, time.perf_counter()-started, preview)
        else:
            text, conf = _read_image(preview, config, language)
            result = OCRResult(text, conf, name, time.perf_counter()-started, preview)
        if first is None:
            first = result
        if result.text:
            return result
    return first


def compare(image: Image.Image, config: str, language: str) -> tuple[OCRResult, OCRResult]:
    return (recognize(image, config, language, WHITE),
            recognize(image, config, language, NORMAL))
