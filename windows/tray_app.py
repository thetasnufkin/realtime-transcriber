# /// script
# requires-python = "==3.12.*"
# dependencies = ["pystray", "pillow", "pyaudiowpatch", "numpy", "soxr", "websockets"]
# ///
"""tray_app.py（Windows 側）タスクトレイ常駐アプリ

デスクトップのショートカット（windows/setup.bat で作る）から uvw で起動する。コンソール窓は出ない。
  1. WSL のサーバーを起動（動いていれば使い回す）
  2. 再生音の取り込みを開始
  3. サーバーが応答したらブラウザで字幕画面を開く
  4. トレイの「終了」で全部止める

  uvw run \\\\wsl.localhost\\Ubuntu\\home\\...\\windows\\tray_app.py
確認用のオプション: --no-browser（ブラウザを開かない）/ --no-capture（取り込みを始めない）/
  --quit-after 秒（その秒数で終了する）/ --port（config.toml の代わり）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from capture import LoopbackStreamer  # noqa: E402
from controller import Controller, Status  # noqa: E402
from tray_view import APP_NAME, describe, draw_icon, should_notify, tooltip, write_ico  # noqa: E402
from winplatform import WindowsPlatform, acquire_single_instance, read_port  # noqa: E402

MUTEX_NAME = "Local\\realtime-transcriber-tray"
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "realtime-transcriber"
log = logging.getLogger("tray")


def setup_logging() -> None:
    """コンソールがないので、例外も含めてログファイルに残す"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / "tray.log",
        filemode="w",
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sys.excepthook = lambda *exc: log.critical("例外", exc_info=exc)
    threading.excepthook = lambda a: log.critical(
        f"例外（{a.thread.name}）", exc_info=(a.exc_type, a.exc_value, a.exc_traceback)
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-capture", action="store_true")
    ap.add_argument("--quit-after", type=float)
    ap.add_argument("--port", type=int)
    ap.add_argument("--write-icon", help="ショートカット用の .ico を書き出して終わる（setup.ps1 が使う）")
    args = ap.parse_args()
    if args.write_icon:
        write_ico(args.write_icon)
        return

    setup_logging()
    project = HERE.parent
    mutex = acquire_single_instance(MUTEX_NAME)  # noqa: F841  終了まで保持する
    if mutex is None:
        log.info("すでに起動しているので、字幕画面を開いて終わります")
        webbrowser.open(f"http://localhost:{args.port or read_port(project)}/")
        return
    run_tray(WindowsPlatform(project, LOG_DIR, port=args.port), args)


def run_tray(platform: WindowsPlatform, args: argparse.Namespace) -> None:
    import pystray  # Windows のトレイ。WSL のテストでは読み込まない

    if args.no_browser:
        platform.open_browser = lambda url: log.info(f"（--no-browser）{url} は開きません")
    ingest_url = f"ws://127.0.0.1:{platform.port}/ingest"
    icon: pystray.Icon | None = None
    shown = Status()

    def on_status(status: Status) -> None:
        nonlocal shown
        color, text = describe(status)
        log.info(f"状態: {status}")
        if icon is not None:
            icon.icon = draw_icon(color)
            icon.title = tooltip(text)
            icon.update_menu()
            if message := should_notify(shown, status):
                icon.notify(message, APP_NAME)
        shown = status

    controller = Controller(
        platform,
        lambda on_state: LoopbackStreamer(ingest_url, on_state=on_state),
        on_status=on_status,
        capture_on_start=not args.no_capture,
    )

    def open_viewer() -> None:
        webbrowser.open(platform.viewer_url)  # --no-browser でもメニューからは開く

    def toggle_capture() -> None:
        controller.set_capture(not capturing())

    def capturing() -> bool:
        return controller.status.capture in ("waiting", "streaming")

    def toggle_pause() -> None:
        controller.set_paused(not controller.status.paused)

    def can_pause() -> bool:
        s = controller.status
        return s.phase == "running" and s.server in ("ready", "switching")

    def open_log() -> None:
        if Path(platform.log_path).exists():
            os.startfile(platform.log_path)  # noqa: S606  既定のテキストエディタで開く

    def quit_app() -> None:
        # 終了には最大 25 秒ほどかかるので、メニューの処理を止めないよう別スレッドで行う
        def work() -> None:
            log.info(f"終了: {controller.quit()}")
            if icon is not None:
                icon.stop()

        threading.Thread(target=work, name="quit", daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("字幕画面を開く", lambda: open_viewer(), default=True),  # アイコンのクリックでも開く
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("音声を取り込む", lambda: toggle_capture(), checked=lambda item: capturing()),
        pystray.MenuItem(
            lambda item: "再開" if controller.status.paused else "一時停止",
            lambda: toggle_pause(),
            enabled=lambda item: can_pause(),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("ログを開く", lambda: open_log()),
        pystray.MenuItem("終了", lambda: quit_app()),
    )
    color, text = describe(controller.status)
    icon = pystray.Icon("realtime-transcriber", draw_icon(color), tooltip(text), menu=menu)

    def setup(icon: pystray.Icon) -> None:
        icon.visible = True
        threading.Thread(target=controller.run, name="controller", daemon=True).start()
        if args.quit_after:
            threading.Timer(args.quit_after, quit_app).start()

    log.info(f"起動: プロジェクト {platform.distro}:{platform.project} / ポート {platform.port}")
    icon.run(setup=setup)
    log.info("トレイを閉じました")


if __name__ == "__main__":
    main()
