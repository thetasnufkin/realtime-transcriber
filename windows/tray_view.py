"""tray_view.py（Windows 側）トレイの見た目: 状態 → 色・説明文、アイコンの画像

pystray を読み込まないので、WSL の pytest でも検証できる。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from controller import Status

if TYPE_CHECKING:
    from PIL.Image import Image

APP_NAME = "リアルタイム字幕"
TOOLTIP_MAX = 127  # Windows の通知領域のツールチップの上限（128 文字未満）
FIRST_RUN_HINT = (
    "タスクバー右下の「^」（隠れているインジケーター）の中に常駐しています。"
    "クリックで字幕画面、右クリックでメニュー（終了もここから）"
)
COLORS = {
    "gray": (120, 113, 108),  # 起動中・読み込み中
    "green": (22, 163, 74),  # 受信中
    "orange": (217, 119, 6),  # 一時停止中
    "blue": (37, 99, 235),  # 取り込みオフ
    "red": (220, 38, 38),  # エラー
}


def describe(s: Status) -> tuple[str, str]:
    """(色, 説明文)。上にあるものほど優先"""
    if s.phase == "starting":
        return "gray", "起動中…"
    if s.phase == "stopping":
        return "gray", "終了中…"
    if s.phase == "stopped":
        return "gray", s.message or "停止しました"
    if s.phase == "failed":
        return "red", s.message
    if s.server == "loading":
        return "gray", "モデルを読み込み中…"
    if s.server == "error":
        return "red", "モデルを読み込めませんでした（字幕画面に詳細）"
    if s.capture == "error":
        return "red", s.message
    if s.paused:
        return "orange", "一時停止中"
    if s.capture == "off":
        return "blue", "音声の取り込みはオフ"
    if s.server == "switching":
        return "green", "モデルを切り替え中…（今のモデルで認識中）"
    if s.capture == "waiting":
        return "gray", "サーバーに接続中…"
    return "green", "受信中"


def tooltip(text: str) -> str:
    title = f"{APP_NAME} - {text}"
    return title if len(title) <= TOOLTIP_MAX else title[: TOOLTIP_MAX - 1] + "…"


def should_notify(prev: Status, new: Status) -> str | None:
    """通知を出すべき変化なら、その文面。失敗と取り込みのエラーは、なったときに 1 回だけ知らせる"""
    if new.phase == "failed" and prev.phase != "failed":
        return new.message
    if new.capture == "error" and prev.capture != "error":
        return new.message
    return None


def first_run_hint(flag: Path) -> str | None:
    """初めて起動したときだけ、トレイの場所の案内を返す（Windows 11 では新しいアイコンが隠れるため）"""
    if flag.exists():
        return None
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    return FIRST_RUN_HINT


def draw_icon(color: str, size: int = 64) -> Image:
    """色付きの角丸の四角に、字幕を表す白い横線 2 本"""
    from PIL import Image, ImageDraw

    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2 * s, 2 * s, 62 * s, 62 * s), radius=14 * s, fill=COLORS[color] + (255,))
    white = (255, 255, 255, 255)
    d.rounded_rectangle((13 * s, 25 * s, 51 * s, 32 * s), radius=3.5 * s, fill=white)
    d.rounded_rectangle((13 * s, 38 * s, 41 * s, 45 * s), radius=3.5 * s, fill=white)
    return img


def write_ico(path: str, color: str = "green") -> None:
    """ショートカット用の .ico（複数サイズ入り）"""
    draw_icon(color, 256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
