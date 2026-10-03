"""WebSocket サーバーを起動するコマンドライン入口

python -m realtime_transcriber
python -m realtime_transcriber --speed 1.5                  # 1.5倍速で再生しているとき
python -m realtime_transcriber --final-model large-v3       # 確定用に高精度モデルを使う
python -m realtime_transcriber --glossary "東雲電機,汐留第二開発課"   # 固有名詞を教える
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .segmenter import Session
from .transcriber import WhisperTranscriber

IDLE_TIMEOUT_S = 0.8  # 音声パケットが途切れたら確定する（再生停止時など）


class Display:
    def __init__(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / f"{dt.datetime.now():%Y%m%d_%H%M%S}.txt"

    def show(self, kind: str, text: str) -> None:
        if kind == "partial":
            # 暫定結果は同じ行を上書き（長いと折り返して崩れるので末尾だけ）
            print(f"\r\x1b[2K\x1b[90m… {text[-35:]}\x1b[0m", end="", flush=True)
        else:
            ts = f"{dt.datetime.now():%H:%M:%S}"
            print(f"\r\x1b[2K[{ts}] {text}", flush=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {text}\n")


async def run(args: argparse.Namespace) -> None:
    glossary = [w.strip() for w in args.glossary.split(",") if w.strip()]
    transcriber = WhisperTranscriber(args.model, args.final_model, glossary)
    display = Display(Path(args.out_dir))

    async def handler(ws) -> None:
        print(f"\n[WS] クライアント接続: {ws.remote_address}")
        session = Session(transcriber, args.speed)
        try:
            while True:
                try:
                    session.add(await asyncio.wait_for(ws.recv(), timeout=IDLE_TIMEOUT_S))
                    force = False
                except TimeoutError:
                    if len(session.buf) == 0:
                        continue
                    force = True
                for kind, text in await asyncio.to_thread(session.step, force):
                    display.show(kind, text)
        except ConnectionClosed:
            for kind, text in await asyncio.to_thread(session.step, True):
                display.show(kind, text)
            print("\n[WS] クライアント切断")

    async with serve(handler, args.host, args.port, max_size=None):
        print(f"[WS] ws://{args.host}:{args.port} で待機中（保存先: {display.path}）")
        print(f"     再生速度 {args.speed}x / 用語集 {glossary or 'なし'}")
        print("     Windows 側で capture_client.py を起動してください。Ctrl+C で終了")
        await asyncio.Future()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--model", default="kotoba-tech/kotoba-whisper-v2.0-faster", help="暫定表示用（速さ重視）"
    )
    ap.add_argument("--final-model", default=None, help="確定用（精度重視）。例: large-v3")
    ap.add_argument("--speed", type=float, default=1.0, help="動画の再生速度（1.5, 2.0 など）")
    ap.add_argument("--glossary", default="", help="固有名詞をカンマ区切りで（例: 東雲電機,汐留第二開発課）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out-dir", default="transcripts")
    try:
        asyncio.run(run(ap.parse_args()))
    except KeyboardInterrupt:
        print("\n終了しました")
