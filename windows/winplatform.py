"""winplatform.py（Windows 側）Controller が使う OS とのやりとり

- WSL のディストリビューション名とプロジェクトの場所は、このファイル自身のパス
  （\\\\wsl.localhost\\Ubuntu\\home\\...）から割り出す。設定を二重に持たない
- サーバーは wsl.exe を CREATE_NO_WINDOW で起動し、出力はログファイルに書く
- HTTP は 127.0.0.1 宛て（PowerShell などで localhost が IPv6 を先に試して遅くなるのを避ける）
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tomllib
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path, PureWindowsPath

from controller import ServerProcess, ServerState

DEFAULT_PORT = 8765
HTTP_TIMEOUT_S = 2.0
KILL_SCRIPT_TIMEOUT_S = 20.0
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)  # WSL のテストでも import できるように
ERROR_ALREADY_EXISTS = 183
WSL_UNC_RE = re.compile(r"^\\\\wsl(?:\.localhost|\$)\\([^\\]+)(\\.*)?$", re.IGNORECASE)


def wsl_location(windows_path: str) -> tuple[str, str]:
    """\\\\wsl.localhost\\Ubuntu\\home\\me\\proj → ("Ubuntu", "/home/me/proj")"""
    m = WSL_UNC_RE.match(str(PureWindowsPath(windows_path)))
    if not m:
        raise ValueError(
            f"WSL の中のパスではありません: {windows_path}（\\\\wsl.localhost\\... から起動してください）"
        )
    return m.group(1), (m.group(2) or "\\").replace("\\", "/")


def read_port(project_dir: Path) -> int:
    """config.toml の port（なければ既定値）。トレイとサーバーで同じポートを使う"""
    try:
        with (project_dir / "config.toml").open("rb") as f:
            return int(tomllib.load(f).get("port", DEFAULT_PORT))
    except FileNotFoundError:
        return DEFAULT_PORT


def acquire_single_instance(name: str) -> object | None:
    """名前付きミューテックスで二重起動を防ぐ。すでに起動していれば None。
    戻り値はプロセスが終わるまで保持しておく（解放するとミューテックスも消える）"""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


class WindowsPlatform:
    def __init__(self, project_dir: Path, log_dir: Path, port: int | None = None):
        self.distro, self.project = wsl_location(str(project_dir))
        self.port = port if port is not None else read_port(project_dir)
        self.viewer_url = f"http://localhost:{self.port}/"
        self._base = f"http://127.0.0.1:{self.port}"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = str(log_dir / "server.log")

    def _wsl(self, command: str) -> list[str]:
        return [
            "wsl.exe",
            "-d",
            self.distro,
            "--",
            "bash",
            "-lc",
            f"cd {shlex.quote(self.project)} && {command}",
        ]

    def server_command(self) -> list[str]:
        # 出力をすぐログに書くようバッファしない
        return self._wsl(f"PYTHONUNBUFFERED=1 exec ./scripts/start_server.sh --port {self.port}")

    def server_state(self) -> ServerState:
        try:
            with urllib.request.urlopen(f"{self._base}/api/health", timeout=HTTP_TIMEOUT_S) as r:
                return json.load(r)["status"]
        except urllib.error.HTTPError as e:  # 503: 読み込み中・読み込み失敗
            try:
                return json.load(e)["status"]
            except (ValueError, KeyError):
                return "error"
        except (OSError, ValueError):  # 接続できない・タイムアウト
            return "down"

    def launch_server(self) -> ServerProcess:
        log = Path(self.log_path)
        if log.exists():
            log.replace(log.with_suffix(".log.1"))  # 前回のログを 1 世代だけ残す
        with log.open("wb") as out:
            return subprocess.Popen(
                self.server_command(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )

    def request_shutdown(self) -> None:
        req = urllib.request.Request(f"{self._base}/api/shutdown", method="POST")
        try:
            urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S).close()
        except OSError:
            pass  # すでに止まっている。止まったかどうかは呼び出し側が確かめる

    def kill_server(self) -> None:
        with open(self.log_path, "ab") as out:
            try:
                subprocess.run(
                    self._wsl("./scripts/stop_server.sh"),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=KILL_SCRIPT_TIMEOUT_S,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                pass  # 止まったかどうかは呼び出し側が確かめる

    def open_browser(self, url: str) -> None:
        webbrowser.open(url)
