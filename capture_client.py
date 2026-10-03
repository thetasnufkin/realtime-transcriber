# /// script
# requires-python = "==3.12.*"
# dependencies = ["pyaudiowpatch", "numpy", "soxr", "websockets"]
# ///
"""capture_client.py  (Windows 側で動かす)

Windows で今鳴っている音（ブラウザ・動画・会議アプリなど）を WASAPI ループバックで
直接キャプチャし、16kHz / mono / int16 に変換して WSL の文字起こしサーバーへ送る。
マイクは使わない。サーバーが起動していなければ、準備できるまで自動で再接続を繰り返す。

  uv run capture_client.py
  uv run capture_client.py --url ws://localhost:9000/ingest   # ポートを変えた場合
"""
import argparse
import queue
import time

import numpy as np
import pyaudiowpatch as pyaudio
import soxr
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import connect

TARGET_SR = 16000
RETRY_S = 2.0


def find_loopback(p: pyaudio.PyAudio) -> dict:
    """既定の再生デバイス（スピーカー/ヘッドホン）に対応するループバックデバイスを探す"""
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    speaker = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if speaker.get("isLoopbackDevice"):
        return speaker
    for lb in p.get_loopback_device_info_generator():
        if speaker["name"] in lb["name"]:
            return lb
    raise RuntimeError(f"ループバックデバイスが見つかりません（再生デバイス: {speaker['name']}）")


def drain(q: queue.Queue) -> None:
    """接続していない間に溜まった古い音声を捨てる"""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8765/ingest")
    args = ap.parse_args()

    p = pyaudio.PyAudio()
    dev = find_loopback(p)
    rate, ch = int(dev["defaultSampleRate"]), dev["maxInputChannels"]

    q: queue.Queue[bytes] = queue.Queue()

    def callback(data, frame_count, time_info, status):
        q.put(data)
        return (None, pyaudio.paContinue)

    stream = p.open(
        format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
        input_device_index=dev["index"], frames_per_buffer=int(rate * 0.1),
        stream_callback=callback,
    )
    print(f"[CAPTURE] {dev['name']} ({rate}Hz, {ch}ch)")

    try:
        while True:  # 接続が切れたら再接続する
            try:
                with connect(args.url, max_size=None, open_timeout=3) as ws:
                    print(f"[WS] {args.url} に接続しました。音声を再生してください（Ctrl+C で終了）")
                    drain(q)
                    resampler = soxr.ResampleStream(rate, TARGET_SR, 1, dtype="float32")
                    while True:
                        try:
                            data = q.get(timeout=0.5)  # 無音時はパケットが来ないことがある
                        except queue.Empty:
                            continue
                        x = np.frombuffer(data, dtype=np.int16).reshape(-1, ch).mean(axis=1)
                        y = resampler.resample_chunk((x / 32768.0).astype(np.float32))
                        if len(y):
                            ws.send((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())
            except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake):
                print(f"[WS] サーバー待ち... ({args.url})", end="\r", flush=True)
                time.sleep(RETRY_S)
    except KeyboardInterrupt:
        print("\n終了しました")
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()


if __name__ == "__main__":
    main()
