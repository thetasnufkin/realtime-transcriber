"""設定の読み込み。優先順位は 既定値 < config.toml < CLI 引数"""

from __future__ import annotations

import argparse
import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import tomli_w

DEFAULT_CONFIG_PATH = Path("config.toml")
EDITABLE_KEYS = ("model", "final_model", "speed", "glossary")  # ブラウザから変えられる設定
SAVED_HEADER = "# ブラウザの設定画面から保存した値を含む。各項目の説明は config.example.toml を参照\n\n"


@dataclass(frozen=True)
class Config:
    model: str = "kotoba-tech/kotoba-whisper-v2.0-faster"  # 暫定表示用（速さ重視）
    final_model: str = ""  # 確定用（精度重視）。空なら暫定用と同じモデル
    speed: float = 1.0  # 動画の再生速度
    # 倍速の音声を WSOLA で通常の速さに戻してから認識するか。ベンチマークでは戻すと CER が
    # 1.5 倍速で 8.9% → 18.9%、2 倍速で 17.4% → 38.0% に悪化したので既定は戻さない（bench/results）
    speed_restore: bool = False
    glossary: list[str] = field(default_factory=list)  # 固有名詞
    host: str = "127.0.0.1"  # この PC からしか届かないようにする
    port: int = 8765
    out_dir: str = "transcripts"

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if str(f.type) == "bool":
                if not isinstance(value, bool):
                    raise ValueError(f"設定 {f.name} は true / false にしてください: {value!r}")
                continue
            expected = {"float": (int, float), "int": int, "str": str}.get(str(f.type))
            if expected and (not isinstance(value, expected) or isinstance(value, bool)):
                raise ValueError(f"設定 {f.name} の型が違います: {value!r}")
        if not isinstance(self.glossary, list) or not all(isinstance(w, str) for w in self.glossary):
            raise ValueError(f"設定 glossary は文字列の配列にしてください: {self.glossary!r}")
        if not 0.5 <= self.speed <= 4.0:
            raise ValueError(f"設定 speed は 0.5〜4.0 にしてください: {self.speed}")
        if not 1 <= self.port <= 65535:
            raise ValueError(f"設定 port が範囲外です: {self.port}")


def load_config(path: Path) -> Config:
    """config.toml を読む。ファイルがなければ既定値"""
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    if unknown := sorted(set(data) - known):
        raise ValueError(f"{path} に未知の設定があります: {', '.join(unknown)}")
    return Config(**data)


def add_cli_args(ap: argparse.ArgumentParser) -> None:
    """config.toml を上書きする CLI 引数。指定しなかったものは config.toml の値を使う"""
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="設定ファイル")
    ap.add_argument("--model", help="暫定表示用（速さ重視）")
    ap.add_argument("--final-model", help="確定用（精度重視）。例: large-v3")
    ap.add_argument("--speed", type=float, help="動画の再生速度（1.5, 2.0 など）")
    ap.add_argument("--glossary", help="固有名詞をカンマ区切りで（例: 東雲電機,汐留第二開発課）")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--out-dir")


def overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """CLI で指定された設定だけを返す"""
    overrides = {
        f.name: getattr(args, f.name) for f in fields(Config) if getattr(args, f.name, None) is not None
    }
    if "glossary" in overrides:
        overrides["glossary"] = [w.strip() for w in overrides["glossary"].split(",") if w.strip()]
    return overrides


def config_from_args(args: argparse.Namespace) -> Config:
    return replace(load_config(args.config), **overrides_from_args(args))


def save_config(path: Path, updates: dict[str, Any]) -> None:
    """config.toml に updates の項目だけを書き込む（他の項目はファイルの値を残す）。
    コメントは残らないので、先頭に説明の場所を書いておく"""
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
    data.update(updates)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(SAVED_HEADER + tomli_w.dumps(data), encoding="utf-8")
    os.replace(tmp, path)  # 書き込み途中で落ちても壊れたファイルを残さない
