"""評価を実行する（GPU を使う。source .venv/bin/activate の後に実行する）

  python -m bench.run                 # すべての条件
  python -m bench.run --only speed    # グループを絞る（models / speed / glossary）
  python -m bench.run --n 20          # FLEURS の文数を減らして試す

結果は bench/results/results.json に条件ごとに上書き保存する。表とグラフは python -m bench.report。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from bench import fleurs
from bench.metrics import cer, term_recall
from bench.simulate import simulate
from bench.stream import Stream, build_stream, speed_up
from bench.wavio import read_wav
from realtime_transcriber.config import Config
from realtime_transcriber.segmenter import Session
from realtime_transcriber.transcriber import WhisperTranscriber

ROOT = Path(__file__).parent
RESULTS = ROOT / "results" / "results.json"
OUT_DIR = ROOT / "data" / "out"  # 認識結果の全文（コミットしない）
GLOSSARY_TSV = ROOT / "glossary" / "sentences.tsv"
GLOSSARY_AUDIO = ROOT / "data" / "glossary"
KOTOBA = Config().model
LARGE = "large-v3"
DEFAULT_N = 100


@dataclass(frozen=True)
class Case:
    name: str
    group: str  # models / speed / glossary
    dataset: str  # fleurs / glossary
    model: str = KOTOBA
    final_model: str = LARGE
    speed: float = 1.0  # 再生速度（音声をこの速さにして流す）
    speed_aware: bool = True  # False: 倍速対応なし（等速として VAD・認識する）
    stretch: bool = True  # False: 無音の判定だけ速度に合わせ、WSOLA で戻さない
    glossary: bool = False


CASES = [
    Case("kotoba-whisper のみ", "models", "fleurs", final_model=""),
    Case("kotoba + large-v3（既定）", "models", "fleurs"),
    Case("1.5 倍速・対応あり", "speed", "fleurs", speed=1.5),
    Case("1.5 倍速・対応なし", "speed", "fleurs", speed=1.5, speed_aware=False),
    Case("1.5 倍速・区切りだけ対応", "speed", "fleurs", speed=1.5, stretch=False),
    Case("2 倍速・対応あり", "speed", "fleurs", speed=2.0),
    Case("2 倍速・対応なし", "speed", "fleurs", speed=2.0, speed_aware=False),
    Case("2 倍速・区切りだけ対応", "speed", "fleurs", speed=2.0, stretch=False),
    Case("用語集なし", "glossary", "glossary"),
    Case("用語集あり", "glossary", "glossary", glossary=True),
]


def load_glossary_set() -> tuple[list[tuple[str, np.ndarray]], list[str]]:
    """(正解の文, 音声) の並びと用語の一覧。音声がなければ Windows の音声合成で作る"""
    rows = [
        line.split("\t")
        for line in GLOSSARY_TSV.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not (GLOSSARY_AUDIO / f"{len(rows):02d}.wav").exists():
        make_glossary_audio()
    utterances = [
        (ref, read_wav(GLOSSARY_AUDIO / f"{i:02d}.wav")) for i, (ref, _, _) in enumerate(rows, start=1)
    ]
    terms = sorted({t for _, _, ts in rows for t in ts.split(",")})
    return utterances, terms


def make_glossary_audio() -> None:
    def win(p: Path) -> str:
        return subprocess.check_output(["wslpath", "-w", str(p)], text=True).strip()

    GLOSSARY_AUDIO.mkdir(parents=True, exist_ok=True)
    ps1 = ROOT / "glossary" / "make_tts.ps1"
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", win(ps1),
         "-Tsv", win(GLOSSARY_TSV), "-OutDir", win(GLOSSARY_AUDIO)],
        check=True,
    )  # fmt: skip


def gpu_name() -> str:
    try:
        return subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_case(case: Case, stream: Stream, tr: WhisperTranscriber, terms: list[str]) -> dict:
    tr.set_glossary(terms if case.glossary else [])
    played = speed_up(stream, case.speed)
    session = Session(tr, case.speed if case.speed_aware else 1.0, stretch=case.stretch)
    t0 = time.perf_counter()
    r = simulate(session, played.audio)
    wall = time.perf_counter() - t0
    p50, p90 = r.latency_percentiles(50, 90)
    result = {
        **asdict(case),
        "utterances": len(stream.segments),
        "audio_min": round(played.duration / 60, 2),
        "cer": round(cer(stream.reference, r.hypothesis), 4),
        "latency_p50_s": round(p50, 2),
        "latency_p90_s": round(p90, 2),
        "rtf": round(r.rtf, 3),
        "finals": len(r.finals),
        "wall_s": round(wall, 1),
        "gpu": gpu_name(),
        "date": dt.date.today().isoformat(),
    }
    if case.dataset == "glossary":
        result["term_recall"] = round(term_recall(terms, r.hypothesis), 3)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{case.name}.txt").write_text(
        "\n".join(e.event.text for e in r.finals) + "\n", encoding="utf-8"
    )
    return result


def save(results: list[dict]) -> None:
    """条件の名前ごとに上書きする（一部の条件だけ測り直せるように）"""
    old = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else []
    merged = {r["name"]: r for r in old} | {r["name"]: r for r in results}
    order = [c.name for c in CASES]
    rows = sorted(merged.values(), key=lambda r: order.index(r["name"]) if r["name"] in order else 99)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["models", "speed", "glossary"])
    ap.add_argument("--cases", nargs="+", help="条件の名前で絞る")
    ap.add_argument("--n", type=int, default=DEFAULT_N, help="FLEURS の文数")
    args = ap.parse_args()
    cases = [c for c in CASES if args.only in (None, c.group) and (not args.cases or c.name in args.cases)]

    streams: dict[str, Stream] = {}
    terms: list[str] = []
    if any(c.dataset == "fleurs" for c in cases):
        items = fleurs.load(args.n)
        streams["fleurs"] = build_stream([(it.text, it.audio) for it in items])
    if any(c.dataset == "glossary" for c in cases):
        utterances, terms = load_glossary_set()
        streams["glossary"] = build_stream(utterances)
    for name, s in streams.items():
        print(f"[DATA] {name}: {len(s.segments)} 発話 / {s.duration / 60:.1f} 分")

    models: dict[tuple[str, str], WhisperTranscriber] = {}
    results = []
    for case in cases:
        key = (case.model, case.final_model)
        if key not in models:
            models.clear()  # VRAM を空けてから読み込む
            models[key] = WhisperTranscriber(case.model, case.final_model or None, [])
        print(f"[RUN] {case.name} ...", flush=True)
        r = run_case(case, streams[case.dataset], models[key], terms)
        print(
            f"      CER {r['cer']:.2%} / 遅延 p50 {r['latency_p50_s']}s p90 {r['latency_p90_s']}s"
            f" / RTF {r['rtf']}" + (f" / 用語 {r['term_recall']:.0%}" if "term_recall" in r else ""),
            flush=True,
        )
        results.append(r)
        save(results)


if __name__ == "__main__":
    main()
