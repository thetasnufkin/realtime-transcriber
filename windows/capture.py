"""capture.py（Windows 側）再生中の音を取り込み、文字起こしサーバーへ送る

Windows で今鳴っている音（ブラウザ・動画・会議アプリなど）を WASAPI ループバックで
直接キャプチャし、16kHz / mono / int16 に変換して WSL のサーバーの /ingest へ送る。
マイクは使わない。サーバーが起動していなければ、準備できるまで自動で再接続を繰り返す。

capture_client.py（コンソール用）と tray_app.py（タスクトレイ用）の両方から使う。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Protocol

import numpy as np
import soxr
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import connect

TARGET_SR = 16000
RETRY_S = 2.0
CHUNK_S = 0.1  # 取り込む単位（サーバーには 0.1 秒ずつ届く）
POLL_S = 0.5  # 無音時はパケットが来ないことがあるので、この間隔で停止の指示を確認する

# on_state(状態, 詳細)。状態は "waiting"（サーバー待ち）/ "streaming"（送信中）/ "stopped"
StateCallback = Callable[[str, str], None]


class AudioSource(Protocol):
    """音声の入力元。テストでは偽物に差し替える"""

    name: str
    rate: int
    channels: int

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        """int16 のインターリーブ PCM を CHUNK_S ごとに on_chunk に渡し始める（別スレッドから呼ばれてよい）"""
        ...

    def close(self) -> None: ...


class LoopbackSource:
    """既定の再生デバイス（スピーカー/ヘッドホン）のループバック"""

    def __init__(self) -> None:
        import pyaudiowpatch as pyaudio  # Windows 専用。テストでは読み込まない

        self._pyaudio = pyaudio
        self._p = pyaudio.PyAudio()
        try:
            dev = self._find_loopback()
        except Exception:
            self._p.terminate()
            raise
        self.name = dev["name"]
        self.rate = int(dev["defaultSampleRate"])
        self.channels = dev["maxInputChannels"]
        self._index = dev["index"]
        self._stream = None

    def _find_loopback(self) -> dict:
        pa = self._pyaudio
        wasapi = self._p.get_host_api_info_by_type(pa.paWASAPI)
        speaker = self._p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if speaker.get("isLoopbackDevice"):
            return speaker
        for lb in self._p.get_loopback_device_info_generator():
            if speaker["name"] in lb["name"]:
                return lb
        raise RuntimeError(f"ループバックデバイスが見つかりません（再生デバイス: {speaker['name']}）")

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        pa = self._pyaudio

        def callback(data, frame_count, time_info, status):
            on_chunk(data)
            return (None, pa.paContinue)

        self._stream = self._p.open(
            format=pa.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self._index,
            frames_per_buffer=int(self.rate * CHUNK_S),
            stream_callback=callback,
        )

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
        self._p.terminate()


def drain(q: queue.Queue) -> None:
    """接続していない間に溜まった古い音声を捨てる"""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def to_pcm16k(data: bytes, channels: int, resampler: soxr.ResampleStream) -> bytes:
    """int16 インターリーブ → モノラル → 16kHz int16"""
    x = np.frombuffer(data, dtype=np.int16).reshape(-1, channels).mean(axis=1)
    y = resampler.resample_chunk((x / 32768.0).astype(np.float32))
    return (np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes()


class LoopbackStreamer:
    """音声をサーバーへ送り続ける。run() は stop() が呼ばれるまで戻らない"""

    def __init__(
        self,
        url: str,
        on_state: StateCallback = lambda state, detail: None,
        open_source: Callable[[], AudioSource] = LoopbackSource,
    ):
        self.url = url
        self.on_state = on_state
        self.open_source = open_source
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="loopback-streamer", daemon=True)
        t.start()
        return t

    def run(self) -> None:
        source = self.open_source()  # デバイスが見つからなければここで例外
        q: queue.Queue[bytes] = queue.Queue()
        try:
            source.start(q.put)
            self.on_state("device", f"{source.name} ({source.rate}Hz, {source.channels}ch)")
            while not self._stop.is_set():  # 接続が切れたら再接続する
                try:
                    self._send(q, source)
                except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake):
                    self.on_state("waiting", self.url)
                    self._stop.wait(RETRY_S)
        finally:
            source.close()
            self.on_state("stopped", "")

    def _send(self, q: queue.Queue[bytes], source: AudioSource) -> None:
        with connect(self.url, max_size=None, open_timeout=3) as ws:
            self.on_state("streaming", self.url)
            drain(q)
            resampler = soxr.ResampleStream(source.rate, TARGET_SR, 1, dtype="float32")
            while not self._stop.is_set():
                try:
                    data = q.get(timeout=POLL_S)
                except queue.Empty:
                    continue
                pcm = to_pcm16k(data, source.channels, resampler)
                if pcm:
                    ws.send(pcm)
