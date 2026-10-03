"""音声区間をテキストにする部分。テストでは Transcriber を満たす偽物に差し替える"""

from __future__ import annotations

import re
from typing import Protocol

import numpy as np

from .audio import SR

# Whisper が無音や BGM でよく出す「幻聴」フレーズ
HALLUCINATIONS = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "チャンネル登録よろしくお願いします",
    "チャンネル登録よろしくお願いします。",
}
REPEAT_RE = re.compile(r"(.{1,8}?)\1{3,}")  # 同じ断片が4回以上続く暴走を1回に畳む


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray, context: str, final: bool) -> str:
        """audio（16kHz float32、通常速度）を認識する。final=True は確定用の高精度設定"""
        ...

    def set_glossary(self, glossary: list[str]) -> None:
        """用語集を差し替える（次の認識から使う）"""
        ...


def clean_text(text: str) -> str:
    text = REPEAT_RE.sub(r"\1", text.strip())
    return "" if text in HALLUCINATIONS else text


class WhisperTranscriber:
    """faster-whisper による実装。暫定用と確定用で別のモデルを使える"""

    def __init__(self, partial_model: str, final_model: str | None, glossary: list[str]):
        from faster_whisper import WhisperModel  # GPU を使わないテストでは読み込まない

        print(f"[MODEL] 暫定用: {partial_model} を読み込み中...")
        self.partial = WhisperModel(partial_model, device="cuda", compute_type="float16")
        if final_model and final_model != partial_model:
            print(f"[MODEL] 確定用: {final_model} を読み込み中...")
            self.final = WhisperModel(final_model, device="cuda", compute_type="float16")
        else:
            self.final = self.partial
        self.set_glossary(glossary)
        for final in (False, True):  # ウォームアップ
            self.transcribe(np.zeros(SR, dtype=np.float32), "", final=final)
        print("[MODEL] 準備完了")

    def set_glossary(self, glossary: list[str]) -> None:
        self.hotwords = " ".join(glossary) or None
        self.glossary_prompt = "、".join(glossary)

    def transcribe(self, audio: np.ndarray, context: str, final: bool) -> str:
        prompt = "。".join(p for p in (self.glossary_prompt, context) if p) or None
        segments, _ = (self.final if final else self.partial).transcribe(
            audio,
            language="ja",
            beam_size=5 if final else 1,
            temperature=(0.0, 0.2, 0.4) if final else 0.0,
            condition_on_previous_text=False,
            initial_prompt=prompt,
            hotwords=self.hotwords,
            without_timestamps=True,
            vad_filter=False,  # VAD は自前でかけている
        )
        return clean_text("".join(s.text for s in segments if s.no_speech_prob < 0.6))
