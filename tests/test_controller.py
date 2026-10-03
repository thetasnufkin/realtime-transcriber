"""windows/controller.py: 起動・終了の手順（偽の Platform と取り込みで検証する）"""

import threading
import time

import pytest
from controller import KILL_TIMEOUT_S, SHUTDOWN_TIMEOUT_S, START_TIMEOUT_S, Controller, Status


class FakeProcess:
    def __init__(self, platform: "FakePlatform") -> None:
        self.platform = platform
        self.exit_code: int | None = None

    def poll(self):
        return self.exit_code

    def kill(self) -> None:
        self.platform.events.append("process.kill")
        self.exit_code = -9
        self.platform.state = "down"


class FakePlatform:
    """state を書き換えてサーバーの様子を真似る。呼ばれた順番を events に残す"""

    viewer_url = "http://localhost:8765/"
    log_path = r"C:\logs\server.log"

    def __init__(self, state: str = "down") -> None:
        self.state = state
        self.events: list[str] = []
        self.process: FakeProcess | None = None
        self.on_launch = lambda: None  # 起動後のサーバーの様子
        self.obeys_shutdown = True
        self.obeys_kill = True

    def server_state(self) -> str:
        return self.state

    def launch_server(self) -> FakeProcess:
        self.events.append("launch")
        self.process = FakeProcess(self)
        self.on_launch()
        return self.process

    def request_shutdown(self) -> None:
        self.events.append("shutdown")
        if self.obeys_shutdown:
            self._stop()

    def kill_server(self) -> None:
        self.events.append("kill_server")
        if self.obeys_kill:
            self._stop()

    def _stop(self) -> None:
        self.state = "down"
        if self.process is not None:
            self.process.exit_code = 0

    def open_browser(self, url: str) -> None:
        self.events.append(f"browser {url}")


class FakeStreamer:
    def __init__(self, platform: FakePlatform, on_state, fail: Exception | None = None) -> None:
        self.platform = platform
        self.on_state = on_state
        self.fail = fail
        self._stop = threading.Event()

    def run(self) -> None:
        if self.fail:
            raise self.fail
        self.platform.events.append("capture.start")
        self.on_state("streaming", "ws://127.0.0.1:8765/ingest")
        self._stop.wait()
        self.platform.events.append("capture.stop")
        self.on_state("stopped", "")

    def stop(self) -> None:
        self._stop.set()


class Clock:
    """sleep で進む時計（タイムアウトの試験を一瞬で終わらせる）"""

    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, s: float) -> None:
        self.t += s
        time.sleep(0.001)

    def __call__(self) -> float:
        return self.t


def make(platform: FakePlatform, fail: Exception | None = None):
    statuses: list[Status] = []
    clock = Clock()
    c = Controller(
        platform,
        lambda on_state: FakeStreamer(platform, on_state, fail),
        on_status=statuses.append,
        sleep=clock.sleep,
        clock=clock,
    )
    return c, statuses, clock


def run_in_thread(c: Controller) -> threading.Thread:
    t = threading.Thread(target=c.run, daemon=True)
    t.start()
    return t


def wait_for(cond, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "時間内に条件を満たさなかった"
        time.sleep(0.005)


def test_launches_server_starts_capture_and_opens_browser_once_it_answers():
    p = FakePlatform("down")
    c, statuses, clock = make(p)
    p.on_launch = lambda: threading.Timer(0.05, lambda: setattr(p, "state", "loading")).start()
    t = run_in_thread(c)
    wait_for(lambda: c.status.phase == "running")

    # 取り込みはサーバーを待たずに始め、ブラウザは応答した時点（読み込み中でも）で 1 回だけ開く
    assert p.events[:3] == ["launch", "capture.start", "browser http://localhost:8765/"]
    assert c.status.server == "loading" and not c.reused
    p.state = "ready"
    wait_for(lambda: c.status.server == "ready")
    assert c.quit() == "停止しました"
    t.join(2)
    assert p.events.count("browser http://localhost:8765/") == 1


def test_reuses_a_running_server_and_stops_it_on_quit():
    p = FakePlatform("ready")
    c, _, _ = make(p)
    t = run_in_thread(c)
    wait_for(lambda: c.status.phase == "running")
    assert c.reused and "launch" not in p.events

    c.quit()
    t.join(2)
    assert "shutdown" in p.events  # 使い回したサーバーも止める
    assert c.status.phase == "stopped"


def test_quit_stops_capture_before_shutting_down_the_server():
    p = FakePlatform("ready")
    c, _, _ = make(p)
    t = run_in_thread(c)
    wait_for(lambda: "capture.start" in p.events)
    c.quit()
    t.join(2)
    # 入力を先に閉じると、サーバーは途中の区間を確定してから止まる
    assert p.events.index("capture.stop") < p.events.index("shutdown")
    assert "kill_server" not in p.events


def test_quit_kills_the_server_if_shutdown_is_ignored():
    p = FakePlatform("down")
    p.on_launch = lambda: setattr(p, "state", "ready")
    p.obeys_shutdown = False
    c, _, clock = make(p)
    t = run_in_thread(c)
    wait_for(lambda: c.status.phase == "running")
    start = clock()
    assert c.quit() == "サーバーが応答しないため強制終了しました"
    t.join(2)
    assert p.events[-2:] == ["shutdown", "kill_server"]
    assert clock() - start >= SHUTDOWN_TIMEOUT_S  # 猶予を待ってから


def test_quit_kills_wsl_exe_as_a_last_resort():
    p = FakePlatform("down")
    p.on_launch = lambda: setattr(p, "state", "ready")
    p.obeys_shutdown = p.obeys_kill = False
    c, _, clock = make(p)
    t = run_in_thread(c)
    wait_for(lambda: c.status.phase == "running")
    start = clock()
    c.quit()
    t.join(2)
    assert p.events[-3:] == ["shutdown", "kill_server", "process.kill"]
    assert clock() - start >= SHUTDOWN_TIMEOUT_S + KILL_TIMEOUT_S


def test_server_exiting_during_startup_is_reported_with_the_log_path():
    p = FakePlatform("down")
    c, _, clock = make(p)
    p.on_launch = lambda: setattr(p.process, "exit_code", 1)
    c.run()
    assert c.status.phase == "failed" and p.log_path in c.status.message
    assert "起動直後に終了" in c.status.message and clock() < START_TIMEOUT_S  # タイムアウトを待たずに気づく
    assert c.status.capture == "off"
    assert not any(e.startswith("browser") for e in p.events)


def test_server_not_answering_times_out():
    p = FakePlatform("down")
    c, _, clock = make(p)
    c.run()
    assert c.status.phase == "failed" and "応答しませんでした" in c.status.message
    assert clock() >= START_TIMEOUT_S


def test_server_stopping_on_its_own_is_reported():
    p = FakePlatform("ready")
    c, _, _ = make(p)
    t = run_in_thread(c)
    wait_for(lambda: c.status.phase == "running")
    p.state = "down"
    t.join(2)
    assert c.status.phase == "failed" and c.status.message == "サーバーが停止しました"
    assert c.status.capture == "off"


def test_capture_can_be_toggled_and_device_errors_are_reported():
    p = FakePlatform("ready")
    c, _, _ = make(p)
    t = run_in_thread(c)
    wait_for(lambda: c.status.capture == "streaming")
    c.set_capture(False)
    assert c.status.capture == "off" and p.events.count("capture.stop") == 1
    c.set_capture(True)
    wait_for(lambda: p.events.count("capture.start") == 2)
    c.quit()
    t.join(2)

    p2 = FakePlatform("ready")
    c2, _, _ = make(p2, fail=RuntimeError("ループバックデバイスが見つかりません"))
    t2 = run_in_thread(c2)
    # 取り込みのエラーは、起動の手順が進んでも（running になっても）消えない。サーバーと字幕画面は使える
    wait_for(lambda: c2.status.phase == "running")
    time.sleep(0.05)
    assert c2.status.capture == "error" and "ループバックデバイス" in c2.status.message
    c2.quit()
    t2.join(2)


@pytest.mark.parametrize("state", ["down", "ready"])
def test_quit_during_startup_does_not_open_the_browser(state):
    p = FakePlatform("down")
    c, _, _ = make(p)
    t = run_in_thread(c)
    wait_for(lambda: "launch" in p.events)
    c.quit()
    p.state = state  # 終了の指示の後にサーバーが応答しても
    t.join(2)
    assert not any(e.startswith("browser") for e in p.events)
    assert c.status.phase == "stopped"
