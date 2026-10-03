import argparse
from pathlib import Path

import pytest

from realtime_transcriber.config import Config, add_cli_args, config_from_args, load_config

ROOT = Path(__file__).resolve().parents[1]


def parse(argv: list[str]) -> Config:
    ap = argparse.ArgumentParser()
    add_cli_args(ap)
    return config_from_args(ap.parse_args(argv))


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_file_gives_defaults_bound_to_localhost(tmp_path: Path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg == Config()
    assert cfg.host == "127.0.0.1"


def test_example_file_matches_defaults():
    assert load_config(ROOT / "config.example.toml") == Config()


def test_toml_values_are_loaded(tmp_path: Path):
    p = write(tmp_path, 'final_model = "large-v3"\nspeed = 2\nglossary = ["東雲電機", "汐留第二開発課"]\n')
    cfg = load_config(p)
    assert (cfg.final_model, cfg.speed, cfg.glossary) == ("large-v3", 2, ["東雲電機", "汐留第二開発課"])


def test_cli_overrides_toml_only_where_given(tmp_path: Path):
    p = write(tmp_path, 'speed = 2.0\nglossary = ["東雲電機"]\nport = 9000\n')
    cfg = parse(["--config", str(p), "--speed", "1.5", "--glossary", "A社, B部 ,"])
    assert cfg.speed == 1.5
    assert cfg.glossary == ["A社", "B部"]
    assert cfg.port == 9000  # CLI で指定していないので config.toml の値


@pytest.mark.parametrize(
    "text",
    [
        "spead = 1.5\n",  # タイプミス
        'speed = "fast"\n',
        "speed = 10.0\n",
        'glossary = "東雲電機"\n',
        "port = 0\n",
        "speed = \n",  # TOML の文法エラー
        'speed_restore = "no"\n',
        "speed_restore = 0\n",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, text: str):
    with pytest.raises(ValueError):
        load_config(write(tmp_path, text))


def test_example_has_the_line_start_server_sh_replaces():
    """scripts/start_server.sh は初回にこの行を large-v3 に置き換えて config.toml を作る"""
    lines = (ROOT / "config.example.toml").read_text(encoding="utf-8").splitlines()
    assert 'final_model = ""' in lines
