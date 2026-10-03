# CLAUDE.md — realtime-transcriber

このファイルは Claude Code がこのリポジトリで作業するときの方針書。作業前に必ず読むこと。

## プロジェクトの目的

PC で再生中の音声（ブラウザ・動画・オンライン会議）や、マイク・録音ファイルを、
**完全ローカル**でリアルタイムに日本語文字起こしするアプリ。
就活用ポートフォリオとして GitHub で公開する。「動くこと」に加えて
**設計判断の理由と、数値で示した改善（精度・遅延）**を README で説明できる状態をゴールにする。

## 譲れない方針

- **音声を外部に送らない**。文字起こしはローカル GPU のみ。外部 API はベンチマーク比較用のオプションとしてだけ許可し、既定では無効。
- 既存の動作（Windows ループバック → WSL サーバー）を壊さない。大きな変更の前にプランを提示して確認をとる。
- サーバーは **127.0.0.1 だけで待ち受け**、この PC からしかアクセスできないようにする（Windows のブラウザからは WSL の localhost 転送で届く）。そのため認証は付けない。
- 1 つの変更は 1 つの目的に絞り、動作確認の手順をコミットメッセージか PR 説明に書く。

## 開発環境（作業者の PC）

- Windows 11 + WSL2 (Ubuntu 26.04)。リポジトリは WSL 側 `~/projects/realtime-transcriber`
- GPU: RTX 3060 12GB（WSL から CUDA 利用可、nvcc なし。cuBLAS/cuDNN は pip の nvidia-* パッケージ）
- Python 3.12（uv 管理）。**システムの Python 3.14 は使わない**
- `source .venv/bin/activate` で `LD_LIBRARY_PATH` が設定される。`uv run` ではこれが効かないので、GPU を使うコマンドは activate 後に `python ...` で実行する
- Windows 側の Python は uv が用意する（`uv run capture_client.py`、PEP 723 のインラインメタデータで依存を宣言）
- Windows の PowerShell 5.1 / cmd 用スクリプト（.ps1 / .bat）は **ASCII のみ**で書く（UTF-8 の日本語は文字化けして壊れる）。.bat は CRLF

## 現在の構成

```
src/realtime_transcriber/     WSL。サーバー本体（python -m realtime_transcriber で起動）
  app.py                      FastAPI: GET / (ビューア), WS /ingest (WS / は旧クライアント用), WS /captions, /api/*
  hub.py                      確定文の履歴・閲覧者への配信・transcripts/ への保存
  export.py                   確定文の TXT / SRT 書き出し（/api/transcript.txt, .srt）
  segmenter.py                VAD で区切り、暫定/確定の Event（時刻付き）を出す
  transcriber.py              Transcriber Protocol と faster-whisper 実装
  config.py                   既定値 < config.toml < CLI 引数
  static/                     ビューア（素の HTML/JS/CSS）
scripts/start_server.sh       WSL。初回に config.toml を作り（確定用 large-v3）、PID を run/ に記録して起動
scripts/stop_server.sh        WSL。PID のコマンドラインを確かめてから止める（/api/shutdown で止まらないときの後片付け）
windows/                      Windows。WSL のフォルダーから uv / uvw で直接実行する（PEP 723）
  tray_app.py                 タスクトレイ常駐アプリ（pystray）。デスクトップのショートカットから uvw で起動
  controller.py               起動・終了の手順（OS 非依存。Platform を偽物にして pytest）
  winplatform.py              wsl.exe の起動（窓なし）、/api/health、二重起動防止（ミューテックス）
  tray_view.py                状態 → アイコンの色・説明文・通知
  capture.py                  WASAPI ループバックの取り込み（LoopbackStreamer。入力元は差し替え可能）
  setup.bat / setup.ps1       ショートカットの作成（デスクトップ・スタートメニュー）。uninstall.bat で削除
server/transcribe_server.py   互換シム（旧 start.bat 用）
capture_client.py             Windows。windows/capture.py をコンソールで動かす薄い CLI
start.bat                     Windows。コンソールを見ながら起動したいとき用（サーバー → ブラウザ → capture_client）
tests/                        pytest（GPU 不要。偽の Transcriber・VAD・入力元・Platform を使う）
```

ログ: トレイとサーバーの出力は `%LOCALAPPDATA%\realtime-transcriber\`（tray.log / server.log）。

サーバーの処理: Silero VAD で発話区間を判定 → 発話中は 1 秒ごとに軽量モデルで暫定表示
→ 無音 0.6 秒 / 上限 20 秒（区間内の最長の「間」で切る）/ 入力途切れ で確定し、
高精度モデル（任意で large-v3）で認識。倍速再生は WSOLA で通常速度に戻してから認識。
直前の確定文と用語集をプロンプトと hotwords に渡す。繰り返しの暴走と定番の幻聴フレーズは除去。

## 目標アーキテクチャ（ロードマップ）

### Phase 1: Web UI 化（最優先）
- サーバーを FastAPI（+ uvicorn）に移行し、同じポートで以下を提供する
  - `GET /` : 字幕ビューア（静的 HTML/JS、ビルド不要の素の JS で十分）
  - `WS /ingest` : 音声入力（既存の capture_client と互換を保つ）
  - `WS /captions` : 暫定/確定テキストを JSON で配信（複数の閲覧者に broadcast）
- ビューアの機能: 暫定（灰色）→ 確定の差し替え表示、自動スクロール、文字サイズ変更、
  テキスト/SRT のダウンロード、設定（速度・用語集・モデル）の変更
- 設定は CLI 引数ではなく `config.toml` + UI から変更できるようにする
- 一時停止すると、segmenter に溜まっている途中の音声も破棄する
- Phase 2 のために `GET /api/health`（準備完了の確認）と `POST /api/shutdown`（正常終了）を用意する

### Phase 2: ワンクリック起動
- 目標: **cmd や PowerShell を一切触らず、デスクトップのアイコンのダブルクリックだけで起動から終了までできる**こと
- Windows のタスクトレイ常駐アプリ（pystray）が次をまとめて行う
  1. コンソール窓を出さずに WSL のサーバーを起動（`wsl.exe` を `CREATE_NO_WINDOW` で。トレイ自体は `uvw` / `pythonw`）。
     すでにサーバーが動いていれば起動せずに使い回す
  2. ループバックの音声取り込みを開始
  3. サーバーが応答したらすぐブラウザで字幕画面を開く（モデルの読み込み中は画面に「読み込み中」と出るので、
     待たされている理由がわかる）。応答の確認には `/api/health` を使う（503 = 読み込み中でも応答とみなす）
  4. トレイの「終了」で `/api/shutdown` を呼び、取り込みと WSL のプロセスを含めて全部止める
     （使い回したサーバーも止める）
- デスクトップとスタートメニューにショートカットを作るセットアップを用意する（.ps1 は ASCII のみ）
- ログイン時の自動起動は入れない（GPU メモリを約 8GB 使うので、使うときだけ起動する）

### Phase 3: 評価とアピール
- `bench/` に評価ハーネス: 正解テキスト付きの音声で CER（文字誤り率）、確定までの遅延、RTF を計測
- 比較軸: モデル（kotoba-whisper v2.0 / large-v3 / ReazonSpeech など）、倍速 1x/1.5x/2x と WSOLA の有無、用語集の有無
- 結果を README に表とグラフで載せる。CER は句読点・空白を除いて正規化してから計算する
- 話者分離、要約、翻訳は余力があれば

## テストの方針

- GPU やモデルがなくてもロジックを検証できるよう、`Transcriber` を差し替え可能にしておく
  （区間の長さを返す偽物で、VAD の区切り・取りこぼしがないこと・倍速時の上限を pytest で確認）
- テスト音声は `espeak-ng` などで生成するか、ライセンスが明確なコーパスを使う。
  **就活の説明会など、第三者の録音をリポジトリにコミットしない**
- 実際の再生音の取り込みや聞き取りの質は Claude Code からは確認できない。
  作業者が試す手順（どの動画で、どのオプションで、何を見るか）を毎回具体的に書く

## コーディング規約

- Python: 型ヒントあり、`ruff` でフォーマット/リント。設定値は定数かモジュール冒頭にまとめる
- ログやメッセージは日本語で OK。ただし .ps1 / .bat は ASCII のみ
- 依存の追加は `uv add`。Windows クライアントの依存は PEP 723 ヘッダーに書く
- 秘密情報（トークン等）はコミットしない。`transcripts/` と `.venv/` は .gitignore

## README に必ず書くこと

1. デモ GIF（字幕が流れる様子）
2. 構成図（Windows ループバック → WSL サーバー → ブラウザの字幕画面）
3. 設計判断: 完全ローカルにした理由、VAD による区切り、暫定/確定の 2 段階、倍速対応、2 モデル構成
4. ベンチマーク結果の表
5. セットアップ手順（WSL + uv + GPU）と既知の制約（Windows + WSL2 + NVIDIA GPU 前提、この PC からのみアクセス可能 等）
