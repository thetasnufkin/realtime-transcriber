"""FLEURS（Google、CC BY 4.0）の日本語 test セット

人が読み上げた Wikipedia の文。音声は bench/data/fleurs/ にダウンロードし、リポジトリには入れない。
同じ文を複数の話者が読んでいるので、文ごとに 1 つの録音を使う。
"""

from __future__ import annotations

import csv
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from bench.wavio import read_wav

BASE_URL = "https://huggingface.co/datasets/google/fleurs/resolve/main/data/ja_jp"
DATA_DIR = Path(__file__).parent / "data" / "fleurs"
SR = 16000


@dataclass(frozen=True)
class Item:
    id: str
    text: str  # 原文（句読点・漢字・数字を含む）
    audio: np.ndarray  # 16kHz mono float32


def download(data_dir: Path = DATA_DIR) -> None:
    """test.tsv と音声（約 450MB）を取得する。済んでいれば何もしない"""
    data_dir.mkdir(parents=True, exist_ok=True)
    tsv = data_dir / "test.tsv"
    if not tsv.exists():
        urllib.request.urlretrieve(f"{BASE_URL}/test.tsv", tsv)
    if not (data_dir / "test").is_dir():
        tar = data_dir / "test.tar.gz"
        print(f"[FLEURS] 音声をダウンロード中（約 450MB）: {tar}")
        urllib.request.urlretrieve(f"{BASE_URL}/audio/test.tar.gz", tar)
        with tarfile.open(tar) as t:
            t.extractall(data_dir, filter="data")
        tar.unlink()


def load(n: int, data_dir: Path = DATA_DIR) -> list[Item]:
    """文 ID 順に n 文を読む（同じ文は最初の録音だけ）。毎回同じ並びになる"""
    download(data_dir)
    first: dict[str, tuple[str, str]] = {}
    with (data_dir / "test.tsv").open(encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            sid, file, raw = row[0], row[1], row[2]
            if sid not in first or file < first[sid][0]:
                first[sid] = (file, raw)
    items = []
    for sid in sorted(first, key=int)[:n]:
        file, raw = first[sid]
        items.append(Item(sid, raw, read_wav(data_dir / "test" / file, SR)))
    return items
