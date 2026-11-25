"""Streaming Qwen level control for mono PCM16."""
from math import exp

MAX_GAIN_DB = 12
MAX_GAIN = 10 ** (MAX_GAIN_DB / 20)


class LoudnessNormalizer:
    def __init__(self, rate: int, gain: float, enabled: bool):
        self.rate = rate
        self.gain = gain
        self.enabled = enabled
        self.automatic_gain = None

    def process(self, pcm: bytes) -> bytes:
        import numpy as np

        if not pcm:
            return pcm
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        if self.enabled:
            # Measure only 10 ms windows above the speech gate. Keep pauses quiet.
            block = max(1, self.rate // 100)
            usable = len(audio) // block * block
            frames = audio[:usable].reshape(-1, block) if usable else audio[None, :]
            powers = np.mean(frames * frames, axis=1)
            active = powers[powers >= 10 ** (-48 / 10)]
            if active.size:
                target = float(np.clip(10 ** (-21 / 20) / np.sqrt(active.mean()), 0.5, MAX_GAIN))
                if self.automatic_gain is None:
                    self.automatic_gain = target
                previous = self.automatic_gain
                # Quicker reduction for louder speech, slower gain increase.
                tau = 0.12 if target < previous else 0.45
                mix = 1 - exp(-len(audio) / self.rate / tau)
                self.automatic_gain += mix * (target - previous)
                audio *= np.linspace(previous, self.automatic_gain, len(audio), dtype=np.float32)
            elif self.automatic_gain is not None:
                audio *= min(1.0, self.automatic_gain)

        audio *= self.gain
        # Soft knee above -3 dBFS, asymptotic ceiling at -1 dBFS.
        # This is a sample-peak limit; processing uses only the current block.
        knee, ceiling = 10 ** (-3 / 20), 10 ** (-1 / 20)
        magnitude = np.abs(audio)
        peaks = magnitude > knee
        magnitude[peaks] = knee + (ceiling - knee) * np.tanh((magnitude[peaks] - knee) / (ceiling - knee))
        return np.rint(np.copysign(magnitude, audio) * 32767).astype('<i2').tobytes()
