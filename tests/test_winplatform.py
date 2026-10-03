"""windows/winplatform.py と scripts/stop_server.sh（Windows 以外でも確かめられる部分）"""

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from test_app import start_live_server, wait_live_ready  # noqa: F401  start_live_server はフィクスチャ
from winplatform import WindowsPlatform, read_port, wsl_location

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"\\wsl.localhost\Ubuntu\home\me\projects\rt", ("Ubuntu", "/home/me/projects/rt")),
        (r"\\wsl$\Ubuntu-24.04\home\me\rt", ("Ubuntu-24.04", "/home/me/rt")),
        (r"\\WSL.LOCALHOST\Ubuntu", ("Ubuntu", "/")),
    ],
)
def test_wsl_location(path: str, expected: tuple[str, str]):
    assert wsl_location(path) == expected


def test_wsl_location_rejects_windows_paths():
    with pytest.raises(ValueError, match="WSL"):
        wsl_location(r"C:\Users\me\rt")


def test_read_port(tmp_path: Path):
    assert read_port(tmp_path) == 8765
    (tmp_path / "config.toml").write_text("port = 9001\n", encoding="utf-8")
    assert read_port(tmp_path) == 9001


def test_server_command_runs_start_server_sh_in_the_project(tmp_path: Path):
    p = WindowsPlatform(Path(r"\\wsl.localhost\Ubuntu\home\me\my proj"), tmp_path, port=9001)
    assert p.server_command() == [
        "wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc",
        "cd '/home/me/my proj' && PYTHONUNBUFFERED=1 exec ./scripts/start_server.sh --port 9001",
    ]  # fmt: skip
    assert p.viewer_url == "http://localhost:9001/"


def test_server_state_reads_health(tmp_path: Path, start_live_server):  # noqa: F811
    gate = threading.Event()
    addr = start_live_server(gate)
    port = int(addr.rsplit(":", 1)[1])
    p = WindowsPlatform(Path(r"\\wsl.localhost\Ubuntu\home\me\rt"), tmp_path, port=port)

    assert p.server_state() == "loading"  # 503 でも応答とみなす
    gate.set()
    wait_live_ready(addr)
    assert p.server_state() == "ready" and p.paused is False
    p.set_paused(True)
    assert p.server_state() == "ready" and p.paused is True
    p.set_paused(False)
    assert p.server_state() == "ready" and p.paused is False

    down = WindowsPlatform(Path(r"\\wsl.localhost\Ubuntu\home\me\rt"), tmp_path, port=1)
    assert down.server_state() == "down"
    down.request_shutdown()  # 止まっているサーバーに送っても例外にならない


# --- scripts/stop_server.sh ---


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "stop_server.sh", tmp_path / "scripts")
    (tmp_path / "run").mkdir()
    return tmp_path


def fake_server(ignore_term: bool = False) -> subprocess.Popen:
    """コマンドラインに realtime_transcriber を含むプロセス（本物のサーバーの代わり）"""
    trap = "trap '' TERM; " if ignore_term else ""
    return subprocess.Popen(["bash", "-c", f"{trap}exec -a 'python -m realtime_transcriber' sleep 60"])


def stop(project: Path, grace: str = "5") -> str:
    r = subprocess.run(
        [project / "scripts" / "stop_server.sh", grace], capture_output=True, text=True, timeout=20
    )
    return r.stdout


def test_stop_server_stops_the_server(project: Path):
    proc = fake_server()
    time.sleep(0.2)
    (project / "run" / "server.pid").write_text(str(proc.pid))
    out = stop(project)
    assert proc.wait(2) is not None and "終了しました" in out
    assert not (project / "run" / "server.pid").exists()


def test_stop_server_kills_if_term_is_ignored(project: Path):
    proc = fake_server(ignore_term=True)
    time.sleep(0.2)
    (project / "run" / "server.pid").write_text(str(proc.pid))
    out = stop(project, grace="1")
    assert proc.wait(2) == -9 and "強制終了" in out


def test_stop_server_does_not_touch_a_reused_pid(project: Path):
    other = subprocess.Popen(["sleep", "60"])  # PID が別のプロセスに使い回された状況
    try:
        (project / "run" / "server.pid").write_text(str(other.pid))
        out = stop(project)
        assert other.poll() is None and "サーバーではありません" in out
    finally:
        other.kill()


def test_stop_server_without_pid_file(project: Path):
    assert "ありません" in stop(project)


def test_start_server_writes_its_pid(tmp_path: Path):
    """start_server.sh は exec する前に自分の PID（= python の PID）を書く"""
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "start_server.sh", tmp_path / "scripts")
    shutil.copy(ROOT / "config.example.toml", tmp_path)
    # 偽の python（自分の PID を書いて終わる）を PATH の先頭に置く。
    # exec はシェル関数を呼べないので実行ファイルにする（本物のサーバーを起動しないように）
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "python").write_text(f'#!/usr/bin/env bash\necho $$ > "{tmp_path}/actual.pid"\n')
    (fakebin / "python").chmod(0o755)
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "activate").write_text(f'export PATH="{fakebin}:$PATH"\n')

    r = subprocess.run([tmp_path / "scripts" / "start_server.sh"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "run" / "server.pid").read_text().strip() == (
        tmp_path / "actual.pid"
    ).read_text().strip()
