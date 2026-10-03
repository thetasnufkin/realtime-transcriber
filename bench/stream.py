"""評価用の音声ストリームを作る: 発話を間を空けてつなぐ、倍速にする"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from audiotsm import wsola
from audiotsm.io.array import ArrayReader, ArrayWriter

SR = 16000
GAP_S = 1.0  # 発話と発話の間（話者が息継ぎする程度）
LEAD_S = 0.5  # 先頭の無音


@dataclass(frozen=True)
class Segment:
    text: str
    start: float  # ストリーム内の秒数
    end: float


@dataclass(frozen=True)
class Stream:
    audio: np.ndarray
    segments: list[Segment]

    @property
    def duration(self) -> float:
        return len(self.audio) / SR

    @property
    def reference(self) -> str:
        return "".join(s.text for s in self.segments)


def build_stream(utterances: Sequence[tuple[str, np.ndarray]], gap_s: float = GAP_S) -> Stream:
    parts = [np.zeros(int(LEAD_S * SR), dtype=np.float32)]
    segments, t = [], LEAD_S
    for text, audio in utterances:
        parts.append(audio.astype(np.float32))
        segments.append(Segment(text, t, t + len(audio) / SR))
        t += len(audio) / SR
        parts.append(np.zeros(int(gap_s * SR), dtype=np.float32))
        t += int(gap_s * SR) / SR
    return Stream(np.concatenate(parts), segments)


def speed_up(stream: Stream, speed: float) -> Stream:
    """動画の倍速再生と同じく、ピッチを保ったまま速くする（ブラウザの倍速再生も同種の方式）"""
    if abs(speed - 1.0) < 1e-3:
        return stream
    writer = ArrayWriter(channels=1)
    wsola(channels=1, speed=speed).run(ArrayReader(stream.audio.reshape(1, -1)), writer)
    audio = writer.data[0].astype(np.float32)
    segments = [Segment(s.text, s.start / speed, s.end / speed) for s in stream.segments]
    return Stream(audio, segments)
