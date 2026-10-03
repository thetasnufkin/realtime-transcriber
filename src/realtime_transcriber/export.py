"""確定文を TXT / SRT に書き出す"""

from __future__ import annotations

from collections.abc import Sequence

from .hub import Caption

MIN_CAPTION_S = 0.5  # SRT で一瞬で消えないよう、表示時間の最小値


def to_txt(captions: Sequence[Caption]) -> str:
    """transcripts/ に保存しているのと同じ形式"""
    return "".join(f"[{c.at}] {c.text}\n" for c in captions)


def srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(captions: Sequence[Caption]) -> str:
    """時刻は最初の音声を受け取ってからの経過時間"""
    blocks = []
    for i, c in enumerate(captions, start=1):
        end = max(c.end, c.start + MIN_CAPTION_S)
        blocks.append(f"{i}\n{srt_time(c.start)} --> {srt_time(end)}\n{c.text}\n")
    return "\n".join(blocks)
