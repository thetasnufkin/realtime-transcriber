#!/usr/bin/env bash
# start_server.sh で起動したサーバーを止める（/api/shutdown で止まらなかったときの後片付け）
#   scripts/stop_server.sh [猶予秒数（既定 5）]
# PID が別のプロセスに使い回されていることがあるので、コマンドラインを確かめてから止める
set -uo pipefail
cd "$(dirname "$0")/.."
PID_FILE=run/server.pid
GRACE_S=${1:-5}

if [ ! -f "$PID_FILE" ]; then
  echo "[STOP] $PID_FILE がありません（起動していないか、start_server.sh 以外で起動）"
  exit 0
fi
pid=$(cat "$PID_FILE")

# 生きているサーバーか（終了済みのゾンビはコマンドラインが空になるので、kill -0 ではなくこれで見る）
is_server() {
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "realtime_transcriber"
}

if ! is_server; then
  echo "[STOP] PID $pid はサーバーではありません（すでに終了済み）"
  rm -f "$PID_FILE"
  exit 0
fi

echo "[STOP] PID $pid に終了を指示します"
kill -TERM "$pid"  # uvicorn は SIGTERM で途中の区間を確定させてから終わる
for _ in $(seq $((GRACE_S * 10))); do
  is_server || { rm -f "$PID_FILE"; echo "[STOP] 終了しました"; exit 0; }
  sleep 0.1
done
echo "[STOP] ${GRACE_S} 秒で終わらないので強制終了します"
kill -KILL "$pid"
rm -f "$PID_FILE"
