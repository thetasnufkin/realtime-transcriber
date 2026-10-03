"""サーバーの受信ループ（app.py の ingest）を仮想時計で再現して、確定までの遅延を測る

実際に 0.1 秒ずつ実時間で流すと、10 分の音声に 10 分かかる。代わりに
「チャンク i は (i+1) * 0.1 秒に届く」「認識している間（実測の処理時間）は時計が進む」
「処理が終わった時点までに届いたチャンクはまとめて渡す（app.py と同じ）」として時計を進める。
GPU の処理時間は本物なので、遅延と RTF は実時間で流した場合と同じになる。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from realtime_transcriber.segmenter import Event, Session

SR = 16000
CHUNK_S = 0.1  # capture_client が送る単位
IDLE_TIMEOUT_S = 0.8  # app.py と同じ: 音声が途切れたら確定する


@dataclass(frozen=True)
class Emitted:
    event: Event
    at: float  # 結果が出た時刻（ストリームの先頭からの実時間）

    @property
    def latency(self) -> float:
        """区間の終わり（話し終わり）から結果が出るまで"""
        return self.at - self.event.end


@dataclass(frozen=True)
class SimResult:
    emitted: list[Emitted]
    processing_s: float  # 認識・VAD にかかった時間の合計
    duration_s: float  # ストリームの長さ（実時間）

    @property
    def finals(self) -> list[Emitted]:
        return [e for e in self.emitted if e.event.kind == "final"]

    @property
    def hypothesis(self) -> str:
        return "".join(e.event.text for e in self.finals)

    @property
    def rtf(self) -> float:
        return self.processing_s / self.duration_s

    def latency_percentiles(self, *qs: float) -> list[float]:
        lat = [e.latency for e in self.finals]
        return [float(np.percentile(lat, q)) for q in qs] if lat else [float("nan")] * len(qs)


def simulate(
    session: Session, audio: np.ndarray, clock: Callable[[], float] = time.perf_counter
) -> SimResult:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    chunk = int(CHUNK_S * SR) * 2
    chunks = [pcm[i : i + chunk] for i in range(0, len(pcm), chunk)]
    arrival = [(i + 1) * CHUNK_S for i in range(len(chunks))]

    now, nxt, total = 0.0, 0, 0.0
    emitted: list[Emitted] = []

    def step(force: bool) -> None:
        nonlocal now, total
        t0 = clock()
        events = session.step(force)
        dt = clock() - t0
        now += dt
        total += dt
        emitted.extend(Emitted(e, now) for e in events)

    while nxt < len(chunks):
        now = max(now, arrival[nxt])
        while nxt < len(chunks) and arrival[nxt] <= now:  # 認識中に届いた分はまとめて渡す
            session.add(chunks[nxt])
            nxt += 1
        step(force=False)
    # 入力が途切れたら IDLE_TIMEOUT_S 後に残りを確定する
    while len(session.buf):
        now = max(now, arrival[-1] + IDLE_TIMEOUT_S)
        step(force=True)
    return SimResult(emitted, total, len(audio) / SR)
