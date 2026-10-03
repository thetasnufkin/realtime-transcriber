"""GPU・モデルなしでテストするための偽物"""

from __future__ import annotations

import numpy as np
from faster_whisper.vad import VadOptions

from realtime_transcriber.audio import SR

FRAME = 160  # 10ms


class FakeTranscriber:
    """渡された音声を記録し、長さを表す文字列を返す"""

    def __init__(self) -> None:
        self.calls: list[tuple[bool, np.ndarray]] = []

    def transcribe(self, audio: np.ndarray, context: str, final: bool) -> str:
        self.calls.append((final, audio.copy()))
        return f"{len(audio) / SR:.2f}s"

    @property
    def finals(self) -> list[np.ndarray]:
        return [a for final, a in self.calls if final]


def energy_vad(audio: np.ndarray, opts: VadOptions) -> list[dict[str, int]]:
    """Silero の代わり。10ms ごとのエネルギーで判定し、無音の結合・短い発話の除去・前後の余白を真似る"""
    n = len(audio) // FRAME
    active = np.abs(audio[: n * FRAME]).reshape(n, FRAME).max(axis=1) > 0.01
    raw: list[list[int]] = []
    for i, a in enumerate(active):
        if not a:
            continue
        if raw and i * FRAME - raw[-1][1] < opts.min_silence_duration_ms * SR // 1000:
            raw[-1][1] = (i + 1) * FRAME
        else:
            raw.append([i * FRAME, (i + 1) * FRAME])
    min_len = opts.min_speech_duration_ms * SR // 1000
    pad = opts.speech_pad_ms * SR // 1000
    segs = [s for s in raw if s[1] - s[0] >= min_len]
    out = []
    for i, (s, e) in enumerate(segs):
        lo = max(0, s - pad) if i == 0 else max(s - pad, (segs[i - 1][1] + s) // 2)
        hi = min(len(audio), e + pad) if i == len(segs) - 1 else min(e + pad, (e + segs[i + 1][0]) // 2)
        out.append({"start": lo, "end": hi})
    return out


def tone(seconds: float, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * freq * t) + 0.05).astype(np.float32)  # 0 を跨がないようにずらす


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def to_pcm(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
