#!/usr/bin/env bash
# WSL 側でサーバーを起動する（start.bat / Phase 2 のトレイアプリから呼ぶ）
#   scripts/start_server.sh [--speed 1.5 など、python -m realtime_transcriber の引数]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f config.toml ]; then
  # 初回: 雛形から作る。確定用は精度重視の large-v3（あとからブラウザの設定画面で変えられる）
  sed 's/^final_model = ""$/final_model = "large-v3"/' config.example.toml > config.toml
  echo "[SETUP] config.example.toml から config.toml を作成しました（確定用モデル: large-v3）"
fi

source .venv/bin/activate
source scripts/gpu_env.sh  # GPU のライブラリのパス（LD_LIBRARY_PATH）

# 終了時の後片付け用に PID を残す（exec するので python がこの PID を引き継ぐ）。scripts/stop_server.sh が使う
mkdir -p run
echo $$ > run/server.pid
exec python -m realtime_transcriber "$@"
