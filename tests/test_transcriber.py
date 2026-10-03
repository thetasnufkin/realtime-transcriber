"""transcriber.py: モデルごとのプロンプトの渡し方、出力の後処理（GPU なし）"""

import pytest

from realtime_transcriber.transcriber import accepts_prompt, build_prompt, clean_text


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("large-v3", True),
        ("Systran/faster-whisper-large-v3", True),
        ("kotoba-tech/kotoba-whisper-v2.0-faster", False),  # 蒸留モデルには渡さない
        ("distil-large-v3", False),
        ("Systran/faster-distil-whisper-large-v3", False),
    ],
)
def test_accepts_prompt(name: str, expected: bool):
    assert accepts_prompt(name) is expected


def test_build_prompt():
    assert build_prompt(["東雲電機", "汐留第二開発課"], "直前の文", True) == (
        "東雲電機、汐留第二開発課。直前の文",
        "東雲電機 汐留第二開発課",
    )
    assert build_prompt([], "直前の文", True) == ("直前の文", None)
    assert build_prompt([], "", True) == (None, None)
    assert build_prompt(["東雲電機"], "直前の文", False) == (None, None)


def test_clean_text():
    assert clean_text("はいはいはいはいはい") == "はい"
    assert clean_text(" ご視聴ありがとうございました。 ") == ""
    assert clean_text("本日はよろしくお願いします。") == "本日はよろしくお願いします。"
