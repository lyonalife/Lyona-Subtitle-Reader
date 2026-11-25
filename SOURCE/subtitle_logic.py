"""Filtering and repeat suppression for recognized game subtitles.

No network calls. This module can be tested independently of the GUI.
"""
from __future__ import annotations

from collections import deque
from difflib import SequenceMatcher
import re


CYRILLIC = re.compile(r"[\u0400-\u052F]")
SPACE = re.compile(r"\s+")
WORDS = re.compile(r"[\w\u0400-\u052F]+", re.UNICODE)


def tidy(raw: str) -> str:
    """Remove OCR garbage without removing Russian dialogue punctuation."""
    lines = []
    for line in raw.replace("\r", "").split("\n"):
        line = SPACE.sub(" ", line).strip(" _|~\\/•·*[]{}«» ")
        if len(line) >= 2 and CYRILLIC.search(line):
            lines.append(line)
    return "\n".join(lines[:4])[:360]


def key(text: str) -> str:
    text = text.casefold().replace("ё", "е")
    return re.sub(r"[^\wа-я]+", "", text, flags=re.UNICODE)


def similar(a: str, b: str) -> float:
    a, b = key(a), key(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


class SubtitleTracker:
    """Confirm a caption, then speak only genuinely appended words."""

    def __init__(self, frames: int = 2, repeat_seconds: float = 12.0,
                 settle_seconds: float = 0.0):
        self.frames = max(1, frames)
        self.repeat_seconds = repeat_seconds
        self.settle_seconds = max(0.0, settle_seconds)
        self.candidate = ""
        self.count = 0
        self.candidate_changed_at = 0.0
        self.pending_started_at = 0.0
        self.last_detection_at = 0.0
        self.last_accepted = ""
        self.history: deque[tuple[str, float]] = deque(maxlen=40)
        self.blank_count = 0

    @staticmethod
    def _new_words(previous: str, current: str) -> str:
        """Extract a word-aligned suffix when an OCR line grows in place."""
        old = [m.group().casefold().replace('ё', 'е') for m in WORDS.finditer(previous)]
        matches = list(WORDS.finditer(current))
        new = [m.group().casefold().replace('ё', 'е') for m in matches]
        if not old or len(new) <= len(old):
            return ""
        exact = new[:len(old)] == old
        corrected_last = (len(old) > 1 and new[:len(old)-1] == old[:-1]
                          and len(old[-1]) >= 3 and new[len(old)-1].startswith(old[-1]))
        corrected_ocr = (len(old) >= 2 and
                         similar(' '.join(old), ' '.join(new[:len(old)])) >= 0.88)
        if exact or corrected_last or corrected_ocr:
            return current[matches[len(old)].start():].strip()
        return ""

    def observe(self, raw: str, now: float) -> str | None:
        text = tidy(raw)
        if not text:
            self.blank_count += 1
            if self.blank_count >= 2:
                self.candidate = ""
                self.count = 0
                self.pending_started_at = 0.0
                # A caption can legitimately return later. The recent-history
                # cooldown still prevents an immediate re-read after a blink.
                self.last_accepted = ""
            return None
        self.blank_count = 0

        if text == self.candidate:
            self.count += 1
        else:
            old_words = WORDS.findall(self.candidate)
            new_words = WORDS.findall(text)
            letter_correction = (self.candidate and len(old_words) == len(new_words)
                                 and abs(len(key(text))-len(key(self.candidate)))
                                     <= max(3, len(key(self.candidate)) // 10)
                                 and similar(self.candidate, text) >= 0.88)
            related = (self.candidate and
                       (similar(self.candidate, text) >= 0.65
                        or key(text).startswith(key(self.candidate))
                        or key(self.candidate).startswith(key(text))))
            if not related:
                self.pending_started_at = now
            self.candidate = text
            if letter_correction:
                self.count += 1
            else:
                self.count = 1
                self.candidate_changed_at = now

        complete = (text.rstrip().endswith(('.', '!', '?', '…'))
                    and len(WORDS.findall(text)) >= 2)
        quiet_time = 0.0 if complete else self.settle_seconds
        if (self.count < self.frames or
                now - self.candidate_changed_at < quiet_time):
            return None

        stable = self.candidate
        spoken = self._new_words(self.last_accepted, stable)
        if not spoken and self.last_accepted:
            old_words = WORDS.findall(self.last_accepted)
            new_words = WORDS.findall(stable)
            if (similar(stable, self.last_accepted) >= 0.86
                    and len(new_words) <= len(old_words)):
                # Corrected letters and a briefly shortened OCR result are
                # not new dialogue. Keep the better full line for later growth.
                if len(new_words) >= len(old_words):
                    self.last_accepted = stable
                return None
            previous = self.last_accepted.splitlines()
            current = stable.splitlines()
            new_lines = [line for line in current
                         if not any(similar(line, p) >= 0.87 for p in previous)]
            spoken = " ".join(new_lines).strip()
        elif not self.last_accepted:
            spoken = stable.replace('\n', ' ').strip()

        if len(key(spoken)) < 2:
            return None
        while self.history and now - self.history[0][1] > self.repeat_seconds:
            self.history.popleft()
        if any(similar(spoken, past) >= 0.90 for past, _ in self.history):
            return None
        self.last_detection_at = self.pending_started_at or now
        self.last_accepted = stable
        self.history.append((spoken, now))
        return spoken
