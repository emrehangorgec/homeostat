"""Self-contained HTML report for one experiment, rendered from the SQLite store.

No dependencies and no network at view time except Google Fonts (with fallbacks):
charts are inline SVG drawn to one scale, the tooltip is a few lines of inline JS.
"""

from __future__ import annotations

import html
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

from homeostat.eval.report import ARMS, CATEGORIES, UNSCORED, is_correct, summarize
from homeostat.store.sqlite import Store

OUTCOME_STYLE = {
    # outcome: (status role, glyph, label)
    "recovered": ("good", "circle", "recovered"),
    "escalated": ("warning", "triangle", "escalated to a human"),
    "observed_only": ("warning", "triangle", "observed only"),
    "undetected": ("critical", "square", "undetected"),
    "link_lost": ("unscored", "ring", "link lost (unscored)"),
    "not_healthy_at_start": ("unscored", "ring", "unhealthy baseline (unscored)"),
    "not_applicable": ("unscored", "ring", "fault not applicable (unscored)"),
}

CHECK_LABELS = {
    "foreground": "in front",
    "process": "process up",
    "ui_marker": "UI marker",
    "visual": "not blank",
    "stable": "stable",
    "responsive": "responsive",
}


def mask_serial(serial: str) -> str:
    """Reports get published: never print a real device serial. Simulator ids are not personal."""
    return serial if serial.startswith("sim-") else "masked"


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _glyph(shape: str, role: str, size: int = 10) -> str:
    h = size / 2
    cls = f"g-{role}"
    if shape == "circle":
        body = f'<circle cx="{h}" cy="{h}" r="{h - 1}" class="{cls}"/>'
    elif shape == "triangle":
        body = f'<path d="M{h} 1 L{size - 1} {size - 1} L1 {size - 1} Z" class="{cls}"/>'
    elif shape == "square":
        body = f'<rect x="1" y="1" width="{size - 2}" height="{size - 2}" class="{cls}"/>'
    else:
        body = f'<circle cx="{h}" cy="{h}" r="{h - 1.5}" class="g-ring"/>'
    return f'<svg class="glyph" width="{size}" height="{size}" viewBox="0 0 {size} {size}" aria-hidden="true">{body}</svg>'


def _chip(outcome: str, count: int | None = None) -> str:
    role, shape, label = OUTCOME_STYLE.get(outcome, ("unscored", "ring", outcome))
    text = f"{label} {count}" if count is not None else label
    return f'<span class="chip">{_glyph(shape, role)}{esc(text)}</span>'


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(value))
    for m in (1, 2, 2.5, 5, 10):
        if value <= m * exp:
            return m * exp
    return 10 * exp


def _ticks(maximum: float, count: int = 5) -> list[float]:
    step = maximum / count
    return [round(step * i, 6) for i in range(count + 1)]


def _fmt_s(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f} s"


# -- charts -------------------------------------------------------------------

LABEL_W, RIGHT_W, ROW_H, TOP, BOTTOM, WIDTH = 190, 150, 38, 14, 34, 900


def _label_w(labels) -> int:
    """Room for the longest row label (13 px monospace is about 8 px per character)."""
    return max(LABEL_W, 8 * max((len(label) for label in labels), default=0) + 20)


def _rate_chart(summaries: list) -> str:
    label_w = _label_w(s.key for s in summaries)
    plot_w = WIDTH - label_w - RIGHT_W
    height = TOP + ROW_H * len(summaries) + BOTTOM
    x = lambda p: label_w + p * plot_w  # noqa: E731
    parts = [f'<svg class="chart" viewBox="0 0 {WIDTH} {height}" role="img" '
             f'aria-label="Correct outcome rate per scenario with 95% Wilson intervals">']
    for t in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{TOP - 6}" y2="{height - BOTTOM + 4}" class="grid"/>')
        parts.append(f'<text x="{x(t):.1f}" y="{height - BOTTOM + 20}" class="tick" text-anchor="middle">{int(t * 100)}%</text>')
    for i, s in enumerate(summaries):
        cy = TOP + ROW_H * i + ROW_H / 2
        parts.append(f'<text x="0" y="{cy + 4:.1f}" class="row-label">{esc(s.key)}</text>')
        if s.n == 0:
            parts.append(f'<text x="{x(0):.1f}" y="{cy + 4:.1f}" class="muted-text">no scored runs</text>')
            continue
        lo, hi = s.ci
        tip = f"{s.key}: {s.correct}/{s.n} correct ({s.rate:.0%}), 95% CI {lo:.0%} to {hi:.0%}"
        parts.append(f'<line x1="{x(lo):.1f}" x2="{x(hi):.1f}" y1="{cy:.1f}" y2="{cy:.1f}" class="ci"/>')
        for edge in (lo, hi):
            parts.append(f'<line x1="{x(edge):.1f}" x2="{x(edge):.1f}" y1="{cy - 6:.1f}" y2="{cy + 6:.1f}" class="ci"/>')
        parts.append(f'<circle cx="{x(s.rate):.1f}" cy="{cy:.1f}" r="6" class="mark"/>')
        parts.append(f'<circle cx="{x(s.rate):.1f}" cy="{cy:.1f}" r="14" class="hit" tabindex="0" data-tip="{esc(tip)}"/>')
        parts.append(f'<text x="{WIDTH - RIGHT_W + 16}" y="{cy + 4:.1f}" class="value">'
                     f'{s.rate:.0%} <tspan class="muted-text">{s.correct}/{s.n}</tspan></text>')
    parts.append("</svg>")
    return "".join(parts)


def _strip_chart(rows: list[tuple[str, list[tuple[float, str]]]], unit_label: str, aria: str) -> str:
    values = [v for _, pts in rows for v, _ in pts]
    maximum = _nice_max(max(values) if values else 1.0)
    label_w = _label_w(key for key, _ in rows)
    plot_w = WIDTH - label_w - RIGHT_W
    height = TOP + ROW_H * len(rows) + BOTTOM
    x = lambda v: label_w + v / maximum * plot_w  # noqa: E731
    parts = [f'<svg class="chart" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="{esc(aria)}">']
    for t in _ticks(maximum):
        label = f"{t:g}"
        parts.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{TOP - 6}" y2="{height - BOTTOM + 4}" class="grid"/>')
        parts.append(f'<text x="{x(t):.1f}" y="{height - BOTTOM + 20}" class="tick" text-anchor="middle">{label}</text>')
    parts.append(f'<text x="{WIDTH - RIGHT_W + 16}" y="{height - BOTTOM + 20}" class="tick">{esc(unit_label)}</text>')
    for i, (key, pts) in enumerate(rows):
        cy = TOP + ROW_H * i + ROW_H / 2
        parts.append(f'<text x="0" y="{cy + 4:.1f}" class="row-label">{esc(key)}</text>')
        if not pts:
            parts.append(f'<text x="{x(0):.1f}" y="{cy + 4:.1f}" class="muted-text">no values</text>')
            continue
        for j, (v, tip) in enumerate(sorted(pts)):
            jitter = ((j * 7) % 5 - 2) * 2.4  # deterministic, keeps overlapping runs visible
            parts.append(f'<circle cx="{x(v):.1f}" cy="{cy + jitter:.1f}" r="4" class="dot"/>')
            parts.append(f'<circle cx="{x(v):.1f}" cy="{cy + jitter:.1f}" r="9" class="hit" tabindex="0" data-tip="{esc(tip)}"/>')
        med = statistics.median(v for v, _ in pts)
        parts.append(f'<line x1="{x(med):.1f}" x2="{x(med):.1f}" y1="{cy - 13:.1f}" y2="{cy + 13:.1f}" class="median"/>')
        parts.append(f'<text x="{WIDTH - RIGHT_W + 16}" y="{cy + 4:.1f}" class="value">median {med:.1f}</text>')
    parts.append("</svg>")
    return "".join(parts)


# -- sections -----------------------------------------------------------------

def _incident_story(store: Store, run: dict[str, Any], label: str | None = None) -> str:
    label = label or run["scenario_id"]
    incident = store.incident(run["incident_id"])
    actions = store.actions_for(run["incident_id"])
    if incident is None:
        return ""
    t0 = incident["detected_at"]
    symptoms = json.loads(incident["symptoms"])
    steps = []
    for a in actions:
        verify = json.loads(a["verify"]) if a["verify"] else None
        checks = ""
        if verify:
            pills = []
            for name, ok in verify["checks"].items():
                state = "unknown" if ok is None else ("pass" if ok else "fail")
                pills.append(f'<span class="check check-{state}">{esc(CHECK_LABELS.get(name, name))}'
                             f'<span class="sr"> {state}</span></span>')
            checks = f'<div class="checks">{"".join(pills)}</div>'
        changed = a["proposed"] != a["action"]
        proposal = (f'<span class="mono">{esc(a["proposed"])}</span> → ' if changed else "")
        steps.append(
            f'<li><span class="t mono">+{a["at"] - t0:.1f} s</span>'
            f'<div><div class="step-head"><span class="src mono">{esc(a["source"])}</span>'
            f'<span class="verdict v-{esc(a["verdict"])}">{esc(a["verdict"])}</span>'
            f'{proposal}<span class="mono strong">{esc(a["action"])}</span></div>'
            f'<div class="reason">{esc(a["reason"])}</div>{checks}</div></li>'
        )
    duration = (incident["closed_at"] or t0) - t0
    return (
        f'<article class="story"><header><h3>{esc(label)}</h3>{_chip(incident["outcome"])}'
        f'<span class="muted-text mono">incident {esc(incident["id"])} · {duration:.1f} s · '
        f'{incident["attempts"]} {"attempt" if incident["attempts"] == 1 else "attempts"} · '
        f'verify {esc(incident["verify_strength"] or "–")}</span></header>'
        f'<p class="symptoms">Symptoms at detection: '
        f'{", ".join(f"<span class=mono>{esc(s)}</span>" for s in symptoms)}</p>'
        f'<ol class="steps">{"".join(steps)}</ol></article>'
    )


def _pick_story_runs(runs: list[dict[str, Any]], key=lambda r: r["scenario_id"]) -> list[dict[str, Any]]:
    """One incident per row: an incorrect one if any, otherwise the slowest recovery."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        if r["incident_id"]:
            by[key(r)].append(r)
    picked = []
    for scenario_runs in by.values():
        wrong = [r for r in scenario_runs if not is_correct(r)]
        pool = wrong or scenario_runs
        picked.append(max(pool, key=lambda r: r["time_to_recovery_s"] or 0))
    return picked


def _diagnostician_section(store: Store, rows: dict[str, list[dict[str, Any]]]) -> str:
    """Model calls, latency, abstention, and confidence against the correct outcome.

    Calibration uses the first diagnosis of each incident against whether the run ended
    correctly: an observe step is right without resolving anything at once, so "did this
    step recover" is the wrong target.
    """
    table_rows, all_diagnoses = [], []
    for key, rs in rows.items():
        firsts = []
        for r in rs:
            if not r["incident_id"] or r["outcome"] in UNSCORED:
                continue
            ds = store.diagnoses_for(r["incident_id"])
            all_diagnoses += ds
            if ds and ds[0]["confidence"] is not None:
                firsts.append((ds[0]["confidence"], is_correct(r)))
        if not firsts:
            continue
        conf = statistics.mean(c for c, _ in firsts)
        acc = statistics.mean(1.0 if ok else 0.0 for _, ok in firsts)
        table_rows.append(
            f"<tr><th scope='row' class='mono'>{esc(key)}</th><td class='num'>{len(firsts)}</td>"
            f"<td class='num'>{conf:.2f}</td><td class='num'>{acc:.0%}</td>"
            f"<td class='num'>{conf - acc:+.2f}</td></tr>"
        )
    if not all_diagnoses:
        return ""
    models = sorted({d["model"] for d in all_diagnoses})
    latencies = [d["latency_s"] for d in all_diagnoses if d["latency_s"]]
    failures = sum(1 for d in all_diagnoses if d["diagnosis"] is None)
    abstentions = sum(1 for d in all_diagnoses if d["abstain"] == 1)
    cost = sum(d["cost_usd"] or 0.0 for d in all_diagnoses)
    summary = (
        f"{len(all_diagnoses)} model calls to {', '.join(models)}; {failures} failed, {abstentions} abstained. "
        f"Median latency {statistics.median(latencies):.1f} s. Cost ${cost:.2f}."
        if latencies else f"{len(all_diagnoses)} model calls."
    )
    return f"""
  <section>
    <div class="section-head">
      <h2>The diagnostician</h2>
      <p>{esc(summary)}</p>
      <p>For a calibrated model, confidence matches how often it is right. Each row compares the confidence of
      the first diagnosis in an incident with whether that run ended correctly.</p>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th scope="col">scenario</th><th scope="col">incidents</th><th scope="col">mean confidence</th>
      <th scope="col">correct</th><th scope="col">overconfidence</th></tr></thead>
      <tbody>{"".join(table_rows)}</tbody>
    </table></div>
  </section>
"""


def _select(store: Store, spec: str) -> list[dict[str, Any]]:
    """`experiment` or `experiment:scenario,scenario` (keep only those scenarios of that session)."""
    experiment, _, scenarios = spec.partition(":")
    runs = store.runs(experiment)
    if scenarios:
        keep = set(scenarios.split(","))
        runs = [r for r in runs if r["scenario_id"] in keep]
    return runs


def standalone(fragment: str) -> str:
    """A complete HTML document (doctype, charset, viewport) around a report fragment."""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{fragment}\n</html>\n"
    )


def render(store: Store, experiment_ids: str | list[str], name: str | None = None) -> str:
    """Render one report for one or more experiments (e.g. a scenario rerun in a later session).

    Each id may restrict scenarios, `m1-device-02:app_crash,wrong_foreground`, so a rerun
    with changed rules never shares a row with runs made under the old ones.
    """
    ids = [experiment_ids] if isinstance(experiment_ids, str) else list(experiment_ids)
    runs = [r for spec in ids for r in _select(store, spec)]
    if not runs:
        raise ValueError(f"no runs stored for experiments {ids!r}")
    experiment_id = name or " + ".join(ids)
    sessions = "; ".join(ids)

    first_incident = next((store.incident(r["incident_id"]) for r in runs if r["incident_id"]), None)
    device = json.loads(first_incident["first_state"])["device"] if first_incident else {}
    simulated = str(device.get("serial", "")).startswith("sim-")
    started = datetime.fromtimestamp(min(r["injected_at"] for r in runs))
    ended = datetime.fromtimestamp(max(r["injected_at"] for r in runs))

    arms = sorted({r["arm"] for r in runs}, key=lambda a: ARMS.index(a) if a in ARMS else len(ARMS))

    def row_key(r: dict[str, Any]) -> str:
        return r["scenario_id"] if len(arms) == 1 else f"{r['scenario_id']} · {r['arm']}"

    ordered = sorted(runs, key=lambda r: (r["scenario_id"], arms.index(r["arm"]))) if len(arms) > 1 else runs
    order = list(dict.fromkeys(row_key(r) for r in ordered))
    by_scenario = {k: [r for r in runs if row_key(r) == k] for k in order}
    summaries = [summarize(by_scenario[k], k) for k in order]
    scored = [r for r in runs if r["outcome"] not in UNSCORED]
    overall = summarize(runs, "all")
    unscored = len(runs) - len(scored)

    def tip(r: dict[str, Any], field: str) -> tuple[float, str]:
        return (r[field], f"{r['scenario_id']} run {r['id']}: {r[field]:.1f} s ({r['outcome']})")

    detect_rows = [(k, [tip(r, "detection_latency_s") for r in rs if r["detection_latency_s"] is not None])
                   for k, rs in by_scenario.items()]
    ttr_rows = [(k, [tip(r, "time_to_recovery_s") for r in rs if r["time_to_recovery_s"] is not None])
                for k, rs in by_scenario.items()]

    table_rows = []
    for s in summaries:
        category = by_scenario[s.key][0]["category"]
        chips = " ".join(_chip(o, c) for o, c in sorted(s.outcomes.items(), key=lambda kv: -kv[1]))
        lo, hi = s.ci
        rate = f"{s.rate:.0%} <span class='muted-text'>[{lo:.0%}, {hi:.0%}]</span>" if s.n else "–"
        table_rows.append(
            f"<tr><th scope='row' class='mono'>{esc(s.key)}</th><td>{esc(category)}</td>"
            f"<td class='num'>{s.n}</td><td class='num'>{rate}</td>"
            f"<td class='num'>{_fmt_s(s.median_detection_s)}</td><td class='num'>{_fmt_s(s.median_ttr_s)}</td>"
            f"<td class='num'>{s.excess_actions}</td>"
            f"<td class='num'>{s.llm_calls}{f' / ${s.cost_per_run:.3f}' if s.cost_usd else ''}</td>"
            f"<td>{chips}</td></tr>"
        )

    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        cells[(r["arm"], r["category"])].append(r)
    matrix_rows = []
    for arm in ARMS:
        tds = []
        for c in CATEGORIES:
            s = summarize(cells[(arm, c)], c)
            if s.n == 0:
                tds.append("<td class='empty'>not run</td>")
            else:
                lo, hi = s.ci
                tds.append(f"<td class='num'>{s.rate:.0%}<br><span class='muted-text'>[{lo:.0%}, {hi:.0%}] n={s.n}</span></td>")
        matrix_rows.append(f"<tr><th scope='row' class='mono'>{esc(arm)}</th>{''.join(tds)}</tr>")

    stories = "".join(_incident_story(store, r, row_key(r)) for r in _pick_story_runs(runs, row_key))
    diagnostician = _diagnostician_section(store, by_scenario)
    banner = (
        "<div class='banner'>Simulated device. These numbers exercise the code path; they are not device results.</div>"
        if simulated else ""
    )
    lo, hi = overall.ci
    title = f"homeostat · {experiment_id}"
    lede = (
        f"{overall.correct} of {overall.n} scored runs reached the correct outcome "
        f"({overall.rate:.0%}, 95% CI {lo:.0%} to {hi:.0%})."
        + (f" {unscored} runs were not scored because the harness, not the guardian, failed." if unscored else "")
    )

    return TEMPLATE.format(
        title=esc(title),
        experiment=esc(experiment_id),
        banner=banner,
        model=esc(device.get("model", "unknown device")),
        sdk=esc(device.get("sdk", "?")),
        serial=esc(mask_serial(str(device.get("serial", "?")))),
        window=f"{started:%Y-%m-%d %H:%M} to {ended:%H:%M}",
        arms=esc(", ".join(sorted({r["arm"] for r in runs}))),
        total=len(runs),
        sessions=esc(sessions),
        lede=esc(lede),
        rate_chart=_rate_chart(summaries),
        table_rows="".join(table_rows),
        detect_chart=_strip_chart(detect_rows, "seconds", "Detection latency per run, seconds"),
        ttr_chart=_strip_chart(ttr_rows, "seconds", "Time to verified recovery per run, seconds"),
        matrix_rows="".join(matrix_rows),
        matrix_head="".join(f"<th scope='col'>{esc(c)}</th>" for c in CATEGORIES),
        stories=stories,
        diagnostician=diagnostician,
    )


TEMPLATE = """<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root {{
  --bg: #f6f7f6; --surface: #ffffff; --ink: #111312; --ink-2: #4c524e; --muted: #7d837f;
  --rule: #dde1de; --grid: #e9ece9; --mark: #2a78d6; --mark-soft: #86b6ef;
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b; --unscored: #9aa09c;
  --pass-bg: #e3f4e3; --pass-ink: #0b5e0b; --fail-bg: #fbe4e4; --fail-ink: #9b2626;
  --banner-bg: #fff4d6; --banner-ink: #6b4a00;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --cond: "IBM Plex Sans Condensed", "Arial Narrow", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --bg: #141615; --surface: #1b1d1c; --ink: #eef0ee; --ink-2: #b9bfbb; --muted: #858c87;
    --rule: #2e3230; --grid: #252927; --mark: #3987e5; --mark-soft: #1c5cab;
    --pass-bg: #16301a; --pass-ink: #8fdc8f; --fail-bg: #3a1c1c; --fail-ink: #f0a0a0;
    --banner-bg: #3a2e10; --banner-ink: #f3d487;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --bg: #141615; --surface: #1b1d1c; --ink: #eef0ee; --ink-2: #b9bfbb; --muted: #858c87;
  --rule: #2e3230; --grid: #252927; --mark: #3987e5; --mark-soft: #1c5cab;
  --pass-bg: #16301a; --pass-ink: #8fdc8f; --fail-bg: #3a1c1c; --fail-ink: #f0a0a0;
  --banner-bg: #3a2e10; --banner-ink: #f3d487;
}}
body {{ background: var(--bg); color: var(--ink); font: 15px/1.55 var(--sans); }}
.page {{ max-width: 980px; margin: 0 auto; padding-inline: 20px; padding-block: 40px 72px;
  display: grid; gap: 44px; }}
h1, h2, h3 {{ font-family: var(--cond); font-weight: 600; text-wrap: balance; margin: 0; letter-spacing: -0.01em; }}
h1 {{ font-size: clamp(28px, 4.2vw, 40px); line-height: 1.1; }}
h2 {{ font-size: 22px; }}
h3 {{ font-size: 17px; }}
p {{ margin: 0; max-width: 68ch; }}
.mono {{ font-family: var(--mono); font-size: 0.92em; }}
.strong {{ font-weight: 500; }}
.eyebrow {{ font: 500 12px/1 var(--mono); letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }}
.muted-text {{ color: var(--muted); fill: var(--muted); }}
header.top {{ display: grid; gap: 14px; }}
.lede {{ font-size: 18px; color: var(--ink-2); }}
.spec {{ display: flex; flex-wrap: wrap; gap: 8px 28px; margin: 4px 0 0; padding: 14px 0 0; border-top: 1px solid var(--rule); }}
.spec div {{ display: grid; gap: 2px; }}
.spec dt {{ font: 500 11px/1.2 var(--mono); letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }}
.spec dd {{ margin: 0; font-family: var(--mono); font-size: 14px; font-variant-numeric: tabular-nums; }}
.banner {{ background: var(--banner-bg); color: var(--banner-ink); padding: 10px 14px; border-radius: 6px; font-weight: 500; }}
section {{ display: grid; gap: 14px; }}
.section-head {{ display: grid; gap: 6px; }}
.section-head p {{ color: var(--ink-2); }}
.figure {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; padding: 18px 18px 10px; overflow-x: auto; }}
.chart {{ display: block; width: 100%; min-width: 640px; height: auto; font-family: var(--mono); }}
.chart .grid {{ stroke: var(--grid); stroke-width: 1; }}
.chart .tick {{ fill: var(--muted); font-size: 12px; }}
.chart .row-label {{ fill: var(--ink); font-size: 13px; }}
.chart .value {{ fill: var(--ink); font-size: 13px; font-variant-numeric: tabular-nums; }}
.chart .ci {{ stroke: var(--ink-2); stroke-width: 2; stroke-linecap: round; }}
.chart .mark {{ fill: var(--mark); stroke: var(--surface); stroke-width: 2; }}
.chart .dot {{ fill: var(--mark); fill-opacity: 0.85; stroke: var(--surface); stroke-width: 2; }}
.chart .median {{ stroke: var(--ink); stroke-width: 2; stroke-linecap: round; }}
.chart .hit {{ fill: transparent; cursor: default; }}
.chart .hit:focus {{ outline: none; stroke: var(--ink); stroke-width: 1.5; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--ink-2); font-size: 13px; }}
.legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
.key-line {{ width: 18px; height: 2px; background: var(--ink-2); display: inline-block; }}
.key-dot {{ width: 10px; height: 10px; border-radius: 50%; background: var(--mark); display: inline-block; }}
.key-median {{ width: 2px; height: 14px; background: var(--ink); display: inline-block; }}
.table-wrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--rule); vertical-align: top; }}
thead th {{ font: 500 11px/1.2 var(--mono); letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }}
tbody tr:last-child th, tbody tr:last-child td {{ border-bottom: 0; }}
td.num {{ font-family: var(--mono); font-variant-numeric: tabular-nums; white-space: nowrap; }}
td.empty {{ color: var(--muted); font-style: italic; }}
.chip {{ display: inline-flex; align-items: center; gap: 6px; margin: 0 10px 2px 0; white-space: nowrap; font-size: 13px; }}
.glyph {{ flex: none; }}
.g-good {{ fill: var(--good); }} .g-warning {{ fill: var(--warning); }} .g-critical {{ fill: var(--critical); }}
.g-ring {{ fill: none; stroke: var(--unscored); stroke-width: 1.5; stroke-dasharray: 2 1.5; }}
.stories {{ display: grid; gap: 16px; }}
.story {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; padding: 18px 20px; display: grid; gap: 10px; }}
.story header {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; }}
.symptoms {{ color: var(--ink-2); font-size: 14px; }}
.steps {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 0; }}
.steps li {{ display: grid; grid-template-columns: 72px 1fr; gap: 12px; padding: 10px 0; border-top: 1px dashed var(--rule); }}
.steps .t {{ color: var(--muted); font-variant-numeric: tabular-nums; padding-top: 1px; }}
.step-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }}
.src {{ color: var(--ink-2); }}
.verdict {{ font: 500 11px/1 var(--mono); letter-spacing: 0.05em; text-transform: uppercase; padding: 4px 7px; border-radius: 4px; border: 1px solid var(--rule); color: var(--ink-2); }}
.v-escalate {{ border-color: var(--warning); }}
.v-substitute {{ border-color: var(--mark); }}
.reason {{ color: var(--muted); font-size: 13px; }}
.checks {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }}
.check {{ font: 500 12px/1 var(--mono); padding: 4px 7px; border-radius: 4px; }}
.check-pass {{ background: var(--pass-bg); color: var(--pass-ink); }}
.check-pass::before {{ content: "✓ "; }}
.check-fail {{ background: var(--fail-bg); color: var(--fail-ink); }}
.check-fail::before {{ content: "✗ "; }}
.check-unknown {{ border: 1px dashed var(--rule); color: var(--muted); }}
.check-unknown::before {{ content: "? "; }}
.sr {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }}
.notes {{ display: grid; gap: 10px; color: var(--ink-2); font-size: 14px; }}
.notes p strong {{ color: var(--ink); font-weight: 600; }}
#tip {{ position: fixed; pointer-events: none; background: var(--ink); color: var(--bg); font: 12px/1.4 var(--mono);
  padding: 6px 9px; border-radius: 5px; max-width: 320px; z-index: 10; }}
@media (max-width: 520px) {{ .steps li {{ grid-template-columns: 1fr; gap: 2px; }} }}
</style>

<main class="page">
  <header class="top">
    <span class="eyebrow">homeostat · experiment report</span>
    <h1>{experiment}</h1>
    {banner}
    <p class="lede">{lede}</p>
    <dl class="spec">
      <div><dt>device</dt><dd>{model}</dd></div>
      <div><dt>android api</dt><dd>{sdk}</dd></div>
      <div><dt>serial</dt><dd>{serial}</dd></div>
      <div><dt>window</dt><dd>{window}</dd></div>
      <div><dt>arm</dt><dd>{arms}</dd></div>
      <div><dt>runs</dt><dd>{total}</dd></div>
      <div><dt>sessions</dt><dd>{sessions}</dd></div>
    </dl>
  </header>

  <section>
    <div class="section-head">
      <h2>Correct outcome</h2>
      <p>A run is correct when it ends the way the fault expects, recovered or, when nothing on the device
      can fix it, escalated to a human, and no action was more disruptive than the fault needed. A run counts
      as recovered only when the healthy-state oracle confirms it and no symptom is left: the
      target is in front, its UI marker is present, the screen is not one flat color, it keeps the same process
      for the whole stability window, and an app that publishes heartbeats keeps publishing them.</p>
    </div>
    <div class="figure">{rate_chart}</div>
    <div class="legend"><span><i class="key-dot"></i>correct share of scored runs</span>
      <span><i class="key-line"></i>95% Wilson interval</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th scope="col">scenario</th><th scope="col">category</th><th scope="col">scored</th>
      <th scope="col">correct</th><th scope="col">median detection</th><th scope="col">median recovery</th>
      <th scope="col">unneeded disruptive</th><th scope="col">model calls / cost per run</th>
      <th scope="col">outcomes</th></tr></thead>
      <tbody>{table_rows}</tbody>
    </table></div>
  </section>

  <section>
    <div class="section-head">
      <h2>Detection latency</h2>
      <p>Seconds from fault injection to the guardian opening an incident. Faults land at a random point of the
      polling cycle, so this includes the wait for the next observation.</p>
    </div>
    <div class="figure">{detect_chart}</div>
    <div class="legend"><span><i class="key-dot"></i>one run</span><span><i class="key-median"></i>median</span></div>
  </section>

  <section>
    <div class="section-head">
      <h2>Time to verified recovery</h2>
      <p>Seconds from injection until the oracle confirmed health, recovered runs only. The stability window
      the oracle waits through is part of this time.</p>
    </div>
    <div class="figure">{ttr_chart}</div>
    <div class="legend"><span><i class="key-dot"></i>one recovered run</span><span><i class="key-median"></i>median</span></div>
  </section>

  <section>
    <div class="section-head">
      <h2>Results matrix</h2>
      <p>The table the project exists to fill. Rules are expected to win on known faults; the held-out column
      stays empty until rules, prompts and policy are frozen.</p>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th scope="col">arm</th>{matrix_head}</tr></thead>
      <tbody>{matrix_rows}</tbody>
    </table></div>
  </section>

{diagnostician}
  <section>
    <div class="section-head">
      <h2>Inside the loop</h2>
      <p>One incident per scenario, step by step: what the rules proposed, what the policy decided, and what the
      oracle checked afterwards. Times are seconds after detection at which each action was taken.
      An incorrect incident is shown when there is one, otherwise the slowest recovery.</p>
    </div>
    <div class="stories">{stories}</div>
  </section>

  <section class="notes">
    <h2>How to read this</h2>
    <p><strong>Unscored runs.</strong> A lost USB link or a baseline that was not healthy before injection says
    nothing about recovery. Those runs are listed but left out of every rate.</p>
    <p><strong>Unneeded disruptive actions.</strong> Each fault states the lowest action impact that fixes
    it. Actions above that (restarting an app whose backend is down, say) are counted here even when the run
    recovered.</p>
    <p><strong>Intervals.</strong> Rates carry 95% Wilson intervals. With 20 runs, 20 of 20 recovered still only
    supports a true rate above about 84%.</p>
    <p><strong>Scope.</strong> This is the rules-only baseline. Model-based arms are added from milestone M3.</p>
  </section>
</main>
<div id="tip" hidden></div>
<script>
(() => {{
  const tip = document.getElementById("tip");
  const show = (el, x, y) => {{
    tip.textContent = el.dataset.tip; tip.hidden = false;
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + "px";
    tip.style.top = Math.max(8, y - r.height - 10) + "px";
  }};
  document.querySelectorAll("[data-tip]").forEach(el => {{
    el.addEventListener("pointermove", e => show(el, e.clientX, e.clientY));
    el.addEventListener("pointerleave", () => {{ tip.hidden = true; }});
    el.addEventListener("focus", () => {{ const b = el.getBoundingClientRect(); show(el, b.right, b.top); }});
    el.addEventListener("blur", () => {{ tip.hidden = true; }});
  }});
}})();
</script>
"""
