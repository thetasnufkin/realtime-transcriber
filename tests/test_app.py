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


def test_health_waits_for_the_model(tmp_path: Path):
    gate = threading.Event()
    with make_client(tmp_path, gate) as client:
        r = client.get("/api/health")
        assert (r.status_code, r.json()["status"]) == (503, "loading")
        gate.set()
        wait_ready(client)
        assert client.get("/api/health").json() == {"status": "ready", "paused": False}


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

        # ダウンロード
        txt = client.get("/api/transcript.txt")
        assert txt.headers["content-disposition"].endswith('.txt"')
        assert txt.text == f"[{f['at']}] {f['text']}\n"
        srt = client.get("/api/transcript.srt")
        assert srt.headers["content-type"].startswith("application/x-subrip")
        assert srt.text.startswith("1\n00:00:00,") and f["text"] in srt.text
        assert client.get("/api/transcript.csv").status_code == 404

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
def start_live_server(tmp_path: Path):
    """本物の uvicorn をスレッドで起動する（空いているポートを使う）。TestClient と違い、
    実際のクライアントが受け取る応答（HTTP ステータス・切断時の挙動）を確かめられる"""
    servers = []

    def start(gate: threading.Event | None = None) -> str:
        def load(cfg: Config) -> FakeTranscriber:
            if gate is not None:
                assert gate.wait(5)
            return FakeTranscriber()

        app = create_app(Config(out_dir=str(tmp_path)), load, vad=energy_vad, echo=False)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append((server, thread))
        deadline = time.monotonic() + 5
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return f"127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(5)


def wait_live_ready(addr: str) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            urllib.request.urlopen(f"http://{addr}/api/health")
            return
        except urllib.error.HTTPError:  # 503: 読み込み中
            assert time.monotonic() < deadline
            time.sleep(0.02)


def test_ingest_during_model_loading_is_accepted(start_live_server):
    """読み込み中に繋いでも断らない（旧 capture_client は 403 を受けると再接続せずに落ちる）。
    準備前の音声は捨て、準備後の音声から認識する"""
    gate = threading.Event()
    live_server = start_live_server(gate)
    with connect(f"ws://{live_server}/captions") as cap:
        assert json.loads(cap.recv())["type"] == "history"
        assert json.loads(cap.recv())["model_state"] == "loading"
        with connect(f"ws://{live_server}/", open_timeout=3) as ing:  # 旧パスで接続
            send_audio(ing, np.concatenate([tone(1.0), silence(1.0)]))  # 準備前: 捨てられる
            time.sleep(0.3)
            gate.set()
            wait_live_ready(live_server)
            send_audio(ing, np.concatenate([tone(2.0), silence(1.0)]))  # 準備後: 認識される
            msgs = []
            while not (msgs and msgs[-1]["type"] == "final"):
                msgs.append(json.loads(cap.recv(timeout=5)))

    finals = [m for m in msgs if m["type"] == "final"]
    assert len(finals) == 1
    assert float(finals[0]["text"].removesuffix("s")) >= 2.0  # 準備後の 2 秒の発話


def test_disconnect_flushes_the_rest(start_live_server):
    """無音を待たずに切断しても、途中の区間を確定させてから暫定表示を消す"""
    live_server = start_live_server()
    wait_live_ready(live_server)

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


def test_log_output_has_no_terminal_control_codes(tmp_path: Path, capsys):
    """トレイから起動したとき（標準出力がファイル）は、確定文だけを普通の行で書く"""
    import asyncio

    from realtime_transcriber.hub import Hub
    from realtime_transcriber.segmenter import Event

    hub = Hub(tmp_path, echo=True)  # pytest の標準出力は端末ではない
    asyncio.run(hub.publish(1, Event("partial", "途中", 0, 1), 0))
    asyncio.run(hub.publish(1, Event("final", "確定した文。", 0, 1), 0))
    asyncio.run(hub.clear(1))
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out and "途中" not in out
    assert out.endswith("] 確定した文。\n")
