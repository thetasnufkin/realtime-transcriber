"""音声の共通定数と前処理"""

from __future__ import annotations

import numpy as np
from audiotsm import wsola
from audiotsm.io.array import ArrayReader, ArrayWriter

SR = 16000


def time_stretch(audio: np.ndarray, speed: float) -> np.ndarray:
    """speed 倍速の音声を通常速度に戻す（ピッチは保ったまま）"""
    if abs(speed - 1.0) < 1e-3:
        return audio
    writer = ArrayWriter(channels=1)
    wsola(channels=1, speed=1.0 / speed).run(ArrayReader(audio.reshape(1, -1)), writer)
    return writer.data[0].astype(np.float32)
