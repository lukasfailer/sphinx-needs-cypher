"""Render the benchmark results as an SVG line chart (log-log), styled for the
slide deck. Stdlib only — reads ``bench/results/results.csv``, writes
``slides/assets/bench-<workload>.svg``.

    python bench/plot.py                # workload B_antijoin (the headline query)
    python bench/plot.py A_flat
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SERIES = [
    # impl key            display label                        color      dash
    ("author_scan",       "Python · author filter_code",       "#ff8a5e", None),
    ("expert_index",      "Python · hand-indexed (expert)",    "#ffb45e", "6 4"),
    ("reference",         "Cypher · naive executor"    ,        "#9aa5ad", None),
    ("optimized",         "Cypher · + query planner",          "#e4ff3e", None),
    ("optimized_cython",  "Cypher · + Cython",                 "#7cff9b", "6 4"),
    ("bitmap",            "Cypher · + bitmap index",           "#2dd4bf", None),
    ("ubc_cli",           "ubc Rust · CLI end-to-end",         "#f06ad8", None),
    ("ubc_mcp",           "ubc Rust · warm local server",      "#f06ad8", "6 4"),
    ("neo4j",             "Neo4j · warm local server",         "#5c9eff", None),
]

W, H = 1000, 460
ML, MR, MT, MB = 64, 320, 20, 44  # right margin holds the series labels


def fmt_ms(v: float) -> str:
    if v >= 1000:
        return f"{v / 1000:.1f} s"
    if v >= 10:
        return f"{v:.0f} ms"
    if v >= 1:
        return f"{v:.1f} ms"
    if v >= 0.1:
        return f"{v:.2f} ms"
    return f"{v * 1000:.0f} µs"


def fmt_n(v: float) -> str:
    return f"{v / 1000:.0f}k" if v >= 1000 else f"{v:.0f}"


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("workload", nargs="?", default="B_antijoin")
    ap.add_argument("--results", default=str(ROOT / "bench/results/results.csv"),
                    help="results.csv to plot (default: bench/results/results.csv)")
    args = ap.parse_args()
    workload = args.workload
    rows = list(csv.DictReader(Path(args.results).open()))
    data: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        if r["workload"] != workload:
            continue
        data.setdefault(r["impl"], []).append((float(r["needs"]), float(r["ms_best"])))

    xs = sorted({x for pts in data.values() for x, _ in pts})
    ys = [y for pts in data.values() for _, y in pts]
    x0, x1 = math.log10(min(xs)), math.log10(max(xs))
    y0, y1 = math.log10(min(ys)), math.log10(max(ys))
    y0, y1 = math.floor(y0), math.ceil(y1)

    def X(v: float) -> float:
        return ML + (math.log10(v) - x0) / (x1 - x0) * (W - ML - MR)

    def Y(v: float) -> float:
        return H - MB - (math.log10(v) - y0) / (y1 - y0) * (H - MT - MB)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="JetBrains Mono, monospace" font-size="12">'
    ]
    # horizontal grid + y labels, one per decade
    for e in range(y0, y1 + 1):
        v = 10.0**e
        out.append(
            f'<line x1="{ML}" y1="{Y(v):.1f}" x2="{W - MR}" y2="{Y(v):.1f}" '
            f'stroke="#303030" stroke-width="1"/>'
            f'<text x="{ML - 8}" y="{Y(v) + 4:.1f}" fill="#a2a2a2" text-anchor="end">{fmt_ms(v)}</text>'
        )
    # x ticks at the measured sizes
    for v in xs:
        out.append(
            f'<line x1="{X(v):.1f}" y1="{H - MB}" x2="{X(v):.1f}" y2="{H - MB + 5}" '
            f'stroke="#303030"/>'
            f'<text x="{X(v):.1f}" y="{H - MB + 20}" fill="#a2a2a2" text-anchor="middle">{fmt_n(v)}</text>'
        )
    out.append(
        f'<text x="{(ML + W - MR) / 2:.0f}" y="{H - 6}" fill="#a2a2a2" '
        f'text-anchor="middle">needs in the project (log)</text>'
    )
    # series lines and points
    endpoints: list[tuple[float, float, str, str, float]] = []  # (end_y, end_x, label, color, value)
    for impl, label, color, dash in SERIES:
        pts = sorted(data.get(impl, []))
        if not pts:
            continue
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(x):.1f},{Y(y):.1f}" for i, (x, y) in enumerate(pts))
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.5"{dash_attr}/>')
        for x, y in pts:
            out.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="3.5" fill="{color}"/>')
        lx, ly = pts[-1]
        endpoints.append((Y(ly), X(lx), label, color, ly))
    # right-hand labels: laid out top-to-bottom in endpoint order, so the label
    # stack always matches the vertical order of the lines; a short connector
    # ties each label to its endpoint when it had to move.
    endpoints.sort(key=lambda e: e[0])
    placed: list[float] = []
    for ey, ex, label, color, val in endpoints:
        ty = ey if not placed else max(ey, placed[-1] + 17)
        placed.append(ty)
        if abs(ty - ey) > 8:  # label was displaced: draw a connector
            out.append(
                f'<line x1="{ex + 5:.1f}" y1="{ey:.1f}" x2="{ex + 10:.1f}" y2="{ty:.1f}" '
                f'stroke="{color}" stroke-width="1" opacity="0.6"/>'
            )
        out.append(
            f'<text x="{ex + 12:.1f}" y="{ty + 4:.1f}" fill="{color}">'
            f"{label}: {fmt_ms(val)}</text>"
        )
    # mark the headline gap: author code vs the bitmap executor, at the largest size
    author = dict(sorted(data.get("author_scan", [])))
    planned = dict(sorted(data.get("bitmap", [])))
    if author and planned:
        xm = max(author)
        if xm in planned:
            ya, yp = Y(author[xm]), Y(planned[xm])
            xg = X(xm) - 30
            ratio = author[xm] / planned[xm]
            step = 1000 if ratio >= 10000 else 100
            ratio_label = f"{round(ratio / step) * step:,}×".replace(",", " ")
            out.append(
                f'<line x1="{xg}" y1="{ya + 8:.1f}" x2="{xg}" y2="{yp - 8:.1f}" '
                f'stroke="#e4ff3e" stroke-width="2"/>'
                f'<path d="M{xg - 4},{ya + 8:.1f} L{xg + 4},{ya + 8:.1f} L{xg},{ya + 2:.1f} Z" fill="#e4ff3e"/>'
                f'<path d="M{xg - 4},{yp - 8:.1f} L{xg + 4},{yp - 8:.1f} L{xg},{yp - 2:.1f} Z" fill="#e4ff3e"/>'
                f'<text x="{xg - 10}" y="{(ya + yp) / 2 + 4:.1f}" fill="#e4ff3e" '
                f'text-anchor="end" font-weight="700">{ratio_label}</text>'
            )
    out.append("</svg>")

    dest = ROOT / f"slides/assets/bench-{workload}.svg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
