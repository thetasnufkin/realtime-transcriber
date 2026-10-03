"""VAD で音声ストリームを発話区間に区切り、暫定 (partial) / 確定 (final) の結果を出す"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps

from .audio import SR, time_stretch
from .transcriber import Transcriber

PARTIAL_INTERVAL_S = 1.0  # 暫定結果を出す間隔（実時間）
MIN_SILENCE_S = 0.6  # 通常速度でこれだけ無音が続いたら区切り
MAX_SEGMENT_S = 20.0  # 通常速度に換算した 1 区間の上限（Whisper の窓は 30 秒）
CONTEXT_CHARS = 80  # 次の認識に渡す直前の文脈の長さ

# (音声, 設定) -> [{"start": サンプル位置, "end": サンプル位置}, ...]
Vad = Callable[[np.ndarray, VadOptions], list[dict[str, int]]]


@dataclass(frozen=True)
class Event:
    kind: Literal["partial", "final", "clear"]  # clear: 確定したが文字にならなかった（暫定表示を消す）
    text: str
    start: float  # このストリームの先頭からの秒数（受け取った音声の時間）
    end: float


class Session:
    """1 本の音声ストリームの状態（バッファ・文脈）を持つ"""

    def __init__(self, transcriber: Transcriber, speed: float, vad: Vad = get_speech_timestamps):
        self.tr = transcriber
        self.speed = speed
        self.detect = vad
        self.buf = np.zeros(0, dtype=np.float32)
        self.consumed = 0  # buf の先頭が、ストリームの先頭から何サンプル目か
        self.since_partial = 0
        self.context = ""
        # 倍速だと無音も短くなるので、判定の時間を速度で割る
        self.min_silence = int(MIN_SILENCE_S / speed * SR)
        self.max_len = int(MAX_SEGMENT_S / speed * SR)
        self.vad = VadOptions(
            min_silence_duration_ms=int(MIN_SILENCE_S / speed * 1000),
            min_speech_duration_ms=100,
            speech_pad_ms=int(150 / speed),
        )
        self.vad_fine = VadOptions(
            min_silence_duration_ms=int(120 / speed),
            min_speech_duration_ms=100,
            speech_pad_ms=int(60 / speed),
        )

    def add(self, pcm: bytes) -> None:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self.buf = np.concatenate([self.buf, x])
        self.since_partial += len(x)

    def _best_gap(self, n: int) -> int:
        """上限に達したとき、単語の途中で切らないよう区間内で一番長い「間」の中央を返す"""
        fine = self.detect(self.buf, self.vad_fine)
        gaps = [
            (fine[i + 1]["start"] - fine[i]["end"], (fine[i]["end"] + fine[i + 1]["start"]) // 2)
            for i in range(len(fine) - 1)
            if fine[i]["end"] > n * 0.3  # 短すぎる区間にならないよう後半から選ぶ
        ]
        return max(gaps)[1] if gaps else n

    def _run(self, audio: np.ndarray, final: bool) -> str:
        return self.tr.transcribe(time_stretch(audio, self.speed), self.context, final)

    def _drop(self, n: int) -> None:
        """buf の先頭 n サンプルを捨てる"""
        self.buf = self.buf[n:]
        self.consumed += n

    def _event(self, kind: Literal["partial", "final", "clear"], text: str, start: int, end: int) -> Event:
        return Event(kind, text, (self.consumed + start) / SR, (self.consumed + end) / SR)

    def step(self, force: bool = False) -> list[Event]:
        n = len(self.buf)
        if n < int(0.3 * SR):
            if force:
                self._drop(n)
            return []

        speech = self.detect(self.buf, self.vad)
        if not speech:
            # 発話なし: 古い音は捨てる（発話の頭を切らないよう最後の 0.5 秒だけ残す）
            self._drop(n if force else max(0, n - int(0.5 * SR)))
            self.since_partial = 0
            return []

        last_end = speech[-1]["end"]
        cut = None
        if force:
            cut = n
        elif n - last_end >= self.min_silence:
            cut = last_end
        elif n >= self.max_len:
            cut = self._best_gap(n)

        if cut is not None:
            # 先頭は切り詰めない（短い語頭が VAD で落ちて「抜け」になるのを防ぐ）
            text = self._run(self.buf[:cut], final=True)
            event = self._event("final" if text else "clear", text, speech[0]["start"], min(cut, last_end))
            self._drop(cut)
            self.since_partial = 0
            if text:
                self.context = text[-CONTEXT_CHARS:]
            return [event]

        if self.since_partial >= int(PARTIAL_INTERVAL_S * SR):
            self.since_partial = 0
            text = self._run(self.buf, final=False)
            if text:
                return [self._event("partial", text, speech[0]["start"], last_end)]
        return []
