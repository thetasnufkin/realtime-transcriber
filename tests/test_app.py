import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest
import uvicorn
from fakes import FakeTranscriber, energy_vad, silence, to_pcm, tone
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.client import connect

from realtime_transcriber.app import create_app
from realtime_transcriber.audio import SR
from realtime_transcriber.config import Config

CHUNK_BYTES = int(0.1 * SR) * 2


def make_client(tmp_path: Path, gate: threading.Event | None = None) -> TestClient:
    def load(cfg: Config) -> FakeTranscriber:
        if gate is not None:
            assert gate.wait(5)
        return FakeTranscriber()

    app = create_app(Config(out_dir=str(tmp_path)), load, vad=energy_vad, echo=False)
    return TestClient(app)


def wait_ready(client: TestClient) -> None:
    deadline = time.monotonic() + 5
    while client.get("/api/health").status_code != 200:
        assert time.monotonic() < deadline, "モデルの準備が終わらない"
        time.sleep(0.02)


def send_audio(ws, audio: np.ndarray) -> None:
    pcm = to_pcm(audio)
    send = getattr(ws, "send_bytes", None) or ws.send  # TestClient / websockets の両方
    for i in range(0, len(pcm), CHUNK_BYTES):
        send(pcm[i : i + CHUNK_BYTES])


def receive_until(ws, pred) -> list[dict]:
    msgs = []
    while True:
        msgs.append(ws.receive_json())
        if pred(msgs[-1]):
            return msgs


def test_viewer_and_static_files_are_served(tmp_path: Path):
    with make_client(tmp_path) as client:
        r = client.get("/")
        assert r.status_code == 200 and "リアルタイム字幕" in r.text
        assert client.get("/static/app.js").status_code == 200


def test_health_and_ingest_wait_for_the_model(tmp_path: Path):
    gate = threading.Event()
    with make_client(tmp_path, gate) as client:
        r = client.get("/api/health")
        assert (r.status_code, r.json()["status"]) == (503, "loading")
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect("/ingest"):
                pass
        assert e.value.code == 1013  # capture_client は再接続を繰り返す

        gate.set()
        wait_ready(client)
        assert client.get("/api/health").json() == {"status": "ready"}


def test_load_failure_is_reported(tmp_path: Path):
    def load(cfg: Config):
        raise RuntimeError("CUDA が見つかりません")

    app = create_app(Config(out_dir=str(tmp_path)), load, vad=energy_vad, echo=False)
    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        while client.get("/api/health").json()["status"] == "loading":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert client.get("/api/health").json()["status"] == "error"
        with client.websocket_connect("/captions") as cap:
            cap.receive_json()  # history
            status = cap.receive_json()
            assert "CUDA が見つかりません" in status["error"]


@pytest.mark.parametrize("path", ["/ingest", "/"])  # / は旧 capture_client 用
def test_audio_becomes_captions_for_every_viewer(tmp_path: Path, path: str):
    with make_client(tmp_path) as client:
        wait_ready(client)
        with client.websocket_connect("/captions") as a, client.websocket_connect("/captions") as b:
            for v in (a, b):
                assert v.receive_json() == {"type": "history", "captions": []}
                assert v.receive_json()["model_state"] == "ready"

            # TestClient は切断時にサーバー側の処理をキャンセルするので、確定を待ってから閉じる
            # （切断時の処理は test_disconnect_flushes_the_rest で本物の uvicorn を使って確かめる）
            with client.websocket_connect(path) as ing:
                send_audio(ing, np.concatenate([silence(0.5), tone(1.0), silence(1.0)]))
                for v in (a, b):
                    msgs = receive_until(v, lambda m: m["type"] == "final")
                    assert msgs[0]["type"] == "status" and msgs[0]["ingest_clients"] == 1
                    f = msgs[-1]
                    assert f["id"] == 1 and f["text"].endswith("s")  # FakeTranscriber は長さを返す
                    assert f["start"] == pytest.approx(0.5, abs=0.16)
                    assert f["end"] == pytest.approx(1.5, abs=0.16)

        # 途中から開いた画面にも、それまでの確定文が届く
        with client.websocket_connect("/captions") as late:
            history = late.receive_json()
            assert [c["text"] for c in history["captions"]] == [f["text"]]

        saved = list(tmp_path.glob("*.txt"))
        assert len(saved) == 1 and f["text"] in saved[0].read_text(encoding="utf-8")


def test_shutdown_calls_the_hook(tmp_path: Path):
    with make_client(tmp_path) as client:
        assert client.post("/api/shutdown").status_code == 501
        called = []
        client.app.state.request_shutdown = lambda: called.append(True)
        r = client.post("/api/shutdown")
        assert (r.status_code, called) == (202, [True])


@pytest.fixture
def live_server(tmp_path: Path):
    """本物の uvicorn をスレッドで起動する（空いているポートを使う）"""
    app = create_app(Config(out_dir=str(tmp_path)), lambda cfg: FakeTranscriber(), vad=energy_vad, echo=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_disconnect_flushes_the_rest(live_server: str):
    """無音を待たずに切断しても、途中の区間を確定させてから暫定表示を消す"""
    deadline = time.monotonic() + 5
    while True:
        try:
            urllib.request.urlopen(f"http://{live_server}/api/health")
            break
        except urllib.error.HTTPError:  # 503: 読み込み中
            assert time.monotonic() < deadline
            time.sleep(0.02)

    with connect(f"ws://{live_server}/captions") as cap:
        assert json.loads(cap.recv())["type"] == "history"
        assert json.loads(cap.recv())["type"] == "status"
        with connect(f"ws://{live_server}/ingest") as ing:
            send_audio(ing, tone(1.0))  # 発話の途中で切断
        msgs = []
        while not (msgs and msgs[-1]["type"] == "status" and msgs[-1]["ingest_clients"] == 0):
            msgs.append(json.loads(cap.recv(timeout=5)))

    kinds = [m["type"] for m in msgs if m["type"] != "partial"]
    assert kinds == ["status", "final", "clear", "status"]
