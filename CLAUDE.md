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
server/transcribe_server.py   WSL。WebSocket で 16kHz mono int16 PCM を受け取り文字起こし
capture_client.py             Windows。WASAPI ループバックで再生音を取り込みサーバーへ送る
start.bat                     Windows。上の 2 つをまとめて起動
```

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
- 同じ Wi-Fi のスマホ・タブレットのブラウザから字幕を見られるようにする

### Phase 2: 入力ソースを増やす
- **ブラウザのタブ音声**: デスクトップ版 Chrome/Edge の `getDisplayMedia({audio: true})` でタブの音を取り込み、
  AudioWorklet で 16kHz に変換して `/ingest` に送る（Windows クライアントなしで使える）
- **ブラウザのマイク**: `getUserMedia`（スマホでも動く）
- **ファイルアップロード**: 録音/動画ファイルを送ってバッチ文字起こし（スマホの録音を後から処理する用途）
- 注意: localhost 以外でマイク/画面共有の API を使うには **HTTPS が必須**。
  自己署名証明書ではなく、Tailscale（`tailscale serve`）などで HTTPS とリモートアクセスを用意する方針
- 注意: スマホのブラウザは **他アプリの再生音を取り込めない**（OS の制約）。スマホは「字幕を見る」「マイク」「ファイル」の3用途とする

### Phase 3: 起動を簡単に
- Windows のタスクトレイ常駐アプリ（pystray）: WSL サーバーの起動/停止、ループバック送信の ON/OFF、ビューアを開く
- WSL のネットワークは mirrored モード（`.wslconfig` の `networkingMode=mirrored`）を検討し、LAN 内の端末から直接届くようにする

### Phase 4: 評価とアピール
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
2. 構成図（Windows ループバック / ブラウザ → サーバー → 複数ビューア）
3. 設計判断: 完全ローカルにした理由、VAD による区切り、暫定/確定の 2 段階、倍速対応、2 モデル構成
4. ベンチマーク結果の表
5. セットアップ手順（WSL + uv + GPU）と既知の制約（スマホは他アプリ音声を取れない等）
