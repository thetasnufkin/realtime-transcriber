"""CER（文字誤り率）: 句読点・空白を除き、全角半角などを揃えてから編集距離で数える"""

from __future__ import annotations

import unicodedata

from rapidfuzz.distance import Levenshtein


def normalize(text: str) -> str:
    """NFKC で全角英数字などを揃え、句読点・記号・空白を除く"""
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if unicodedata.category(c)[0] not in ("P", "S", "Z") and not c.isspace())


def cer(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize(reference), normalize(hypothesis)
    return Levenshtein.distance(ref, hyp) / max(len(ref), 1)


def term_recall(terms: list[str], hypothesis: str) -> float:
    """用語集の語が正しい表記で出てきた割合"""
    hyp = normalize(hypothesis)
    return sum(normalize(t) in hyp for t in terms) / max(len(terms), 1)
