import pytest

from realtime_transcriber.export import srt_time, to_srt, to_txt
from realtime_transcriber.hub import Caption

CAPTIONS = [
    Caption(id=1, text="本日はお越しいただきありがとうございます。", start=0.35, end=4.278, at="17:06:04"),
    Caption(id=2, text="短い", start=3661.5, end=3661.6, at="18:07:05"),
]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00:00,000"), (4.2785, "00:00:04,278"), (3661.5, "01:01:01,500"), (-0.1, "00:00:00,000")],
)
def test_srt_time(seconds: float, expected: str):
    assert srt_time(seconds) == expected


def test_srt_blocks_are_numbered_and_last_long_enough():
    assert to_srt(CAPTIONS) == (
        "1\n00:00:00,350 --> 00:00:04,278\n本日はお越しいただきありがとうございます。\n"
        "\n"
        "2\n01:01:01,500 --> 01:01:02,000\n短い\n"  # 0.1 秒の字幕は 0.5 秒まで延ばす
    )


def test_txt_matches_saved_transcript_format():
    assert to_txt(CAPTIONS) == "[17:06:04] 本日はお越しいただきありがとうございます。\n[18:07:05] 短い\n"


def test_empty():
    assert to_srt([]) == "" and to_txt([]) == ""
