# アーキテクチャ

> Phase 3（評価と README）完了時点の構成。実装が変わったらこの図も更新する。

PC で再生中の音声を、外部に一切送らずにローカルの GPU でリアルタイムに文字起こしする。
処理は Windows 側（タスクトレイ常駐アプリ・音声の取り込み）と WSL2 側（文字起こし・Web UI）に分かれ、
両者は PC の中だけで閉じた WebSocket / HTTP（`127.0.0.1:8765`）でつながっている。
使う人はデスクトップの「リアルタイム字幕」をダブルクリックするだけで、cmd や PowerShell は触らない。

## 全体構成

```mermaid
flowchart TB
    subgraph WIN["Windows 11"]
        LNK["デスクトップの<br/>ショートカット"]
        TRAY["tray_app.py（タスクトレイ・uvw で窓なし）<br/>起動・状態の表示・終了（controller.py）"]
        SRC["再生中の音声<br/>（ブラウザ・動画・会議アプリ）"]
        LOOP["WASAPI ループバック<br/>スピーカーに出る音をそのまま取得"]
        CAP["capture.py（トレイ内のスレッド）<br/>モノラル化 → 16kHz に変換（soxr）"]
        VIEW["ブラウザ<br/>字幕ビューア"]
        LNK --> TRAY
        SRC --> LOOP --> CAP
        TRAY -- "開始 / 停止" --> CAP
        TRAY -- "応答したら開く" --> VIEW
    end

    subgraph WSL["WSL2 (Ubuntu) ・ .venv (Python 3.12)"]
        API["FastAPI / uvicorn<br/>127.0.0.1:8765 だけで待ち受け"]
        SEG["Segmenter<br/>発話の区切りを判定"]
        VAD["Silero VAD<br/>（ONNX・CPU）"]
        TSM["WSOLA（既定は使わない）<br/>倍速の音声を通常速度に戻す"]
        TR["Transcriber（faster-whisper）"]
        HUB["Hub<br/>確定文の履歴・配信"]
        CFG[("config.toml")]
        OUT[("transcripts/*.txt")]

        API -- "PCM" --> SEG
        SEG <--> VAD
        SEG --> TR
        SEG -. "speed_restore = true のとき" .-> TSM -.-> TR
        TR -- "暫定 / 確定テキスト" --> HUB
        HUB --> OUT
        API <--> CFG
        SH["scripts/start_server.sh<br/>初回に config.toml を作成・PID を記録"] --> API
    end

    subgraph GPU["RTX 3060 (VRAM 12GB)"]
        M1["kotoba-whisper v2.0<br/>暫定用・速さ重視"]
        M2["large-v3（任意）<br/>確定用・精度重視"]
    end

    TRAY -- "wsl.exe（CREATE_NO_WINDOW）" --> SH
    TRAY -- "/api/health ・ /api/pause<br/>/api/shutdown" --> API
    CAP -- "WebSocket /ingest<br/>16kHz mono int16 PCM" --> API
    TR <-- "CTranslate2 + CUDA<br/>(cuBLAS / cuDNN)" --> GPU
    HUB -- "WebSocket /captions<br/>JSON" --> VIEW
    VIEW -- "GET / ・ /api/config<br/>/api/pause ・ /api/resume<br/>TXT / SRT のダウンロード" --> API
```

- **音声は PC の外に出ない。** サーバーは `127.0.0.1` だけで待ち受け、Windows の `localhost` からは WSL2 の localhost 転送で届く。同じ Wi-Fi の他の端末からも接続できない。
- **Windows 側で取り込む理由:** WSL からは Windows で再生中の音を取得できないため、取り込みだけを Windows のプログラムに任せている。
- **トレイアプリは WSL のフォルダーから直接動かす:** ショートカットは `uvw run \\wsl.localhost\...\windows\tray_app.py` を実行する。
  WSL の場所はこのパスから割り出すので設定を二重に持たず、コードを更新しても入れ直しが要らない
  （ローカルにコピーした場合と起動時間はほぼ同じ約 1 秒だった）。
- **重い処理は WSL 側にまとめる:** GPU を使う処理、区切りの判定、Web UI の配信は 1 つのサーバープロセスが担当する。
- **設定は `config.toml` とブラウザから:** モデル・再生速度・用語集はブラウザの設定画面から変えられ、`config.toml` に保存される。起動時の CLI 引数はそれより優先される。
- **一時停止:** ブラウザで一時停止すると、受け取った音声を捨てるだけでなく、区切り待ちの途中の音声も破棄する（再開後に停止前の音声が字幕に混ざらない）。

## 起動から終了まで（ワンクリック起動）

```mermaid
sequenceDiagram
    autonumber
    actor U as 使う人
    participant T as tray_app.py<br/>(Windows・トレイ)
    participant W as wsl.exe → start_server.sh<br/>(窓なし)
    participant S as サーバー<br/>(WSL・127.0.0.1:8765)
    participant B as ブラウザ

    U->>T: ショートカットをダブルクリック
    T->>S: GET /api/health
    alt 応答なし
        T->>W: 起動（出力は %LOCALAPPDATA% のログへ）
        W->>S: python -m realtime_transcriber
    else すでに動いている（start.bat などで起動済み）
        Note over T,S: 起動せずに使い回す
    end
    T->>S: 取り込みを開始（WebSocket /ingest。つながるまで再接続）
    loop 応答するまで 1 秒ごと
        T->>S: GET /api/health
    end
    S-->>T: 503 読み込み中（＝応答あり）
    T->>B: 字幕画面を開く（「モデルを読み込み中…」が出る）
    S-->>T: 200 準備完了 → アイコンが緑に
    U->>T: トレイの「終了」
    T->>T: 取り込みを止める（サーバーは途中の区間を確定）
    T->>S: POST /api/shutdown
    alt 15 秒で止まらない
        T->>W: scripts/stop_server.sh（PID を確かめて TERM → KILL）
    end
    Note over T: それでも止まらなければ wsl.exe を止める。最後にトレイを閉じる
```

- **ブラウザは応答したらすぐ開く:** モデルの準備（数秒〜初回のダウンロードでは数分）を待たずに開き、画面の「読み込み中」で待たされている理由がわかるようにしている。
- **終了で全部止める:** 使い回したサーバーも止める。止まらないときは PID ファイルで止めるが、PID が別のプロセスに使い回されていることがあるので、コマンドラインを確かめてから止める。
- **アイコンの色で状態がわかる:** 灰（起動中・読み込み中）、緑（受信中）、橙（一時停止中）、青（取り込みオフ）、赤（エラー。通知も出す）。
- **二重起動しない:** 名前付きミューテックスで 2 つ目の起動を検出し、字幕画面を開くだけで終わる。

## AI モデルはどこにあるか

```mermaid
flowchart LR
    HF["Hugging Face Hub<br/>（AI モデルの配布サイト）"]

    subgraph DISK["WSL のディスク（~/.cache/huggingface/hub/）"]
        D1["models--kotoba-tech--<br/>kotoba-whisper-v2.0-faster"]
        D2["models--Systran--<br/>faster-whisper-large-v3"]
    end

    subgraph PKG[".venv 内の faster_whisper パッケージ"]
        D3["assets/silero_vad_v6.onnx"]
    end

    subgraph RUN["実行中のメモリ"]
        G1["GPU VRAM<br/>Whisper 2 モデル（float16）"]
        C1["CPU / RAM<br/>Silero VAD（onnxruntime）"]
    end

    HF -- "初回起動時に 1 度だけ<br/>ダウンロード" --> DISK
    D1 -- "起動時に読み込み" --> G1
    D2 -- "起動時に読み込み" --> G1
    D3 -- "起動時に読み込み" --> C1
```

| モデル | 役割 | 配布元 | 保存場所 | 動く場所 |
|---|---|---|---|---|
| kotoba-whisper v2.0（CTranslate2 形式） | 暫定表示用。日本語に特化し、軽くて速い | Hugging Face `kotoba-tech/kotoba-whisper-v2.0-faster` | `~/.cache/huggingface/hub/` | GPU |
| Whisper large-v3（CTranslate2 形式） | 確定用。精度重視（任意。`final_model` が空なら kotoba-whisper で確定も行う） | Hugging Face `Systran/faster-whisper-large-v3` | `~/.cache/huggingface/hub/` | GPU |
| Silero VAD v6（ONNX） | 発話か無音かの判定 | `faster-whisper` パッケージに同梱 | `.venv/.../faster_whisper/assets/` | CPU |

- インターネットを使うのは、**モデルの初回ダウンロードのときだけ**。文字起こしの処理中は通信しない（`HF_HUB_OFFLINE=1` を付けて起動してもそのまま動く）。
- CTranslate2 形式は、Whisper の重みを推論向けに変換した形式。`faster-whisper` がこれを読み込み、CTranslate2 が CUDA（cuBLAS / cuDNN）を通じて GPU で計算する。
- cuBLAS / cuDNN はシステムにインストールせず、pip の `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` を `.venv` に入れて使っている（`source .venv/bin/activate` でライブラリのパスが通る）。

## 1 つの発話が字幕になるまで

```mermaid
sequenceDiagram
    autonumber
    participant C as capture_client<br/>(Windows)
    participant S as Segmenter<br/>(VAD)
    participant K as kotoba-whisper<br/>(暫定)
    participant L as large-v3<br/>(確定)
    participant V as ブラウザ

    loop 0.1 秒ごと
        C->>S: 16kHz PCM（WebSocket）
    end
    loop 発話が続いている間、1 秒ごと
        S->>K: ここまでの音声（プロンプトは渡さない）
        K-->>V: 暫定テキスト（灰色で表示）
    end
    Note over S: 0.6 秒の無音・20 秒の上限・入力の途切れ<br/>のいずれかで区切る（上限では一番長い「間」で切る）
    S->>L: 区切った音声 + 直前の確定文 + 用語集
    L-->>V: 確定テキスト（暫定を差し替え）
    Note over V: 確定文は transcripts/ にも保存され、<br/>TXT / SRT でダウンロードできる
```

- **暫定と確定の 2 段階:** 暫定は軽いモデルと速い設定（beam 1）ですぐに表示し、確定は重いモデルと丁寧な設定（beam 5）でやり直す。待ち時間の短さと精度を両立するための分担。
- **倍速再生への対応:** 指定した再生速度に合わせて無音判定の時間を縮め、倍速の音声のまま認識する。当初は認識の直前に WSOLA で通常速度に戻していたが、ベンチマークで CER が倍以上に悪化したため既定では戻さない（`speed_restore = true` で戻す。[bench/results/RESULTS.md](../bench/results/RESULTS.md)）。
- **文脈と用語集:** 直前の確定文と用語集をプロンプトと hotwords として確定用の large-v3 に渡し、固有名詞の表記ゆれを減らす。蒸留モデルの kotoba-whisper に渡すと文の途中で出力が止まるので、暫定には渡さない。Whisper 特有の繰り返しの暴走と、定番の幻聴フレーズは取り除く。
