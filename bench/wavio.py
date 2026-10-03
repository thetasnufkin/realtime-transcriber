"""WAV の読み込み（PCM 16bit と 32bit float）。FLEURS は 32bit float、音声合成は PCM 16bit"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import soxr

SR = 16000
PCM, IEEE_FLOAT, EXTENSIBLE = 1, 3, 0xFFFE


def read_wav(path: Path | str, sr: int = SR) -> np.ndarray:
    """モノラル float32（-1〜1）で返す。ステレオは平均、サンプリングレートが違えば変換する"""
    data = Path(path).read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"WAV ではありません: {path}")
    pos, fmt, samples = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + size]
        if cid == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == EXTENSIBLE:
                tag = struct.unpack("<H", body[24:26])[0]  # SubFormat GUID の先頭
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            samples = body
        pos += 8 + size + (size & 1)
    if fmt is None or samples is None:
        raise ValueError(f"fmt / data チャンクがありません: {path}")
    tag, channels, rate, bits = fmt
    if (tag, bits) == (PCM, 16):
        x = np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768.0
    elif (tag, bits) == (IEEE_FLOAT, 32):
        x = np.frombuffer(samples, dtype="<f4").astype(np.float32)
    else:
        raise ValueError(f"未対応の WAV 形式です（format {tag}, {bits}bit）: {path}")
    x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != sr:
        x = soxr.resample(x, rate, sr).astype(np.float32)
    return x
