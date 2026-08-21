from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .models import PRIORITY_ORDER, RunResult, SEVERITY_ORDER


def _escape(value: object) -> str:
    return html.escape(str(value or ""))


def _sparkline(points: list[dict[str, Any]]) -> str:
    values = [int(item.get("health_score", 0)) for item in points[-12:]]
    if len(values) < 2:
        return '<span class="muted">No trend yet</span>'
    width, height = 150, 34
    coords = [
        f"{index * width / (len(values) - 1):.1f},{height - max(0, min(100, value)) * height / 100:.1f}"
        for index, value in enumerate(values)
    ]
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Health score trend"><polyline points="{" ".join(coords)}"/></svg>'
    )


def _summary_payload(result: RunResult) -> dict[str, Any]:
    verdict_counts: dict[str, int] = defaultdict(int)
    for summary in result.project_summaries.values():
        verdict_counts[summary["verdict"]] += 1
    return {
        "started_at": result.started_at,
        "mode": result.mode,
        "offline": result.offline,
        "duration_seconds": round(result.duration_seconds, 2),
        "counts": result.counts(),
        "verdict_counts": dict(verdict_counts),
        "projects": result.project_summaries,
        "comparison": {
            key: value for key, value in result.comparison.items()
            if key not in {"history", "fixed_findings"}
        },
    }


def _group_findings(result: RunResult) -> dict[str, list]:
    grouped: dict[str, list] = defaultdict(list)
    for finding in sorted(
        result.findings,
        key=lambda item: (
            item.project, PRIORITY_ORDER.get(item.priority, 9),
            SEVERITY_ORDER.get(item.severity, 9), item.check,
        ),
    ):
        grouped[finding.project].append(finding)
    return grouped


def _finding_markup(finding, project_id: str) -> str:
    location = f'<code>{_escape(finding.path)}</code>' if finding.path else ""
    details = f"<pre>{_escape(finding.details)}</pre>" if finding.details else ""
    action = (
        f'<p class="action"><strong>Next action:</strong> {_escape(finding.next_action)}</p>'
        if finding.severity in {"error", "warning"} else ""
    )
    evidence = f"<details><summary>Evidence</summary>{details}</details>" if details else ""
    return f'''<article class="finding {finding.severity}" data-project="{_escape(project_id)}"
        data-priority="{_escape(finding.priority)}" data-change="{_escape(finding.change)}">
      <div class="finding-head"><span class="priority">{_escape(finding.priority)}</span>
      <strong>{_escape(finding.check)}</strong><span class="change">{_escape(finding.change)}</span>{location}</div>
      <p>{_escape(finding.message)}</p>{action}{evidence}</article>'''


def _project_markup(result: RunResult, grouped: dict[str, list], project_id: str, summary: dict) -> tuple[str, str]:
    comparison = result.comparison
    trend = comparison.get("history", {}).get(project_id, [])
    delta = comparison.get("projects", {}).get(project_id, {})
    card = f'''<a class="project-card verdict-{summary["verdict"].lower().replace(" ", "-")}" href="#project-{_escape(project_id)}">
      <div class="project-top"><span class="score">{summary["health_score"]}</span><span class="verdict">{_escape(summary["verdict"])}</span></div>
      <h3>{_escape(summary["name"])}</h3><p>{_escape(summary["profile"])}</p>
      <div class="project-stats"><span>{summary["counts"]["error"]} errors</span><span>{summary["counts"]["warning"]} warnings</span></div>
      {_sparkline(trend)}<small>{_escape(summary["top_issue"])}</small></a>'''
    findings = "".join(_finding_markup(item, project_id) for item in grouped.get(project_id, []))
    section = f'''<section id="project-{_escape(project_id)}" class="project-section">
      <div class="section-title"><div><h2>{_escape(summary["name"])}</h2>
      <p>{_escape(summary["reason"])}</p></div><span class="verdict">{_escape(summary["verdict"])}</span></div>
      <div class="delta"><span>New {delta.get("new", 0)}</span><span>Fixed {delta.get("fixed", 0)}</span>
      <span>Unchanged {delta.get("unchanged", 0)}</span><span>Regressions {delta.get("regressions", 0)}</span></div>
      {findings or '<p class="empty">No findings for this project.</p>'}</section>'''
    return card, section


def _styles() -> str:
    return """
:root{--bg:#f2f5f3;--card:#fff;--text:#13231a;--muted:#647269;--line:#d9e2dc;--brand:#0e5139}
@media(prefers-color-scheme:dark){:root{--bg:#0c1510;--card:#142019;--text:#edf7f0;--muted:#9dafa3;--line:#2b3b31;--brand:#0d412f}}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}
main{width:min(1220px,calc(100% - 28px));margin:24px auto 72px}header{padding:28px;border-radius:22px;color:#fff;background:linear-gradient(125deg,#0b3d2c,#127052)}
h1,h2,h3,p{margin-top:0}h1{margin-bottom:6px}.meta{color:#d5eadc;margin:0}.summary{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:16px 0}
.metric,.project-card,.finding,.project-section,.comparison{background:var(--card);border:1px solid var(--line);border-radius:15px}.metric{padding:14px}.metric strong{display:block;font-size:25px}
.controls{position:sticky;top:0;z-index:5;display:flex;gap:8px;flex-wrap:wrap;padding:12px 0;background:var(--bg)}button{border:1px solid var(--line);border-radius:999px;padding:8px 13px;background:var(--card);color:var(--text);cursor:pointer}button.active{background:var(--brand);color:white}
.projects{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.project-card{padding:16px;color:inherit;text-decoration:none;display:block}.project-card:hover{transform:translateY(-2px);border-color:#64a184}
.project-top,.project-stats,.section-title,.finding-head,.delta{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.project-top{justify-content:space-between}.score{font-size:30px;font-weight:800}.verdict{font-size:11px;font-weight:900;letter-spacing:.05em;padding:5px 8px;border-radius:999px;background:#dbe9df;color:#173e29}.verdict-not-ready{border-color:#e2a2a5}
.project-card p,.project-card small,.muted{color:var(--muted)}.project-card small{display:block;margin-top:8px}.spark{width:150px;height:34px;margin-top:9px}.spark polyline{fill:none;stroke:#38a16d;stroke-width:3}
.comparison{padding:18px;margin:18px 0}.comparison ul{margin-bottom:0}.project-section{padding:20px;margin-top:22px;scroll-margin-top:70px}.section-title{justify-content:space-between}.section-title p{color:var(--muted);margin-bottom:0}.delta{margin:12px 0;color:var(--muted)}
.finding{border-left:5px solid #688;padding:13px 15px;margin:9px 0}.finding.error{border-left-color:#d64045}.finding.warning{border-left-color:#df9b17}.finding.pass{border-left-color:#27945d}.finding.info{border-left-color:#4682b4}.priority,.change{text-transform:uppercase;font-size:10px;font-weight:900;letter-spacing:.06em}.priority{color:#b13c40}.change{color:var(--muted)}
.finding p{margin:7px 0 0}.action{color:var(--muted)}code{color:var(--muted)}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;border-radius:8px;background:var(--bg);max-height:340px;overflow:auto}details summary{cursor:pointer;margin-top:9px}.hidden{display:none!important}
@media(max-width:900px){.summary{grid-template-columns:repeat(3,1fr)}.projects{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){main{width:min(100% - 16px,1220px);margin-top:8px}header{padding:20px}.summary{grid-template-columns:repeat(2,1fr)}.projects{grid-template-columns:1fr}.project-section{padding:14px}}
"""


def _document(result: RunResult, cards: list[str], sections: list[str]) -> str:
    counts, comparison = result.counts(), result.comparison
    fixed = comparison.get("fixed_findings", [])
    fixed_markup = "".join(
        f'<li><strong>{_escape(item.get("project"))}</strong> · {_escape(item.get("check"))}: {_escape(item.get("message"))}</li>'
        for item in fixed[:40]
    ) or "<li>No fixed issues in this comparison.</li>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quality & Release Dashboard</title><style>{_styles()}</style></head><body><main>
<header><h1>Multi-Project Quality & Release Dashboard</h1><p class="meta">Mode: {_escape(result.mode)} · Offline: {_escape(result.offline)} · Duration: {result.duration_seconds:.1f}s · {_escape(result.started_at)}</p></header>
<div class="summary"><div class="metric"><span>Projects</span><strong>{len(result.project_summaries)}</strong></div><div class="metric"><span>Errors</span><strong>{counts["error"]}</strong></div><div class="metric"><span>Warnings</span><strong>{counts["warning"]}</strong></div><div class="metric"><span>Passed</span><strong>{counts["pass"]}</strong></div><div class="metric"><span>Fixed</span><strong>{comparison.get("fixed",0)}</strong></div><div class="metric"><span>Regressions</span><strong>{comparison.get("regressions",0)}</strong></div></div>
<nav class="controls" aria-label="Report filters"><button class="active" data-filter="all">All</button><button data-filter="critical">Critical</button><button data-filter="high">High</button><button data-filter="new">New</button><button data-filter="fixed">Fixed</button></nav>
<div class="projects">{"".join(cards)}</div><div class="comparison"><h2>Before / After</h2><p>Previous scan: {_escape(result.previous_scan or "No comparable scan yet")} · New {comparison.get("new",0)} · Fixed {comparison.get("fixed",0)} · Unchanged {comparison.get("unchanged",0)}</p><details><summary>Fixed issues</summary><ul>{fixed_markup}</ul></details></div>
{"".join(sections)}</main><script>const buttons=[...document.querySelectorAll("[data-filter]")];const findings=[...document.querySelectorAll(".finding")];buttons.forEach(button=>button.addEventListener("click",()=>{{buttons.forEach(item=>item.classList.remove("active"));button.classList.add("active");const value=button.dataset.filter;findings.forEach(item=>item.classList.toggle("hidden",value!=="all"&&item.dataset.priority!==value&&item.dataset.change!==value))}}));</script></body></html>"""


def write_reports(result: RunResult, reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = result.started_at.replace(":", "-").replace("+", "_")
    json_path = reports_dir / f"quality-{stamp}.json"
    html_path = reports_dir / f"quality-{stamp}.html"
    summary_path = reports_dir / f"quality-{stamp}.summary.json"
    json_path.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    summary_path.write_text(json.dumps(_summary_payload(result), indent=2, ensure_ascii=False), encoding="utf-8")
    grouped = _group_findings(result)
    pairs = [
        _project_markup(result, grouped, project_id, summary)
        for project_id, summary in result.project_summaries.items()
    ]
    document = _document(result, [pair[0] for pair in pairs], [pair[1] for pair in pairs])
    html_path.write_text(document, encoding="utf-8")
    (reports_dir / "latest.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (reports_dir / "latest.html").write_text(document, encoding="utf-8")
    (reports_dir / "latest-summary.json").write_text(summary_path.read_text(encoding="utf-8"), encoding="utf-8")
    return html_path, json_path
