# -*- coding: utf-8 -*-
"""Small, streaming WSOLA tempo adjustment for 16-bit mono PCM.

Works on short live chunks, preserves pitch far better than changing sample rate.
Only imported inside the optional faster Qwen playback path.
"""
from __future__ import annotations

import numpy as np


class LiveTempo:
    def __init__(self, sample_rate: int, factor: float):
        if not (8000 <= int(sample_rate) <= 192000):
            raise ValueError('Unsupported sample rate')
        if not (1.0 < float(factor) <= 1.35):
            raise ValueError('Tempo must be between 1.0 and 1.35')
        self.factor = float(factor)
        self.rate = int(sample_rate)
        self.hop = max(128, int(round(sample_rate * .01067)))
        self.length = self.hop * 4
        self.overlap = self.length - self.hop
        self.search = int(round(sample_rate * .008))
        self.step = max(2, int(round(sample_rate * .00033)))
        self.window = np.hanning(self.length).astype(np.float32)
        self.source = np.empty(0, dtype=np.float32)
        self.accum = np.zeros(self.length, dtype=np.float32)
        self.weights = np.zeros(self.length, dtype=np.float32)
        self.frame_idx = 0
        self.input_samples = 0
        self.output_samples = 0

    def feed(self, pcm_bytes: bytes) -> bytes:
        if len(pcm_bytes) % 2:
            raise ValueError('PCM chunk length must be even')
        if pcm_bytes:
            new = np.frombuffer(pcm_bytes, dtype='<i2').astype(np.float32) / 32768.0
            self.source = np.concatenate((self.source, new))
            self.input_samples += len(new)
        return self._render(final=False)

    def finish(self) -> bytes:
        target = max(0, round(self.input_samples / self.factor))
        # Padding lets the final overlapping windows fade out instead of truncating a syllable.
        self.source = np.concatenate((self.source, np.zeros(self.length + self.search * 2, dtype=np.float32)))
        return self._render(final=True, target=target)

    @staticmethod
    def _pcm(arr) -> bytes:
        return (np.clip(arr, -1, 1) * 32767.0).astype('<i2').tobytes()

    def _choose_offset(self, expected: int) -> int:
        lo = max(0, expected - self.search)
        hi = min(len(self.source) - self.length, expected + self.search)
        if hi <= lo:
            return lo
        # Align a candidate voice period with the overlapping audio already assembled.
        target = self.accum[self.hop:].copy()
        w = self.weights[self.hop:]
        good = w > 0.05
        if not np.any(good):
            return min(hi, max(lo, expected))
        target[good] /= w[good]
        target[~good] = 0
        win = np.hanning(self.overlap).astype(np.float32)
        target *= win
        tgt_energy = np.sqrt(float(np.dot(target, target)) + 1e-9)
        best = min(hi, max(lo, expected))
        best_score = -1e9
        candidates = list(range(lo, hi + 1, self.step))
        if best not in candidates:
            candidates.append(best)
        for idx in candidates:
            part = self.source[idx:idx + self.overlap] * win
            dot = float(np.dot(part, target))
            norm = np.sqrt(float(np.dot(part, part)) + 1e-9) * tgt_energy
            normalized = dot / norm
            # Avoid large jumps through noise/silence when equally good matches exist.
            penalty = 0.025 * abs(idx - expected) / max(1, self.search)
            score = normalized - penalty
            if score > best_score:
                best_score, best = score, idx
        return best

    def _render(self, final: bool, target: int | None = None) -> bytes:
        produced = []
        while True:
            if final and self.output_samples >= target:
                break
            if self.frame_idx == 0:
                if len(self.source) < self.length:
                    break
                self.accum += self.source[:self.length] * self.window
                self.weights += self.window
            else:
                expected = round(self.frame_idx * self.hop * self.factor)
                if len(self.source) < expected + self.length + (0 if final else self.search):
                    break
                index = self._choose_offset(expected)
                self.accum[:-self.hop] = self.accum[self.hop:]
                self.accum[-self.hop:] = 0
                self.weights[:-self.hop] = self.weights[self.hop:]
                self.weights[-self.hop:] = 0
                frame = self.source[index:index+self.length]
                self.accum += frame * self.window
                self.weights += self.window
            result = np.divide(self.accum[:self.hop], self.weights[:self.hop],
                               out=np.zeros(self.hop, dtype=np.float32),
                               where=self.weights[:self.hop] > 1e-5)
            if final:
                result = result[:max(0, min(len(result), target-self.output_samples))]
            produced.append(self._pcm(result))
            self.output_samples += len(result)
            self.frame_idx += 1
        return b''.join(produced)
