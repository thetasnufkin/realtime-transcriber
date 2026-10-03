# /// script
# requires-python = "==3.12.*"
# dependencies = ["pyaudiowpatch", "numpy", "soxr", "websockets"]
# ///
"""capture_client.py  (Windows 側で動かす。コンソールに状態を表示する版)

再生中の音を取り込んでサーバーへ送る。処理の本体は windows/capture.py。
タスクトレイから使う場合は windows/tray_app.py。

  uv run capture_client.py
  uv run capture_client.py --url ws://localhost:9000/ingest   # ポートを変えた場合
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "windows"))

from capture import LoopbackStreamer  # noqa: E402


def show(state: str, detail: str) -> None:
    if state == "device":
        print(f"[CAPTURE] {detail}")
    elif state == "streaming":
        print(f"\n[WS] {detail} に接続しました。音声を再生してください（Ctrl+C で終了）")
    elif state == "waiting":
        print(f"[WS] サーバー待ち... ({detail})", end="\r", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8765/ingest")
    args = ap.parse_args()
    try:
        LoopbackStreamer(args.url, on_state=show).run()
    except KeyboardInterrupt:
        print("\n終了しました")


if __name__ == "__main__":
    main()
