"""文字起こし結果を閲覧者（/captions）に配信し、確定文の履歴を持つ"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from .segmenter import Event


class Viewer(Protocol):
    async def send_json(self, data: Any) -> None: ...


@dataclass(frozen=True)
class Caption:
    id: int
    text: str
    start: float  # 最初の音声を受け取ってからの秒数
    end: float
    at: str  # 確定した時刻（HH:MM:SS）


class Hub:
    def __init__(self, out_dir: Path, echo: bool = True):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / f"{dt.datetime.now():%Y%m%d_%H%M%S}.txt"
        self.echo = echo
        # 端末なら暫定結果を同じ行に上書き表示する。ログファイルへの出力（トレイから起動）では
        # 上書きや色の制御文字が読みにくいので、確定文だけを普通の行で書く
        self.live = echo and sys.stdout.isatty()
        self.captions: list[Caption] = []
        self.viewers: set[Viewer] = set()
        self.status: dict[str, Any] = {}
        self._t0: float | None = None

    def elapsed(self) -> float:
        """最初の音声入力からの秒数。字幕の時刻（SRT）の基準"""
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        return now - self._t0

    async def publish(self, source: int, event: Event, offset: float) -> None:
        """offset: そのストリームが始まった時点の elapsed()"""
        if event.kind == "partial":
            if self.live:
                # 暫定結果は同じ行を上書き（長いと折り返して崩れるので末尾だけ）
                print(f"\r\x1b[2K\x1b[90m… {event.text[-35:]}\x1b[0m", end="", flush=True)
            await self._broadcast({"type": "partial", "source": source, "text": event.text})
            return
        if event.kind == "clear":
            await self.clear(source)
            return

        cap = Caption(
            id=len(self.captions) + 1,
            text=event.text,
            start=round(offset + event.start, 3),
            end=round(offset + event.end, 3),
            at=f"{dt.datetime.now():%H:%M:%S}",
        )
        self.captions.append(cap)
        if self.echo:
            print(f"\r\x1b[2K[{cap.at}] {cap.text}" if self.live else f"[{cap.at}] {cap.text}", flush=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"[{cap.at}] {cap.text}\n")
        await self._broadcast({"type": "final", "source": source, **asdict(cap)})

    async def clear(self, source: int) -> None:
        """その入力の暫定表示を消す（確定文が空だった・入力が切断された）"""
        if self.live:
            print("\r\x1b[2K", end="", flush=True)
        await self._broadcast({"type": "clear", "source": source})

    async def set_status(self, **status: Any) -> None:
        self.status.update(status)
        await self._broadcast({"type": "status", **self.status})

    async def add_viewer(self, viewer: Viewer) -> None:
        """途中から開いた画面にも、それまでの確定文と現在の状態を送る"""
        await viewer.send_json({"type": "history", "captions": [asdict(c) for c in self.captions]})
        await viewer.send_json({"type": "status", **self.status})
        self.viewers.add(viewer)

    def remove_viewer(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        viewers = list(self.viewers)
        results = await asyncio.gather(*(v.send_json(msg) for v in viewers), return_exceptions=True)
        for v, r in zip(viewers, results, strict=True):
            if isinstance(r, Exception):  # 閉じたタブなど
                self.viewers.discard(v)
