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

# GPU のライブラリのパス（LD_LIBRARY_PATH）は activate で設定される
source .venv/bin/activate
exec python -m realtime_transcriber "$@"
