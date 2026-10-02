"""transcribe_server.py  (WSL 側で動かす)

Windows のキャプチャクライアントから 16kHz / mono / int16 の PCM を WebSocket で受け取り、
faster-whisper でリアルタイムに文字起こしする。

  python server/transcribe_server.py
  python server/transcribe_server.py --speed 1.5                  # 1.5倍速で再生しているとき
  python server/transcribe_server.py --final-model large-v3       # 確定用に高精度モデルを使う
  python server/transcribe_server.py --glossary "東雲電機,汐留第二開発課"   # 固有名詞を教える

仕組み:
  - 受け取った音声をバッファに貯め、Silero VAD で発話区間を判定する
  - 発話中は 1 秒ごとに軽量設定で暫定結果 (partial) を表示
  - 無音 / 長さの上限 / 音声の途切れ で区間を確定 (final) し、高精度設定で認識して保存
  - 長さの上限に達したときは単語の途中で切らず、区間内で一番長い「間」で切る
  - 倍速再生の音声は WSOLA で通常速度に戻してから認識する
  - 直前の確定文と用語集をプロンプトとして渡し、文脈と固有名詞をつなげる
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
from pathlib import Path

import numpy as np
from audiotsm import wsola
from audiotsm.io.array import ArrayReader, ArrayWriter
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions, get_speech_timestamps
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

SR = 16000
PARTIAL_INTERVAL_S = 1.0   # 暫定結果を出す間隔（実時間）
MIN_SILENCE_S = 0.6        # 通常速度でこれだけ無音が続いたら区切り
MAX_SEGMENT_S = 20.0       # 通常速度に換算した 1 区間の上限（Whisper の窓は 30 秒）
IDLE_TIMEOUT_S = 0.8       # 音声パケットが途切れたら確定する（再生停止時など）
CONTEXT_CHARS = 80         # 次の認識に渡す直前の文脈の長さ

# Whisper が無音や BGM でよく出す「幻聴」フレーズ
HALLUCINATIONS = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "チャンネル登録よろしくお願いします",
    "チャンネル登録よろしくお願いします。",
}
REPEAT_RE = re.compile(r"(.{1,8}?)\1{3,}")  # 同じ断片が4回以上続く暴走を1回に畳む


def time_stretch(audio: np.ndarray, speed: float) -> np.ndarray:
    """speed 倍速の音声を通常速度に戻す（ピッチは保ったまま）"""
    if abs(speed - 1.0) < 1e-3:
        return audio
    writer = ArrayWriter(channels=1)
    wsola(channels=1, speed=1.0 / speed).run(ArrayReader(audio.reshape(1, -1)), writer)
    return writer.data[0].astype(np.float32)


class Transcriber:
    def __init__(self, partial_model: str, final_model: str | None, glossary: list[str]):
        print(f"[MODEL] 暫定用: {partial_model} を読み込み中...")
        self.partial = WhisperModel(partial_model, device="cuda", compute_type="float16")
        if final_model and final_model != partial_model:
            print(f"[MODEL] 確定用: {final_model} を読み込み中...")
            self.final = WhisperModel(final_model, device="cuda", compute_type="float16")
        else:
            self.final = self.partial
        self.hotwords = " ".join(glossary) or None
        self.glossary_prompt = "、".join(glossary)
        for final in (False, True):  # ウォームアップ
            self.transcribe(np.zeros(SR, dtype=np.float32), "", final=final)
        print("[MODEL] 準備完了")

    def transcribe(self, audio: np.ndarray, context: str, final: bool) -> str:
        prompt = "。".join(p for p in (self.glossary_prompt, context) if p) or None
        segments, _ = (self.final if final else self.partial).transcribe(
            audio,
            language="ja",
            beam_size=5 if final else 1,
            temperature=(0.0, 0.2, 0.4) if final else 0.0,
            condition_on_previous_text=False,
            initial_prompt=prompt,
            hotwords=self.hotwords,
            without_timestamps=True,
            vad_filter=False,  # VAD は自前でかけている
        )
        text = "".join(s.text for s in segments if s.no_speech_prob < 0.6).strip()
        text = REPEAT_RE.sub(r"\1", text)
        return "" if text in HALLUCINATIONS else text


class Session:
    """1 本の音声ストリームの状態（バッファ・文脈）を持つ"""

    def __init__(self, transcriber: Transcriber, speed: float):
        self.tr = transcriber
        self.speed = speed
        self.buf = np.zeros(0, dtype=np.float32)
        self.since_partial = 0
        self.context = ""
        # 倍速だと無音も短くなるので、判定の時間を速度で割る
        self.min_silence = int(MIN_SILENCE_S / speed * SR)
        self.max_len = int(MAX_SEGMENT_S / speed * SR)
        self.vad = VadOptions(
            min_silence_duration_ms=int(MIN_SILENCE_S / speed * 1000),
            min_speech_duration_ms=100,
            speech_pad_ms=int(150 / speed),
        )
        self.vad_fine = VadOptions(
            min_silence_duration_ms=int(120 / speed),
            min_speech_duration_ms=100,
            speech_pad_ms=int(60 / speed),
        )

    def add(self, pcm: bytes) -> None:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self.buf = np.concatenate([self.buf, x])
        self.since_partial += len(x)

    def _best_gap(self, n: int) -> int:
        """上限に達したとき、単語の途中で切らないよう区間内で一番長い「間」の中央を返す"""
        fine = get_speech_timestamps(self.buf, self.vad_fine)
        gaps = [
            (fine[i + 1]["start"] - fine[i]["end"], (fine[i]["end"] + fine[i + 1]["start"]) // 2)
            for i in range(len(fine) - 1)
            if fine[i]["end"] > n * 0.3  # 短すぎる区間にならないよう後半から選ぶ
        ]
        return max(gaps)[1] if gaps else n

    def _run(self, audio: np.ndarray, final: bool) -> str:
        return self.tr.transcribe(time_stretch(audio, self.speed), self.context, final)

    def step(self, force: bool = False) -> list[tuple[str, str]]:
        n = len(self.buf)
        if n < int(0.3 * SR):
            if force:
                self.buf = self.buf[:0]
            return []

        speech = get_speech_timestamps(self.buf, self.vad)
        if not speech:
            # 発話なし: 古い音は捨てる（発話の頭を切らないよう最後の 0.5 秒だけ残す）
            self.buf = self.buf[:0] if force else self.buf[-int(0.5 * SR):]
            self.since_partial = 0
            return []

        last_end = speech[-1]["end"]
        cut = None
        if force:
            cut = n
        elif n - last_end >= self.min_silence:
            cut = last_end
        elif n >= self.max_len:
            cut = self._best_gap(n)

        if cut is not None:
            # 先頭は切り詰めない（短い語頭が VAD で落ちて「抜け」になるのを防ぐ）
            text = self._run(self.buf[:cut], final=True)
            self.buf = self.buf[cut:]
            self.since_partial = 0
            if text:
                self.context = text[-CONTEXT_CHARS:]
                return [("final", text)]
            return []

        if self.since_partial >= int(PARTIAL_INTERVAL_S * SR):
            self.since_partial = 0
            text = self._run(self.buf, final=False)
            if text:
                return [("partial", text)]
        return []


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
    transcriber = Transcriber(args.model, args.final_model, glossary)
    display = Display(Path(args.out_dir))

    async def handler(ws) -> None:
        print(f"\n[WS] クライアント接続: {ws.remote_address}")
        session = Session(transcriber, args.speed)
        try:
            while True:
                try:
                    session.add(await asyncio.wait_for(ws.recv(), timeout=IDLE_TIMEOUT_S))
                    force = False
                except asyncio.TimeoutError:
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
    ap.add_argument("--model", default="kotoba-tech/kotoba-whisper-v2.0-faster", help="暫定表示用（速さ重視）")
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


if __name__ == "__main__":
    main()
