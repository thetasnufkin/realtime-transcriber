"""windows/capture.py: 取り込んだ音声の変換・送信・再接続・停止（偽の入力元で WSL でも動かす）"""

import socket
import threading
import time

import numpy as np
import pytest
from capture import TARGET_SR, LoopbackStreamer
from websockets.sync.server import serve

RATE, CHANNELS = 48000, 2  # よくある再生デバイスの形式


class FakeSource:
    """48kHz ステレオのトーンを 0.1 秒ずつ（実時間より速く）出す"""

    name = "Fake Speaker [Loopback]"
    rate = RATE
    channels = CHANNELS

    def __init__(self, interval: float = 0.01) -> None:
        self.interval = interval
        self.closed = threading.Event()
        self.chunks_sent = 0

    def start(self, on_chunk) -> None:
        def loop() -> None:
            t = np.arange(int(RATE * 0.1)) / RATE
            left = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
            right = np.zeros_like(left)  # 片側だけ鳴っている → モノラルでは振幅が半分
            chunk = np.stack([left, right], axis=1).tobytes()
            while not self.closed.is_set():
                on_chunk(chunk)
                self.chunks_sent += 1
                time.sleep(self.interval)

        threading.Thread(target=loop, daemon=True).start()

    def close(self) -> None:
        self.closed.set()


class Receiver:
    """/ingest の代わり。受け取った PCM を貯める"""

    def __init__(self, port: int) -> None:
        self.data = bytearray()
        self.connections = 0

        def handler(ws) -> None:
            self.connections += 1
            for msg in ws:
                self.data += msg

        self.server = serve(handler, "127.0.0.1", port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "時間内に条件を満たさなかった"
        time.sleep(0.02)


@pytest.fixture
def port():
    return free_port()


def start(port: int, source: FakeSource, states: list) -> tuple[LoopbackStreamer, threading.Thread]:
    s = LoopbackStreamer(
        f"ws://127.0.0.1:{port}/", on_state=lambda st, d: states.append(st), open_source=lambda: source
    )
    return s, s.start_thread()


def test_streams_16k_mono_and_stops_promptly(port: int):
    rx = Receiver(port)
    source, states = FakeSource(), []
    streamer, thread = start(port, source, states)
    try:
        wait_for(lambda: len(rx.data) >= TARGET_SR * 2 * 2)  # 2 秒分
    finally:
        t = time.monotonic()
        streamer.stop()
        thread.join(2)
        rx.close()

    assert not thread.is_alive() and time.monotonic() - t < 1.0  # 停止の指示から 1 秒以内に止まる
    assert source.closed.is_set()  # デバイスを閉じる
    assert states[:2] == ["device", "streaming"] and states[-1] == "stopped"
    pcm = np.frombuffer(bytes(rx.data), dtype=np.int16)
    # 送ったチャンク数（48kHz）と受け取ったサンプル数（16kHz）の比が 1/3（リサンプラーの遅延分を除く）
    assert len(pcm) == pytest.approx(source.chunks_sent * RATE * 0.1 / 3, abs=TARGET_SR * 0.3)
    peak = np.abs(pcm[TARGET_SR // 2 :]).max() / 32767  # 立ち上がりを除く
    assert peak == pytest.approx(0.25, abs=0.02)  # 左 0.5 / 右 0 の平均


def test_waits_for_the_server_and_drops_audio_from_before_connecting(port: int):
    source, states = FakeSource(), []
    streamer, thread = start(port, source, states)
    try:
        wait_for(lambda: "waiting" in states)
        time.sleep(1.0)  # サーバーがない間も音声は溜まる
        before = source.chunks_sent
        rx = Receiver(port)
        wait_for(lambda: "streaming" in states, timeout=5)
        wait_for(lambda: len(rx.data) > 0)
        time.sleep(0.3)
    finally:
        streamer.stop()
        thread.join(2)
        rx.close()

    sent_after = source.chunks_sent - before
    received_chunks = len(rx.data) / 2 / (TARGET_SR * 0.1)
    assert before > 20
    assert received_chunks <= sent_after + 1  # 接続前に溜まった分は送らない


def test_reconnects_after_the_server_restarts(port: int):
    rx = Receiver(port)
    source, states = FakeSource(), []
    streamer, thread = start(port, source, states)
    try:
        wait_for(lambda: len(rx.data) > 0)
        rx.close()  # サーバーが落ちる
        wait_for(lambda: "waiting" in states)
        rx2 = Receiver(port)
        wait_for(lambda: len(rx2.data) > 0, timeout=6)  # RETRY_S 後に再接続
    finally:
        streamer.stop()
        thread.join(2)
        rx2.close()
    assert states.count("streaming") == 2


def test_missing_device_raises_from_run(port: int):
    def no_device():
        raise RuntimeError("ループバックデバイスが見つかりません")

    with pytest.raises(RuntimeError, match="ループバック"):
        LoopbackStreamer(f"ws://127.0.0.1:{port}/", open_source=no_device).run()
