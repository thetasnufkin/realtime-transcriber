"""ブラウザからの操作: 一時停止 / 再開、設定の変更と保存、モデルの切り替え"""

import threading
import time
import tomllib
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeTranscriber, energy_vad, silence, tone
from fastapi.testclient import TestClient
from test_app import receive_until, send_audio, wait_ready

from realtime_transcriber.app import create_app
from realtime_transcriber.config import Config


class Models:
    """load_transcriber の偽物。読み込んだモデルを記録し、gate で読み込みを止められる"""

    def __init__(self) -> None:
        self.loaded: list[FakeTranscriber] = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail = False

    def __call__(self, cfg: Config) -> FakeTranscriber:
        assert self.gate.wait(5)
        if self.fail:
            raise RuntimeError("モデルが見つかりません")
        tr = FakeTranscriber(name=f"{cfg.model}|{cfg.final_model}")
        tr.set_glossary(cfg.glossary)
        self.loaded.append(tr)
        return tr


def make(tmp_path: Path, cfg: Config | None = None, cli_overrides=()) -> tuple[TestClient, Models, Path]:
    models = Models()
    path = tmp_path / "config.toml"
    cfg = cfg or Config(out_dir=str(tmp_path / "out"))
    app = create_app(cfg, models, vad=energy_vad, echo=False, config_path=path, cli_overrides=cli_overrides)
    return TestClient(app), models, path


def connect_viewer(client: TestClient):
    ws = client.websocket_connect("/captions")
    v = ws.__enter__()
    v.receive_json()  # history
    v.receive_json()  # status
    return ws, v


def test_pause_discards_pending_audio_and_audio_while_paused(tmp_path: Path):
    client, _, _ = make(tmp_path)
    with client:
        wait_ready(client)
        ctx, viewer = connect_viewer(client)
        with client.websocket_connect("/ingest") as ing:
            send_audio(ing, tone(1.0))  # 発話の途中（区切り待ち）
            partial = receive_until(viewer, lambda m: m["type"] == "partial")[-1]

            assert client.post("/api/pause").json() == {"paused": True}
            msgs = receive_until(viewer, lambda m: m["type"] == "status" and m.get("paused"))
            assert {"type": "clear", "source": partial["source"]} in msgs  # 暫定表示を消す

            send_audio(ing, np.concatenate([tone(2.0), silence(1.0)]))  # 停止中: 捨てる
            time.sleep(0.3)
            assert client.post("/api/resume").json() == {"paused": False}

            send_audio(ing, np.concatenate([silence(0.5), tone(1.5), silence(1.0)]))
            final = receive_until(viewer, lambda m: m["type"] == "final")[-1]
        ctx.__exit__(None, None, None)

    # FakeTranscriber は渡された音声の長さを返す: 再開後の発話（1.5 秒 + 余白）だけ
    assert 1.5 <= float(final["text"].removesuffix("s")) < 2.5
    # 停止前の 1 秒と停止中の 3 秒も経過時間に数える（SRT の時刻をずらさない）
    assert final["start"] == pytest.approx(4.5, abs=0.2)
    assert final["id"] == 1  # 停止前・停止中の音声からは確定文が出ていない


def test_speed_and_glossary_apply_immediately_and_are_saved(tmp_path: Path):
    client, models, path = make(tmp_path)
    path.write_text("port = 9000\nspeed = 1.0\n", encoding="utf-8")
    # 0.4 秒の間は、等速（区切り 0.6 秒）ではつながり、2 倍速（区切り 0.3 秒）では区切られる
    speech = np.concatenate([tone(1.0), silence(0.4), tone(1.0), silence(1.0)])
    with client:
        wait_ready(client)
        ctx, viewer = connect_viewer(client)
        with client.websocket_connect("/ingest") as ing:
            send_audio(ing, speech)
            msgs = receive_until(viewer, lambda m: m["type"] == "final")
            time.sleep(0.3)  # 1 区間だけで終わる（等速）

            r = client.put("/api/config", json={"speed": 2.0, "glossary": ["東雲電機", "汐留第二開発課"]})
            assert r.status_code == 200 and r.json()["switching_models"] is False
            assert models.loaded[0].glossary == ["東雲電機", "汐留第二開発課"]

            send_audio(ing, speech)  # 接続中のセッションにも新しい速度が効く
            msgs += receive_until(viewer, lambda m: m["type"] == "final")
            msgs += receive_until(viewer, lambda m: m["type"] == "final")
        ctx.__exit__(None, None, None)
        assert client.get("/api/config").json()["speed"] == 2.0
    assert [m["id"] for m in msgs if m["type"] == "final"] == [1, 2, 3]

    saved = tomllib.loads(path.read_text(encoding="utf-8"))
    assert saved == {"port": 9000, "speed": 2.0, "glossary": ["東雲電機", "汐留第二開発課"]}  # 他の項目は残す
    assert path.read_text(encoding="utf-8").startswith("# ")  # 説明の場所を書いておく
    assert len(models.loaded) == 1  # モデルは読み込み直さない


@pytest.mark.parametrize(
    "body",
    [{"speed": 9.0}, {"speed": "fast"}, {"glossary": "東雲電機"}, {"port": 1}, {"spead": 1.5}],
)
def test_invalid_config_is_rejected_and_not_saved(tmp_path: Path, body: dict):
    client, _, path = make(tmp_path)
    with client:
        wait_ready(client)
        r = client.put("/api/config", json=body)
        assert r.status_code == 422 and r.json()["error"]
        assert client.get("/api/config").json()["speed"] == 1.0
    assert not path.exists()


def test_model_switch_loads_in_background_then_swaps(tmp_path: Path):
    client, models, path = make(tmp_path)
    with client:
        wait_ready(client)
        ctx, viewer = connect_viewer(client)
        ing_ctx = client.websocket_connect("/ingest")  # 切り替えの前から繋がっている入力
        ing = ing_ctx.__enter__()
        send_audio(ing, silence(0.5))
        models.gate.clear()
        r = client.put("/api/config", json={"final_model": "large-v3"})
        assert r.json()["switching_models"] is True
        receive_until(viewer, lambda m: m["type"] == "status" and m["model_state"] == "switching")

        # 読み込み中も今のモデルで動く。もう一度モデルを変えようとすると断る
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/config").json()["final_model"] == ""
        assert client.put("/api/config", json={"model": "medium"}).status_code == 409
        assert client.put("/api/config", json={"speed": 1.5}).status_code == 200  # モデル以外は変えられる

        models.gate.set()
        msgs = receive_until(viewer, lambda m: m["type"] == "status" and m["model_state"] == "ready")
        assert msgs[-1]["config"]["final_model"] == "large-v3"

        # 繋がったままの入力も、入れ替え後の音声は新しいモデルで認識する
        send_audio(ing, np.concatenate([tone(1.0), silence(1.0)]))
        receive_until(viewer, lambda m: m["type"] == "final")
        ctx.__exit__(None, None, None)
        ing_ctx.__exit__(None, None, None)

    assert [m.name.split("|")[1] for m in models.loaded] == ["", "large-v3"]
    assert models.loaded[0].calls == [] and models.loaded[1].calls
    saved = tomllib.loads(path.read_text(encoding="utf-8"))
    assert saved == {"final_model": "large-v3", "speed": 1.5}  # 変えていない model は書かない


def test_model_switch_failure_keeps_the_current_model(tmp_path: Path):
    client, models, path = make(tmp_path)
    with client:
        wait_ready(client)
        ctx, viewer = connect_viewer(client)
        models.fail = True
        client.put("/api/config", json={"model": "not-a-model"})
        msgs = receive_until(viewer, lambda m: m["type"] == "status" and m.get("model_error"))
        ctx.__exit__(None, None, None)
        assert msgs[-1]["model_state"] == "ready"
        assert "モデルが見つかりません" in msgs[-1]["model_error"]
        assert client.get("/api/config").json()["model"] == Config().model
        assert client.get("/api/health").status_code == 200
    assert not path.exists()


def test_cli_overrides_are_reported_to_the_viewer(tmp_path: Path):
    client, _, _ = make(tmp_path, cli_overrides=["final_model", "host"])
    with client:
        with client.websocket_connect("/captions") as v:
            v.receive_json()
            assert v.receive_json()["cli_overrides"] == ["final_model"]  # ブラウザで変えられる項目だけ
