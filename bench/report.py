"""ベンチマークの結果（results.json）から表（RESULTS.md）とグラフ（PNG）を作る

  python -m bench.report

グラフは README で GitHub のライト / ダーク表示に合わせて切り替えられるよう 2 枚書く。
色は 3 系列（倍速対応の方式）なので、検証済みのパレットの先頭 3 色を順に使う。
"""

from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path

ROOT = Path(__file__).parent
RESULTS = ROOT / "results" / "results.json"
OUT_MD = ROOT / "results" / "RESULTS.md"
OUT_PNG = ROOT / "results" / "speed_{mode}.png"
# Linux 側に日本語フォントがないため Windows のものを使う
FONT = Path("/mnt/c/Windows/Fonts/YuGothM.ttc")  # 游ゴシック Medium（英数字もプロポーショナル）

# 検証済みのパレット（ライト / ダークで段を変える。validate_palette.js で両方とも全項目 PASS）
# 系列の色は方式に固定する（どの条件を表示しても同じ方式は同じ色）
# ライトの 3 色目（#1baf7a）は面とのコントラストが 2.74:1 で WARN。
# そのため同じ値の表を必ず併記する（RESULTS.md・README）
THEMES = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7", "series": ["#2a78d6", "#eb6834", "#1baf7a"],
    },
    "dark": {
        "surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835", "series": ["#3987e5", "#d95926", "#199e70"],
    },
}  # fmt: skip
BASELINE = "kotoba + large-v3（既定）"
# 倍速対応の方式: (キー, 表の表記, グラフの凡例)
METHODS = [
    ("restore", "WSOLA で戻す", "WSOLA で通常の速さに戻す"),
    ("segment", "区切りだけ", "区切りだけ速度に合わせる（採用）"),
    ("none", "なし", "倍速対応なし"),
]


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def load() -> list[dict]:
    return json.loads(RESULTS.read_text(encoding="utf-8"))


def method(r: dict) -> str:
    if not r.get("speed_aware", True):
        return "none"
    return "restore" if r.get("stretch", True) else "segment"


def speed_rows(results: list[dict]) -> list[tuple[float, str, dict]]:
    """(速度, 方式, 結果)。1 倍速はどの方式でも同じなので、既定の構成の結果を全方式で使う"""
    by_name = {r["name"]: r for r in results}
    rows = []
    if BASELINE in by_name:
        rows += [(1.0, key, by_name[BASELINE]) for key, _, _ in METHODS]
    rows += [(r["speed"], method(r), r) for r in results if r["group"] == "speed"]
    return rows


def row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def with_range(r: dict, key: str) -> str:
    """平均と、複数回測ったときの範囲"""
    lo_hi = r.get(f"{key}_range")
    return pct(r[key]) + (f"（{pct(lo_hi[0])}〜{pct(lo_hi[1])}）" if lo_hi else "")


def timing(r: dict) -> list[str]:
    return [f"{r['latency_p50_s']:.2f} 秒", f"{r['latency_p90_s']:.2f} 秒", f"{r['rtf']:.2f}"]


def markdown(results: list[dict]) -> str:
    fleurs = next((r for r in results if r["dataset"] == "fleurs"), None)
    lines = ["# ベンチマーク結果", ""]
    if fleurs:
        lines += [
            f"- 音声: FLEURS 日本語 test（CC BY 4.0）の {fleurs['utterances']} 文を 1 秒の間でつないだ "
            f"約 {round(fleurs['audio_min'])} 分のストリーム（1 倍速）",
            f"- 環境: {fleurs['gpu']}、{fleurs['date']} 測定。`python -m bench.run` で再現できる",
            "- CER は句読点・記号・空白を除き、NFKC で全角半角を揃えてから計算",
            "- 遅延は「話し終わり（区間の終わり）から確定文が出るまで」。無音 0.6 秒の待ちを含む",
            "- RTF は処理時間 ÷ 音声の長さ（1 未満なら実時間に追いつく）",
            "",
        ]
    models = [r for r in results if r["group"] == "models"]
    if models:
        lines += [
            "## モデル構成（1 倍速）",
            "",
            "| 構成 | CER | 遅延 p50 | 遅延 p90 | RTF |",
            "|---|---:|---:|---:|---:|",
        ]
        lines += [row([r["name"], pct(r["cer"]), *timing(r)]) for r in models]
        lines.append("")
    speeds = speed_rows(results)
    if speeds:
        lines += [
            "## 倍速再生（kotoba + large-v3）",
            "",
            "- WSOLA で戻す: 無音の判定時間を速度で割り、認識の前に WSOLA で通常の速さに戻す",
            "- 区切りだけ: 無音の判定時間だけ速度で割り、倍速の音声のまま認識する",
            "- なし: 倍速の音声を等速として区切り・認識する",
            "- 倍速の音声は、ブラウザの倍速再生と同じくピッチを保ったまま速くしたもの（WSOLA）",
            "",
            "| 再生速度 | 倍速対応 | CER | 遅延 p50 | 遅延 p90 | RTF |",
            "|---:|---|---:|---:|---:|---:|",
        ]
        label = {key: text for key, text, _ in METHODS}
        order = {key: i for i, (key, _, _) in enumerate(METHODS)}
        if base := next((r for s, _, r in speeds if s == 1.0), None):
            lines.append(row(["1 倍", "（等速）", pct(base["cer"]), *timing(base)]))
        rest = sorted((x for x in speeds if x[0] != 1.0), key=lambda x: (x[0], order[x[1]]))
        lines += [row([f"{s:g} 倍", label[m], pct(r["cer"]), *timing(r)]) for s, m, r in rest]
        lines.append("")
    gloss = [r for r in results if r["group"] == "glossary"]
    if gloss:
        lines += [
            "## 用語集（kotoba + large-v3、1 倍速）",
            "",
            f"架空の社名・部署名を含む自作の {gloss[0]['utterances']} 文を"
            " Windows の音声合成（Haruka）で読み上げたもの（`bench/glossary/sentences.tsv`）。"
            "用語の正答率は、用語が正しい表記で出てきた割合。用語集の効果は用語の並び順で揺れるので、"
            "用語集ありは並び順を変えて 5 回測った平均（括弧内は最小〜最大）。",
            "",
            "| 条件 | 用語の正答率 | CER |",
            "|---|---:|---:|",
        ]
        lines += [row([r["name"], with_range(r, "term_recall"), with_range(r, "cer")]) for r in gloss]
        lines.append("")
    return "\n".join(lines)


def chart(results: list[dict], mode: str) -> Path | None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    rows = speed_rows(results)
    if not rows:
        return None
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)  # 游ゴシックの太さの代替の通知
    t = THEMES[mode]
    if FONT.exists():
        font_manager.fontManager.addfont(str(FONT))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(FONT)).get_name()
    else:
        warnings.warn(f"日本語フォントが見つかりません: {FONT}", stacklevel=1)

    speeds = sorted({s for s, _, _ in rows})
    value = {(s, m): r for s, m, r in rows}
    keys = [key for key, _, _ in METHODS]
    series = [(legend, key) for key, _, legend in METHODS if all((s, key) in value for s in speeds)]
    panels = [
        ("文字誤り率 CER（低いほど良い）", lambda r: r["cer"] * 100, "%"),
        ("確定までの遅延 中央値（秒）", lambda r: r["latency_p50_s"], "秒"),
    ]

    fig, axes = plt.subplots(1, 2, figsize=(9, 3.4), dpi=200, facecolor=t["surface"])
    width = 0.8 / len(series)
    for ax, (title, get, unit) in zip(axes, panels, strict=True):
        ax.set_facecolor(t["surface"])
        for k, (label, key) in enumerate(series):
            xs = [i + (k - (len(series) - 1) / 2) * width for i in range(len(speeds))]
            ys = [get(value[(s, key)]) for s in speeds]
            color = t["series"][keys.index(key)]  # 色は方式に固定
            # 隣り合う棒の間に面の色で 2px のすき間を作る
            ax.bar(xs, ys, width, color=color, edgecolor=t["surface"], linewidth=2, label=label, zorder=3)
        ax.set_xticks(range(len(speeds)), [f"{s:g} 倍速" for s in speeds], color=t["ink2"])
        ax.set_title(title, loc="left", fontsize=11, color=t["ink"], pad=10)
        ax.tick_params(axis="y", colors=t["muted"], labelsize=8, length=0)
        ax.tick_params(axis="x", length=0, labelsize=9)
        ax.yaxis.grid(True, color=t["grid"], linewidth=0.8, zorder=0)
        ax.yaxis.set_major_formatter(lambda v, _, u=unit: f"{v:g}{u}" if u == "%" else f"{v:g}")
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(t["axis"])
    handles, labels = axes[0].get_legend_handles_labels()
    leg = fig.legend(handles, labels, loc="upper center", ncol=len(series), frameon=False, fontsize=9)
    for text in leg.get_texts():
        text.set_color(t["ink2"])  # 凡例の文字は系列の色ではなく文字の色
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    out = Path(str(OUT_PNG).format(mode=mode))
    fig.savefig(out, facecolor=t["surface"])
    plt.close(fig)
    return out


def main() -> None:
    results = load()
    OUT_MD.write_text(markdown(results), encoding="utf-8")
    print(f"[REPORT] {OUT_MD}")
    for mode in THEMES:
        if out := chart(results, mode):
            print(f"[REPORT] {out}")


if __name__ == "__main__":
    main()
