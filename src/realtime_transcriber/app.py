"""FastAPI アプリ。音声の受け取り・字幕の配信・ビューアを 1 つのポートで提供する

GET  /              字幕ビューア
WS   /ingest        16kHz mono int16 PCM を受け取る（WS / も旧クライアント用の別名）
WS   /captions      暫定 / 確定テキストと状態を JSON で配信
GET  /api/health    認識できる状態なら 200、読み込み中・失敗なら 503（一時停止中かも返す）
GET  /api/config    ブラウザから変えられる設定
PUT  /api/config    設定を変えて config.toml に保存（モデルは裏で読み込んでから切り替える）
POST /api/pause     一時停止（区切り待ちの途中の音声も捨てる）
POST /api/resume    再開
GET  /api/transcript.txt, .srt  確定文のダウンロード
POST /api/shutdown  サーバーを終了する（途中の区間は確定させてから止まる）
"""

from __future__ import annotations

import asyncio
import itertools
import traceback
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper.vad import get_speech_timestamps

from .config import EDITABLE_KEYS, Config, save_config
from .export import to_srt, to_txt
from .hub import Hub
from .segmenter import Session, Vad
from .transcriber import Transcriber

IDLE_TIMEOUT_S = 0.8  # 音声パケットが途切れたら確定する（再生停止時など）
STATIC_DIR = Path(__file__).parent / "static"
MODEL_KEYS = ("model", "final_model")
READY_STATES = ("ready", "switching")  # switching: 新しいモデルの読み込み中（今のモデルで認識を続ける）


@dataclass
class Ingest:
    """1 本の音声入力の状態"""

    source: int
    session: Session | None = None  # モデルの準備ができてから作る
    offset: float = 0.0  # このストリームが始まった時点の hub.elapsed()


def create_app(
    cfg: Config,
    load_transcriber: Callable[[Config], Transcriber],
    vad: Vad = get_speech_timestamps,
    echo: bool = True,
    config_path: Path | None = None,
    cli_overrides: Collection[str] = (),
) -> FastAPI:
    """load_transcriber は起動後に別スレッドで呼ぶ（読み込み中もビューアは開ける）。
    config_path を渡すと、ブラウザで変えた設定をそこに保存する"""
    hub = Hub(Path(cfg.out_dir), echo=echo)
    gpu_lock = asyncio.Lock()  # 認識・モデルの入れ替え・一時停止のリセットを直列にする
    source_ids = itertools.count(1)
    ingests: dict[int, Ingest] = {}
    tr: Transcriber | None = None
    paused = False

    def editable(c: Config) -> dict[str, Any]:
        return {k: getattr(c, k) for k in EDITABLE_KEYS}

    async def load() -> None:
        nonlocal tr
        try:
            tr = await asyncio.to_thread(load_transcriber, cfg)
        except Exception as e:
            traceback.print_exc()
            await hub.set_status(model_state="error", error=f"{type(e).__name__}: {e}")
            return
        await hub.set_status(model_state="ready")

    async def switch_models(target: Config, changed: Collection[str]) -> None:
        """新しいモデルを読み込んでから入れ替える。失敗したら今のモデルのまま。
        changed: 保存する項目（ブラウザで変えたものだけ。CLI で指定した値をファイルに書かない）"""
        nonlocal tr, cfg
        await hub.set_status(model_state="switching", model_error=None)
        try:
            new_tr = await asyncio.to_thread(load_transcriber, target)
        except Exception as e:
            traceback.print_exc()
            await hub.set_status(model_state="ready", model_error=f"{type(e).__name__}: {e}")
            return
        async with gpu_lock:
            new_tr.set_glossary(cfg.glossary)  # 読み込み中に用語集が変わっていても最新を使う
            tr = new_tr
            cfg = replace(cfg, model=target.model, final_model=target.final_model)
            for ing in ingests.values():
                if ing.session is not None:
                    ing.session.tr = new_tr
        if config_path is not None:
            save_config(config_path, {k: getattr(cfg, k) for k in changed})
        print(f"\n[MODEL] 切り替え完了: 暫定 {cfg.model} / 確定 {cfg.final_model or '暫定と同じ'}")
        await hub.set_status(model_state="ready", config=editable(cfg))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub.status.update(
            model_state="loading",
            ingest_clients=0,
            paused=False,
            config=editable(cfg),
            cli_overrides=sorted(set(cli_overrides) & set(EDITABLE_KEYS)),
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
        body = {"status": state, "paused": hub.status["paused"]}  # paused はトレイの表示用
        return JSONResponse(body, status_code=200 if state in READY_STATES else 503)

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return editable(cfg)

    @app.put("/api/config")
    async def put_config(body: dict[str, Any] = Body(...)) -> JSONResponse:  # noqa: B008
        nonlocal cfg
        if unknown := sorted(set(body) - set(EDITABLE_KEYS)):
            return JSONResponse({"error": f"変更できない設定です: {', '.join(unknown)}"}, status_code=422)
        try:
            target = replace(cfg, **body)  # Config の検証（型・範囲）を通す
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        changed = {k: v for k, v in body.items() if getattr(cfg, k) != getattr(target, k)}
        model_changed = any(k in changed for k in MODEL_KEYS)
        if model_changed and hub.status["model_state"] != "ready":
            return JSONResponse(
                {"error": "モデルの読み込み中は変更できません。終わってから保存し直してください"},
                status_code=409,
            )

        # 速度と用語集はすぐ反映する。モデルは読み込みが終わってから切り替える
        async with gpu_lock:
            cfg = replace(target, model=cfg.model, final_model=cfg.final_model)
            if tr is not None:
                tr.set_glossary(cfg.glossary)
            for ing in ingests.values():
                if ing.session is not None:
                    ing.session.set_speed(cfg.speed)
        if config_path is not None and (now := {k: v for k, v in changed.items() if k not in MODEL_KEYS}):
            save_config(config_path, now)
        if model_changed:
            asyncio.create_task(switch_models(target, [k for k in MODEL_KEYS if k in changed]))
        await hub.set_status(config=editable(cfg))
        return JSONResponse({"config": editable(cfg), "switching_models": model_changed})

    @app.post("/api/pause")
    async def pause() -> dict[str, bool]:
        nonlocal paused
        paused = True
        async with gpu_lock:  # 認識中の区間の結果を出し終えてから捨てる
            for ing in ingests.values():
                if ing.session is not None:
                    ing.session.reset()
                await hub.clear(ing.source)
        await hub.set_status(paused=True)
        return {"paused": True}

    @app.post("/api/resume")
    async def resume() -> dict[str, bool]:
        nonlocal paused
        paused = False
        await hub.set_status(paused=False)
        return {"paused": False}

    @app.get("/api/transcript.{fmt}")
    async def transcript(fmt: str) -> PlainTextResponse:
        if fmt not in ("txt", "srt"):
            return PlainTextResponse("txt か srt を指定してください", status_code=404)
        body = to_txt(hub.captions) if fmt == "txt" else to_srt(hub.captions)
        # ファイル名はサーバー起動時の transcripts/ のファイルと揃える
        filename = f"{hub.path.stem}.{fmt}"
        return PlainTextResponse(
            body,
            media_type="application/x-subrip" if fmt == "srt" else "text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

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
        # モデルの準備前でも接続は受け入れる（断ると旧 capture_client は再接続せずに落ちる）
        await ws.accept()
        ing = Ingest(source=next(source_ids))
        chunks: asyncio.Queue[tuple[bytes, bool] | None] = asyncio.Queue()  # (音声, 停止中に届いたか)

        async def reader() -> None:
            # 認識中も受信を止めないよう、受け取りは別タスクにする
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    # モデルの準備前に届いた音声は、認識できないので捨てる
                    if msg.get("bytes") and tr is not None:
                        chunks.put_nowait((msg["bytes"], paused))
            finally:
                chunks.put_nowait(None)

        async def process(force: bool) -> None:
            assert ing.session is not None
            # 結果の配信までロックの中で行う（一時停止の直後に暫定表示が出ないように）
            async with gpu_lock:
                for event in await asyncio.to_thread(ing.session.step, force):
                    await hub.publish(ing.source, event, ing.offset)

        def add(chunk: bytes, arrived_paused: bool) -> None:
            if ing.session is None:
                assert tr is not None  # reader は準備後の音声だけを渡す
                ing.session = Session(tr, cfg.speed, vad)
                ing.offset = hub.elapsed()
            # 届いた時点か今のどちらかで停止中なら捨てる（再開の直前に届いた停止中の音声も混ぜない）
            if arrived_paused or paused:
                ing.session.skip(chunk)
            else:
                ing.session.add(chunk)

        reader_task = asyncio.create_task(reader())
        ingests[ing.source] = ing
        await hub.set_status(ingest_clients=len(ingests))
        print(f"\n[WS] 音声の入力が接続: {ws.client} (#{ing.source})")
        try:
            closed = False
            while not closed:
                try:
                    chunk = await asyncio.wait_for(chunks.get(), timeout=IDLE_TIMEOUT_S)
                except TimeoutError:
                    if ing.session is not None and len(ing.session.buf):
                        await process(force=True)
                    continue
                # 認識が追いつかず溜まった分はまとめて渡す
                while True:
                    if chunk is None:
                        closed = True
                        break
                    add(*chunk)
                    try:
                        chunk = chunks.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                if ing.session is not None:
                    await process(force=closed)
        finally:
            reader_task.cancel()
            del ingests[ing.source]  # キャンセルされても数がずれないよう、await より先に消す
            await hub.clear(ing.source)
            await hub.set_status(ingest_clients=len(ingests))
            print(f"\n[WS] 音声の入力が切断 (#{ing.source})")

    return app
