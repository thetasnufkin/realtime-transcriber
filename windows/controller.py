"""controller.py（Windows 側）起動から終了までの手順

トレイアプリから使う。OS とのやりとり（WSL のサーバーの起動・HTTP・ブラウザ）は Platform に
まとめ、テストでは偽物に差し替える（このファイルは Windows 専用のモジュールを読み込まない）。

起動: サーバーが動いていなければ WSL で起動（動いていれば使い回す）→ 取り込みを開始
      → サーバーが応答したらブラウザで字幕画面を開く（1 回だけ）→ 状態を見張り続ける
終了: 取り込みを止める → /api/shutdown → 止まるのを待つ → 止まらなければ PID で止める
      （使い回したサーバーも止める）
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

START_TIMEOUT_S = 120.0  # サーバーが HTTP に応答するまで（WSL の起動を含む。モデルの読み込みは含まない）
SHUTDOWN_TIMEOUT_S = 15.0  # /api/shutdown から止まるまで（途中の区間の確定を含む）
KILL_TIMEOUT_S = 10.0  # stop_server.sh で止めてから
POLL_S = 1.0  # 状態を見張る間隔
CAPTURE_JOIN_S = 3.0

# down: 応答なし / loading・switching・ready・error: /api/health の値
ServerState = Literal["down", "loading", "switching", "ready", "error"]


class ServerProcess(Protocol):
    def poll(self) -> int | None: ...
    def kill(self) -> None: ...


class Streamer(Protocol):
    def run(self) -> None:
        """stop() まで送り続ける。デバイスがないときは例外"""
        ...

    def stop(self) -> None: ...


class Platform(Protocol):
    viewer_url: str
    log_path: str
    paused: bool  # 直近の server_state() で読んだ一時停止の状態

    def server_state(self) -> ServerState: ...
    def set_paused(self, paused: bool) -> None: ...
    def launch_server(self) -> ServerProcess: ...
    def request_shutdown(self) -> None: ...
    def kill_server(self) -> None: ...
    def open_browser(self, url: str) -> None: ...


@dataclass(frozen=True)
class Status:
    """トレイの表示に使う状態"""

    phase: Literal["starting", "running", "stopping", "stopped", "failed"] = "starting"
    server: ServerState = "down"
    capture: Literal["off", "waiting", "streaming", "error"] = "off"
    paused: bool = False
    message: str = ""


class Controller:
    def __init__(
        self,
        platform: Platform,
        make_streamer: Callable[[Callable[[str, str], None]], Streamer],
        on_status: Callable[[Status], None] = lambda s: None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        capture_on_start: bool = True,
    ):
        self.platform = platform
        self.make_streamer = make_streamer
        self.on_status = on_status
        self.sleep = sleep
        self.clock = clock
        self.capture_on_start = capture_on_start
        self.status = Status()
        self.process: ServerProcess | None = None  # 自分で起動したときだけ
        self.reused = False
        self._lock = threading.RLock()
        self._quitting = threading.Event()
        self._streamer: Streamer | None = None
        self._streamer_thread: threading.Thread | None = None

    # --- 状態 ---
    def _update(self, **changes) -> None:
        with self._lock:
            new = replace(self.status, **changes)
            if new == self.status:
                return
            self.status = new
        self.on_status(new)

    def _on_capture(self, state: str, detail: str) -> None:
        if state in ("waiting", "streaming"):
            self._update(capture=state)
        elif state == "stopped":
            self._update(capture="off")

    # --- 起動 ---
    def run(self) -> None:
        """起動して、終了の指示があるまで状態を見張る（別スレッドで呼ぶ）"""
        if not self._start():
            return
        self.platform.open_browser(self.platform.viewer_url)
        self._update(phase="running")
        self._watch()

    def _start(self) -> bool:
        state = self.platform.server_state()
        if state == "down":
            self.process = self.platform.launch_server()
        else:
            self.reused = True  # start.bat などで起動済み
        if self.capture_on_start:
            self.set_capture(True)  # サーバーを待つ間も取り込みは始めておく（つながるまで再接続する）

        deadline = self.clock() + START_TIMEOUT_S
        while state == "down":
            if self._quitting.is_set():
                return False
            if self.process is not None and self.process.poll() is not None:
                return self._fail(
                    f"サーバーが起動直後に終了しました。ログを確認してください: {self.platform.log_path}"
                )
            if self.clock() > deadline:
                return self._fail(
                    f"サーバーが {START_TIMEOUT_S:.0f} 秒以内に応答しませんでした: {self.platform.log_path}"
                )
            self.sleep(POLL_S)
            state = self.platform.server_state()
        self._update(server=state)
        return True

    def _fail(self, message: str) -> bool:
        self._stop_capture()
        self._update(phase="failed", message=message)
        return False

    def _watch(self) -> None:
        while not self._quitting.is_set():
            state = self.platform.server_state()
            if state == "down" and not self._quitting.is_set():
                self._stop_capture()
                self._update(phase="failed", server="down", message="サーバーが停止しました")
                return
            self._update(server=state, paused=self.platform.paused)
            self.sleep(POLL_S)

    # --- 取り込み ---
    def set_capture(self, on: bool) -> None:
        if on:
            # スレッドは作って記録してから起動する（記録の前に止められても、止める側が終わりを待てるように）
            with self._lock:
                if self._streamer is not None:
                    return
                streamer = self._streamer = self.make_streamer(self._on_capture)
                thread = self._streamer_thread = self._streamer_runner(streamer)
            # 起動より先に状態を変える（すぐ失敗したときの "error" を "waiting" で上書きしないように）
            self._update(capture="waiting")
            thread.start()
        else:
            self._stop_capture()

    def _streamer_runner(self, streamer: Streamer) -> threading.Thread:
        # 別スレッドで run() を呼び、デバイスが見つからないなどの例外を状態に出す（起動は呼び出し側）
        def target() -> None:
            try:
                streamer.run()
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    if self._streamer is streamer:
                        self._streamer = self._streamer_thread = None
                self._update(capture="error", message=f"音声を取り込めません: {e}")

        return threading.Thread(target=target, name="capture-supervisor", daemon=True)

    def _stop_capture(self) -> None:
        with self._lock:
            streamer, thread = self._streamer, self._streamer_thread
            self._streamer = self._streamer_thread = None
        if streamer is not None:
            streamer.stop()
            # 起動前のスレッドは join できない（stop 済みなので起動してもすぐ終わる）
            if thread is not None and thread.ident is not None:
                thread.join(CAPTURE_JOIN_S)
        self._update(capture="off")

    # --- 一時停止 ---
    def set_paused(self, paused: bool) -> None:
        """字幕画面の「一時停止」と同じ（区切り待ちの途中の音声も捨てる）"""
        self.platform.set_paused(paused)
        self._update(paused=paused)  # 次の見張りを待たずに表示を変える

    # --- 終了 ---
    def quit(self) -> str:
        """全部止める。結果の説明を返す（トレイの通知用）"""
        self._quitting.set()
        self._update(phase="stopping")
        self._stop_capture()  # 先に入力を閉じると、サーバーは途中の区間を確定させる

        result = "停止しました"
        if self.platform.server_state() != "down" or self._alive():
            self.platform.request_shutdown()
            if not self._wait_stopped(SHUTDOWN_TIMEOUT_S):
                self.platform.kill_server()
                result = "サーバーが応答しないため強制終了しました"
                if not self._wait_stopped(KILL_TIMEOUT_S) and self.process is not None:
                    self.process.kill()  # 最後の手段: wsl.exe を止める
        self._update(phase="stopped", server="down", message=result)
        return result

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _wait_stopped(self, timeout: float) -> bool:
        deadline = self.clock() + timeout
        while self.clock() <= deadline:
            if self.platform.server_state() == "down" and not self._alive():
                return True
            self.sleep(0.2)
        return False
