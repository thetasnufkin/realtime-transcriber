"""windows/tray_view.py: トレイの色・説明文・通知・アイコン"""

from pathlib import Path

import pytest
from controller import Status
from PIL import Image
from tray_view import COLORS, TOOLTIP_MAX, describe, draw_icon, should_notify, tooltip, write_ico

RUNNING = Status(phase="running", server="ready", capture="streaming")


@pytest.mark.parametrize(
    ("status", "color", "text"),
    [
        (Status(), "gray", "起動中…"),
        (RUNNING, "green", "受信中"),
        (Status(phase="running", server="loading", capture="streaming"), "gray", "モデルを読み込み中…"),
        (Status(phase="running", server="ready", capture="waiting"), "gray", "サーバーに接続中…"),
        (Status(phase="running", server="ready", capture="off"), "blue", "音声の取り込みはオフ"),
        (Status(phase="running", server="ready", capture="streaming", paused=True), "orange", "一時停止中"),
        (
            Status(phase="running", server="switching", capture="streaming"),
            "green",
            "モデルを切り替え中…（今のモデルで認識中）",
        ),
        (
            Status(phase="running", server="error", capture="streaming"),
            "red",
            "モデルを読み込めませんでした（字幕画面に詳細）",
        ),
        (Status(phase="stopping"), "gray", "終了中…"),
        (Status(phase="failed", message="サーバーが停止しました"), "red", "サーバーが停止しました"),
    ],
)
def test_describe(status: Status, color: str, text: str):
    assert describe(status) == (color, text)


def test_errors_take_priority_over_pause():
    s = Status(
        phase="running", server="ready", capture="error", paused=True, message="音声を取り込めません: x"
    )
    assert describe(s) == ("red", "音声を取り込めません: x")


def test_tooltip_is_truncated_to_the_windows_limit():
    assert tooltip("受信中") == "リアルタイム字幕 - 受信中"
    long = tooltip("あ" * 300)
    assert len(long) == TOOLTIP_MAX and long.endswith("…")


def test_notify_once_when_it_fails_or_capture_breaks():
    failed = Status(phase="failed", message="サーバーが停止しました")
    assert should_notify(RUNNING, failed) == "サーバーが停止しました"
    assert should_notify(failed, failed) is None
    broken = Status(phase="running", server="ready", capture="error", message="音声を取り込めません")
    assert should_notify(RUNNING, broken) == "音声を取り込めません"
    assert should_notify(broken, broken) is None
    assert should_notify(Status(), RUNNING) is None


@pytest.mark.parametrize("color", list(COLORS))
def test_icon_is_the_state_color_with_white_lines(color: str):
    img = draw_icon(color)
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert img.getpixel((32, 10))[:3] == COLORS[color]  # 背景
    assert img.getpixel((32, 28)) == (255, 255, 255, 255)  # 字幕の線
    assert img.getpixel((0, 0))[3] == 0  # 角は透明


def test_write_ico_contains_small_and_large_sizes(tmp_path: Path):
    path = tmp_path / "app.ico"
    write_ico(str(path))
    with Image.open(path) as ico:
        assert {(16, 16), (32, 32), (256, 256)} <= ico.info["sizes"]
