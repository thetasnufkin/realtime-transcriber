"""bench/: CER の計算、ストリームの組み立て、仮想時計による遅延の測り方（GPU なし）"""

import numpy as np
import pytest
from fakes import FakeTranscriber, energy_vad, silence, tone

from bench.metrics import cer, normalize, term_recall
from bench.simulate import CHUNK_S, IDLE_TIMEOUT_S, simulate
from bench.stream import GAP_S, LEAD_S, SR, build_stream, speed_up
from realtime_transcriber.segmenter import MIN_SILENCE_S, Session


def test_normalize_removes_punctuation_spaces_and_unifies_width():
    assert normalize("「ＡＢＣ」は、 1つ目です。") == "ABCは1つ目です"


def test_cer():
    assert cer("今日は晴れです。", "今日は晴れです") == 0.0
    assert cer("今日は晴れです", "今日は雨です") == pytest.approx(2 / 7)  # 置換 1 + 削除 1
    assert cer("あいう", "") == 1.0


def test_term_recall():
    assert term_recall(["東雲電機", "AI研究室"], "東雲電機のＡＩ研究室です") == 1.0  # 全角英字も揃える
    assert term_recall(["東雲電機", "AI研究室"], "しののめ電機の AI 研究室です") == 0.5


def test_build_stream_places_utterances_with_gaps():
    s = build_stream([("あ", tone(1.0)), ("い", tone(2.0))])
    assert [(x.text, x.start, x.end) for x in s.segments] == [
        ("あ", LEAD_S, LEAD_S + 1.0),
        ("い", LEAD_S + 1.0 + GAP_S, LEAD_S + 3.0 + GAP_S),
    ]
    assert s.duration == pytest.approx(LEAD_S + 3.0 + 2 * GAP_S)
    assert s.reference == "あい"


def test_speed_up_shortens_audio_and_times():
    s = build_stream([("あ", tone(2.0)), ("い", tone(2.0))])
    fast = speed_up(s, 2.0)
    assert fast.duration == pytest.approx(s.duration / 2, abs=0.05)
    assert fast.segments[1].start == pytest.approx(s.segments[1].start / 2)


class FakeClock:
    """step のたびに決まった時間だけ進む時計（処理時間を固定して遅延の計算を確かめる）"""

    def __init__(self, per_step: float) -> None:
        self.t, self.per_step, self.calls = 0.0, per_step, 0

    def __call__(self) -> float:
        self.calls += 1
        if self.calls % 2 == 0:  # 2 回目の呼び出し（step の後）で進める
            self.t += self.per_step
        return self.t


def run(per_step: float):
    stream = build_stream([("あ", tone(1.0)), ("い", tone(1.5, 330))])
    session = Session(FakeTranscriber(), 1.0, vad=energy_vad)
    return stream, simulate(session, stream.audio, clock=FakeClock(per_step))


def test_latency_without_processing_time_is_the_silence_wait():
    stream, r = run(per_step=0.0)
    assert len(r.finals) == 2
    for e, seg in zip(r.finals, stream.segments, strict=True):
        assert e.event.end == pytest.approx(seg.end, abs=0.16)  # 区間の終わり ≒ 話し終わり（VAD の余白）
        # 話し終わりから 0.6 秒の無音を待ち、0.1 秒単位で届いたところで確定する
        assert MIN_SILENCE_S - 0.16 <= e.latency <= MIN_SILENCE_S + CHUNK_S + 0.16
    assert r.processing_s == 0.0 and r.rtf == 0.0


def test_slow_processing_adds_to_latency_and_rtf():
    _, fast = run(per_step=0.0)
    _, slow = run(per_step=0.05)  # 0.1 秒のチャンクごとに 0.05 秒 → 処理は追いつく
    assert slow.rtf == pytest.approx(0.5, abs=0.05)
    for a, b in zip(fast.finals, slow.finals, strict=True):
        assert 0 < b.latency - a.latency <= 0.05 + 1e-9  # 遅れは 1 回分の処理時間まで


def test_backlog_is_handed_over_at_once_when_processing_is_slower_than_real_time():
    _, r = run(per_step=0.3)  # 処理が音声より遅い（溜まった分をまとめて渡す）
    assert r.rtf < 1.5  # 1 チャンクずつ処理していたら 3 倍になる
    assert len(r.finals) == 2


def test_rest_is_flushed_after_the_idle_timeout():
    session = Session(FakeTranscriber(), 1.0, vad=energy_vad)
    audio = np.concatenate([silence(0.5), tone(1.0)])  # 話し終わりの無音なしで終わる
    r = simulate(session, audio, clock=FakeClock(0.0))
    assert len(r.finals) == 1
    assert r.finals[0].at == pytest.approx(len(audio) / SR + IDLE_TIMEOUT_S, abs=CHUNK_S)


def test_read_wav_pcm16_and_float32_stereo_resampled(tmp_path):
    import struct

    from bench.wavio import read_wav

    def wav(path, tag, bits, rate, samples: np.ndarray, channels=1):
        body = samples.tobytes()
        fmt = struct.pack(
            "<HHIIHH", tag, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits
        )
        path.write_bytes(
            b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(body)) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(body)) + body
        )  # fmt: skip

    t = np.arange(16000) / 16000
    x = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    wav(tmp_path / "a.wav", 1, 16, 16000, (x * 32767).astype("<i2"))
    assert np.allclose(read_wav(tmp_path / "a.wav"), x, atol=1e-3)

    stereo = np.stack([x, np.zeros_like(x)], axis=1).astype("<f4")  # 左だけ鳴っている
    wav(tmp_path / "b.wav", 3, 32, 16000, stereo, channels=2)
    assert np.allclose(read_wav(tmp_path / "b.wav"), x / 2, atol=1e-6)

    x48 = (0.5 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)).astype("<f4")
    wav(tmp_path / "c.wav", 3, 32, 48000, x48)
    assert len(read_wav(tmp_path / "c.wav")) == pytest.approx(16000, abs=10)


def test_report_tables_and_charts(tmp_path, monkeypatch):
    import json

    from bench import report

    base = {"dataset": "fleurs", "utterances": 100, "audio_min": 24.1, "gpu": "GPU", "date": "2026-10-03",
            "latency_p90_s": 2.0, "rtf": 0.3, "speed": 1.0, "speed_aware": True}  # fmt: skip
    results = [
        {**base, "name": "kotoba-whisper のみ", "group": "models", "cer": 0.05, "latency_p50_s": 1.0},
        {**base, "name": report.BASELINE, "group": "models", "cer": 0.04, "latency_p50_s": 1.5},
        {**base, "name": "2 倍速・対応あり", "group": "speed", "speed": 2.0, "cer": 0.06,
         "latency_p50_s": 1.2},
        {**base, "name": "2 倍速・対応なし", "group": "speed", "speed": 2.0, "speed_aware": False, "cer": 0.2,
         "latency_p50_s": 2.5},
        {**base, "name": "2 倍速・区切りだけ対応", "group": "speed", "speed": 2.0, "stretch": False,
         "cer": 0.05, "latency_p50_s": 1.1},
        {**base, "name": "用語集あり", "group": "glossary", "dataset": "glossary", "utterances": 12,
         "cer": 0.03, "term_recall": 0.9},
    ]  # fmt: skip
    monkeypatch.setattr(report, "RESULTS", tmp_path / "results.json")
    monkeypatch.setattr(report, "OUT_MD", tmp_path / "RESULTS.md")
    monkeypatch.setattr(report, "OUT_PNG", tmp_path / "speed_{mode}.png")
    (tmp_path / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    report.main()

    md = (tmp_path / "RESULTS.md").read_text(encoding="utf-8")
    assert "| kotoba + large-v3（既定） | 4.0% | 1.50 秒 |" in md
    assert md.count("| 1 倍 |") == 1 and "| 1 倍 | （等速） | 4.0% |" in md  # 1 倍速は方式によらず同じ
    assert "| 2 倍 | WSOLA で戻す | 6.0% |" in md
    assert "| 2 倍 | 区切りだけ | 5.0% |" in md
    assert "| 2 倍 | なし | 20.0% | 2.50 秒 |" in md
    assert "| 用語集あり | 90.0% | 3.0% |" in md
    for mode in ("light", "dark"):
        assert (tmp_path / f"speed_{mode}.png").stat().st_size > 10_000
