import numpy as np
import pytest
from fakes import FakeTranscriber, energy_vad, silence, to_pcm, tone

from realtime_transcriber.audio import SR
from realtime_transcriber.segmenter import MAX_SEGMENT_S, Event, Session

CHUNK = int(0.1 * SR)  # capture_client と同じ 0.1 秒ごとに届く想定


def feed(session: Session, audio: np.ndarray) -> list[Event]:
    """サーバーと同じく、チャンクを受け取るたびに step する"""
    out = []
    pcm = to_pcm(audio)
    for i in range(0, len(pcm), CHUNK * 2):
        session.add(pcm[i : i + CHUNK * 2])
        out += session.step()
    return out


def voiced(audio: np.ndarray) -> int:
    """発話（トーン）部分のサンプル数。int16 に量子化した値で数える"""
    return int(np.count_nonzero(np.frombuffer(to_pcm(audio), dtype=np.int16)))


def make_session(speed: float = 1.0) -> tuple[Session, FakeTranscriber]:
    tr = FakeTranscriber()
    return Session(tr, speed, vad=energy_vad), tr


def test_silence_only_emits_nothing_and_keeps_buffer_short():
    s, tr = make_session()
    assert feed(s, silence(5.0)) == []
    assert tr.calls == []
    assert len(s.buf) <= int(0.5 * SR)


def test_cut_on_silence_without_losing_speech():
    s, tr = make_session()
    audio = np.concatenate([silence(0.5), tone(1.5), silence(1.0), tone(1.2, 330), silence(1.0)])
    out = feed(s, audio)

    finals = [e for e in out if e.kind == "final"]
    assert len(finals) == 2
    assert sum(voiced(a) for a in tr.finals) == voiced(audio)
    # 時刻は発話の位置（前後に VAD の余白 0.15 秒まで）
    assert finals[0].start == pytest.approx(0.5, abs=0.16)
    assert finals[0].end == pytest.approx(2.0, abs=0.16)
    assert finals[1].start == pytest.approx(3.0, abs=0.16)
    assert finals[1].end == pytest.approx(4.2, abs=0.16)


def test_partial_is_emitted_during_speech_and_does_not_consume():
    s, tr = make_session()
    out = feed(s, tone(2.5))

    assert [e.kind for e in out] == ["partial", "partial"]
    assert tr.finals == []
    assert len(s.buf) == int(2.5 * SR)


def test_force_flushes_the_rest():
    s, tr = make_session()
    feed(s, tone(1.0))
    out = s.step(force=True)

    assert [e.kind for e in out] == ["final"]
    assert len(s.buf) == 0
    assert voiced(tr.finals[0]) == voiced(tone(1.0))


def test_empty_final_emits_clear():
    """幻聴の除去などで確定文が空になったら、暫定表示を消すための clear を出す"""
    s, tr = make_session()
    tr.transcribe = lambda audio, context, final: ""
    out = feed(s, np.concatenate([tone(1.0), silence(1.0)]))

    assert [e.kind for e in out] == ["clear"]
    assert s.context == ""


def test_force_on_short_buffer_just_clears():
    s, tr = make_session()
    s.add(to_pcm(tone(0.2)))
    assert s.step(force=True) == []
    assert len(s.buf) == 0


@pytest.mark.parametrize("speed", [1.0, 1.5, 2.0])
def test_max_length_cuts_at_a_gap(speed: float):
    """間の短い長話は上限で切る。切る位置は「間」の中、長さは通常速度換算で上限以内"""
    s, tr = make_session(speed)
    # 0.4 秒の間は通常の区切り（0.6 秒 / speed）より短いので、上限に達するまで切れない。
    # 周期 1.3 秒は上限 20 秒を割り切らないので、上限ちょうどの位置は発話の途中になる
    unit = np.concatenate([tone(0.9 / speed), silence(0.4 / speed)])
    audio = np.concatenate([unit] * int(60 * speed) + [silence(1.0)])
    out = feed(s, audio)

    assert len(tr.finals) >= 3
    finals = [e for e in out if e.kind == "final"]
    assert all(a.end <= b.start + 1e-9 for a, b in zip(finals, finals[1:], strict=False))
    assert finals[-1].end <= len(audio) / SR
    for a in tr.finals[:-1]:  # 最後は無音で切れた区間
        assert len(a) <= (MAX_SEGMENT_S + 0.5) * SR  # time_stretch 後の長さ = 通常速度換算
        assert len(a) >= 0.3 * MAX_SEGMENT_S * SR
    if speed == 1.0:
        assert sum(voiced(a) for a in tr.finals) == voiced(audio)
        for a in tr.finals[:-1]:
            assert not a[-int(0.05 * SR) :].any()  # 発話の途中ではなく間で切れている
