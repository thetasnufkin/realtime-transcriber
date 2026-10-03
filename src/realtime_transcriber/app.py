"""FastAPI アプリ。音声の受け取り・字幕の配信・ビューアを 1 つのポートで提供する

GET  /              字幕ビューア
WS   /ingest        16kHz mono int16 PCM を受け取る（WS / も旧クライアント用の別名）
WS   /captions      暫定 / 確定テキストを JSON で配信
GET  /api/health    モデルの準備ができていれば 200、読み込み中・失敗なら 503
POST /api/shutdown  サーバーを終了する（途中の区間は確定させてから止まる）
"""

from __future__ import annotations

import asyncio
import itertools
import traceback
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper.vad import get_speech_timestamps

from .config import Config
from .hub import Hub
from .segmenter import Session, Vad
from .transcriber import Transcriber

IDLE_TIMEOUT_S = 0.8  # 音声パケットが途切れたら確定する（再生停止時など）
STATIC_DIR = Path(__file__).parent / "static"
VIEWER_CONFIG_KEYS = ("model", "final_model", "speed", "glossary")


def create_app(
    cfg: Config,
    load_transcriber: Callable[[Config], Transcriber],
    vad: Vad = get_speech_timestamps,
    echo: bool = True,
) -> FastAPI:
    """load_transcriber は起動後に別スレッドで呼ぶ（読み込み中もビューアは開ける）"""
    hub = Hub(Path(cfg.out_dir), echo=echo)
    gpu_lock = asyncio.Lock()
    source_ids = itertools.count(1)
    tr: Transcriber | None = None
    ingest_clients = 0

    async def load() -> None:
        nonlocal tr
        await hub.set_status(model_state="loading")
        try:
            tr = await asyncio.to_thread(load_transcriber, cfg)
        except Exception as e:
            traceback.print_exc()
            await hub.set_status(model_state="error", error=f"{type(e).__name__}: {e}")
            return
        await hub.set_status(model_state="ready")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub.status.update(
            model_state="loading",
            ingest_clients=0,
            config={k: v for k, v in asdict(cfg).items() if k in VIEWER_CONFIG_KEYS},
        )
        task = asyncio.create_task(load())
        yield
        task.cancel()

    app = FastAPI(title="realtime-transcriber", lifespan=lifespan)
    app.state.hub = hub
    app.state.request_shutdown = None  # cli.py が uvicorn を止める関数を入れる
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def viewer() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> JSONResponse:
        state = hub.status["model_state"]
        return JSONResponse({"status": state}, status_code=200 if state == "ready" else 503)

    @app.post("/api/shutdown")
    async def shutdown() -> JSONResponse:
        if app.state.request_shutdown is None:
            return JSONResponse({"status": "unsupported"}, status_code=501)
        app.state.request_shutdown()
        return JSONResponse({"status": "shutting_down"}, status_code=202)

    @app.websocket("/captions")
    async def captions(ws: WebSocket) -> None:
        await ws.accept()
        await hub.add_viewer(ws)
        try:
            while True:  # 閲覧者からは何も来ない。切断の検出だけ
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.remove_viewer(ws)

    @app.websocket("/ingest")
    @app.websocket("/")
    async def ingest(ws: WebSocket) -> None:
        nonlocal ingest_clients
        if tr is None:
            await ws.close(code=1013)  # モデルの準備前。クライアントは再接続してくる
            return
        await ws.accept()
        source = next(source_ids)
        session = Session(tr, cfg.speed, vad)
        offset = hub.elapsed()
        chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def reader() -> None:
            # 認識中も受信を止めないよう、受け取りは別タスクにする
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes"):
                        chunks.put_nowait(msg["bytes"])
            finally:
                chunks.put_nowait(None)

        async def process(force: bool) -> None:
            async with gpu_lock:
                events = await asyncio.to_thread(session.step, force)
            for event in events:
                await hub.publish(source, event, offset)

        reader_task = asyncio.create_task(reader())
        ingest_clients += 1
        await hub.set_status(ingest_clients=ingest_clients)
        print(f"\n[WS] 音声の入力が接続: {ws.client} (#{source})")
        try:
            closed = False
            while not closed:
                try:
                    chunk = await asyncio.wait_for(chunks.get(), timeout=IDLE_TIMEOUT_S)
                except TimeoutError:
                    if len(session.buf):
                        await process(force=True)
                    continue
                # 認識が追いつかず溜まった分はまとめて渡す
                while True:
                    if chunk is None:
                        closed = True
                        break
                    session.add(chunk)
                    try:
                        chunk = chunks.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await process(force=closed)
        finally:
            reader_task.cancel()
            ingest_clients -= 1  # キャンセルされても数がずれないよう、await より先に減らす
            await hub.clear(source)
            await hub.set_status(ingest_clients=ingest_clients)
            print(f"\n[WS] 音声の入力が切断 (#{source})")

    return app
