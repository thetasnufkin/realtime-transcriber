"""サーバーを起動するコマンドライン入口

python -m realtime_transcriber                              # config.toml（なければ既定値）で起動
python -m realtime_transcriber --speed 1.5                  # 1.5倍速で再生しているとき
python -m realtime_transcriber --final-model large-v3       # 確定用に高精度モデルを使う
python -m realtime_transcriber --glossary "東雲電機,汐留第二開発課"   # 固有名詞を教える
"""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .config import Config, add_cli_args, config_from_args, overrides_from_args
from .transcriber import Transcriber, WhisperTranscriber

GRACEFUL_SHUTDOWN_S = 10  # 終了時に途中の区間を確定させる猶予


def load_whisper(cfg: Config) -> Transcriber:
    return WhisperTranscriber(cfg.model, cfg.final_model or None, cfg.glossary)


def main() -> None:
    ap = argparse.ArgumentParser()
    add_cli_args(ap)
    args = ap.parse_args()
    try:
        cfg = config_from_args(args)
    except ValueError as e:
        ap.error(str(e))
    print(f"[CONFIG] {args.config if args.config.exists() else '既定値'} を使用")

    app = create_app(
        cfg, load_whisper, config_path=args.config, cli_overrides=overrides_from_args(args).keys()
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=cfg.host,
            port=cfg.port,
            log_level="warning",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        )
    )
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)

    url = f"http://localhost:{cfg.port}/"
    print(f"[HTTP] {url} で待機中（保存先: {app.state.hub.path}）")
    print(f"       再生速度 {cfg.speed}x / 用語集 {cfg.glossary or 'なし'}")
    print(f"       ブラウザで {url} を開き、Windows 側で capture_client.py を起動してください。Ctrl+C で終了")
    server.run()
    print("\n終了しました")
