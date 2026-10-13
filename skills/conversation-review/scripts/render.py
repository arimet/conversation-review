#!/usr/bin/env python3
"""Render conversation-review facts and annotations as Markdown, HTML or PDF."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from html import escape as esc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import walk  # noqa: E402
from redact import Redactor  # noqa: E402

AXES = ("intention", "evidence", "control")
JUDGMENT_TYPES = {
    "intention": {"unclear_request"},
    "evidence": {"accepted_without_proof", "proof_present"},
    "control": {"unrequested_action"},
}
LEVELS = ("green", "orange", "red")
MODES = ("export", "audit", "review")
CRITERIA = ("correctness", "cost", "autonomy", "steps_context")
STYLES = ("centaur", "cyborg", "mixed")
SUMMARY_FIELDS = ("task", "task_event", "style", "style_reason", "strategy_check", "strategy_event", "story")
PREVIEW_LINES = 20


def _validate_phases(phases, top, errors):
    position = {event_id: i for i, event_id in enumerate(top)}
    if not isinstance(phases, list) or not phases:
        errors.append("phases: at least one phase is required")
        return
    expected = 0
    for i, phase in enumerate(phases):
        if not isinstance(phase, dict):
            errors.append(f"phases[{i}]: must be an object")
            continue
        if not phase.get("title"):
            errors.append(f"phases[{i}].title: required")
        first, last = phase.get("first_event"), phase.get("last_event")
        for field, event_id in (("first_event", first), ("last_event", last)):
            if event_id not in position:
                errors.append(f"phases[{i}].{field}: {event_id!r} is not a top-level event id")
        if first not in position or last not in position:
            continue
        if position[first] != expected:
            want = repr(top[expected]) if expected < len(top) else "no event (the previous phase ends the session)"
            errors.append(f"phases[{i}].first_event: expected {want} (phases must be contiguous and cover "
                          f"every event), got {first!r}")
        if position[last] < position[first]:
            errors.append(f"phases[{i}].last_event: {last!r} comes before first_event {first!r}")
        expected = position[last] + 1
    if not errors and expected != len(top):
        errors.append(f"phases[{len(phases) - 1}].last_event: must be {top[-1]!r}, the last event")


def _validate_summary(summary, all_ids, errors):
    if not summary:
        errors.append("summary: required (task, style, strategy check, story)")
        return
    if not isinstance(summary, dict):
        errors.append("summary: must be an object")
        return
    for key in SUMMARY_FIELDS:
        if not summary.get(key):
            errors.append(f"summary.{key}: required")
    if summary.get("style") and summary["style"] not in STYLES:
        errors.append("summary.style: must be centaur, cyborg or mixed")
    for key in ("task_event", "strategy_event"):
        if summary.get(key) and summary[key] not in all_ids:
            errors.append(f"summary.{key}: unknown event {summary[key]!r}")


def _validate_lights(section, name, keys, all_ids, errors):
    if not isinstance(section, dict):
        errors.append(f"{name}: must be an object")
        return
    for key in keys:
        entry = section.get(key) or {}
        if not isinstance(entry, dict):
            errors.append(f"{name}.{key}: must be an object")
            continue
        if entry.get("level") not in LEVELS:
            errors.append(f"{name}.{key}.level: must be green, orange or red")
        if not entry.get("reason"):
            errors.append(f"{name}.{key}.reason: required")
        if entry.get("event_id") not in all_ids:
            errors.append(f"{name}.{key}.event_id: unknown event {entry.get('event_id')!r}")


def validate(facts, annotations, mode):
    if not isinstance(annotations, dict):
        return ["annotations: must be a JSON object"]
    errors = []
    all_ids = {e["id"] for e in walk(facts["events"])}
    _validate_phases(annotations.get("phases") or [], [e["id"] for e in facts["events"]], errors)
    _validate_summary(annotations.get("summary"), all_ids, errors)
    for i, badge in enumerate(annotations.get("badges") or []):
        if not isinstance(badge, dict):
            errors.append(f"badges[{i}]: must be an object")
            continue
        if badge.get("event_id") not in all_ids:
            errors.append(f"badges[{i}].event_id: unknown event {badge.get('event_id')!r}")
        axis = badge.get("axis")
        if axis not in AXES:
            errors.append(f"badges[{i}].axis: must be one of {', '.join(AXES)}")
        elif badge.get("type") not in JUDGMENT_TYPES[axis]:
            errors.append(f"badges[{i}].type: {badge.get('type')!r} is not a {axis} badge "
                          f"(allowed: {', '.join(sorted(JUDGMENT_TYPES[axis]))})")
    if mode == "audit":
        for key in ("verdict", "goal", "improvements"):
            if annotations.get(key):
                errors.append(f"{key}: not allowed in audit mode")
    if mode == "review":
        for key, keys in (("verdict", AXES), ("goal", CRITERIA)):
            section = annotations.get(key)
            _validate_lights({} if section is None else section, key, keys, all_ids, errors)
        items = annotations.get("improvements") or []
        if not isinstance(items, list) or not 3 <= len(items) <= 5:
            errors.append(f"improvements: expected 3 to 5 items, got {len(items)}")
            items = items if isinstance(items, list) else []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"improvements[{i}]: must be an object")
                continue
            if item.get("event_id") not in all_ids:
                errors.append(f"improvements[{i}].event_id: unknown event {item.get('event_id')!r}")
            if item.get("axis") not in AXES:
                errors.append(f"improvements[{i}].axis: must be one of {', '.join(AXES)}")
            if item.get("criterion") not in CRITERIA:
                errors.append(f"improvements[{i}].criterion: must be one of {', '.join(CRITERIA)}")
            for key in ("problem", "better_prompt"):
                if not item.get(key):
                    errors.append(f"improvements[{i}].{key}: required")
    return errors


def _label(event, depth):
    if event["kind"] == "user_message":
        return "Main agent → subagent" if depth else "Developer"
    if event["kind"] == "assistant_message":
        return "Subagent" if depth else "Agent"
    return "System"


def _time(event):
    return event.get("timestamp", "")[11:16]


def _first_line(text):
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def _code(text):
    if not text:
        return ""
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def _metrics_line(m):
    cost = f"${m['cost_usd_estimate']:.2f}"
    if m.get("unpriced_messages"):
        cost += f" (+{m['unpriced_messages']} unpriced messages)"
    tokens_in = m["input_tokens"] + m["cache_read_tokens"] + m["cache_write_tokens"]
    return (f"Cost (estimate): {cost} · Tokens: {tokens_in:,} in / {m['output_tokens']:,} out · "
            f"Tool calls: {m['tool_calls']} · Files written: {m['files_written']} "
            f"(+{m['lines_added']} / -{m['lines_removed']})")


def _md_event(event, depth):
    pad = "  " * depth
    if event["kind"] in ("user_message", "assistant_message", "system"):
        if depth:
            return [f"{pad}- `{event['id']}` **{_label(event, depth)}**: {' '.join(event['text'].split())[:500]}"]
        return [f"### {event['id']} · {_label(event, depth)} · {_time(event)}", "", event["text"], ""]
    name = "Subagent" if event["kind"] == "subagent" else event["tool"]
    line = f"{pad}- `{event['id']}` **{name}** {_code(_first_line(event['input_summary']))}".rstrip()
    changes = ", ".join(f"{f['path']} +{f['added']}/-{f['removed']}" for f in event.get("files", []))
    line += f" ({changes})" if changes else ""
    line += " → error" if event.get("error") else ""
    lines = [line]
    if event.get("output"):
        output = event["output"].splitlines()
        lines += [f"{pad}  ~~~"] + [f"{pad}  {text}" for text in output[:PREVIEW_LINES]]
        if len(output) > PREVIEW_LINES:
            lines.append(f"{pad}  … {len(output) - PREVIEW_LINES} more lines")
        lines.append(f"{pad}  ~~~")
    for child in event.get("children", []):
        lines += _md_event(child, depth + 1)
    if depth == 0:
        lines.append("")
    return lines


def render_markdown(facts):
    s = facts["session"]
    lines = [
        f"# {s['title']}",
        "",
        f"- Date: {s['started_at'][:16].replace('T', ' ')} → {s['ended_at'][:16].replace('T', ' ')}",
        f"- Model: {s['model']} · Branch: {s['branch']} · Directory: {s['cwd']}",
        f"- {_metrics_line(facts['metrics'])}",
        "",
    ]
    lines += [f"> Warning: {w}" for w in facts["warnings"]]
    lines += ["---", ""]
    for event in facts["events"]:
        lines += _md_event(event, 0)
    return "\n".join(lines).rstrip() + "\n"


TEMPLATE = (Path(__file__).resolve().parent / "template.html").read_text(encoding="utf-8")
AXIS_LABELS = {"intention": "Intent", "evidence": "Evidence", "control": "Control"}
CRITERION_LABELS = {"correctness": "Correctly", "cost": "Cost", "autonomy": "Human dependency",
                    "steps_context": "Steps & context"}
GOAL_LINE = ("Goal: get the largest sensible task done correctly, at the lowest cost, with minimal human "
             "dependency, in few steps and with as little context as possible.")
CLOSING_LINE = "The conversation is the most important artifact."


def _axes(event, by_event):
    axes = {b["axis"] for b in by_event.get(event["id"], [])}
    for child in event.get("children", []):
        axes |= _axes(child, by_event)
    return axes


def _axis_classes(axes):
    return " ".join(f"has-{a}" for a in sorted(axes))


def _badge(badge):
    positive = " positive" if badge["type"] == "proof_present" else ""
    origin = "judgment" if badge.get("judgment") else "detected"
    return (f'<div class="badge axis-{esc(badge["axis"])}{positive}" data-axis="{esc(badge["axis"])}">'
            f'<span class="pill">{AXIS_LABELS[badge["axis"]]} · {esc(badge["type"].replace("_", " "))}</span>'
            f'<span class="origin">{origin}</span> {esc(badge.get("note", ""))}</div>')


def _event_html(event, by_event, depth):
    axes = _axes(event, by_event)
    classes = f'ev {event["kind"].replace("_", "-")} {_axis_classes(axes)}'.strip()
    badges = "".join(_badge(b) for b in by_event.get(event["id"], []))
    anchor = f'<a class="anchor" href="#{event["id"]}">{event["id"]}</a>'
    if event["kind"] in ("user_message", "assistant_message", "system"):
        return (f'<article id="{event["id"]}" class="{classes}"><header>{anchor} '
                f'<span class="who">{_label(event, depth)}</span><time>{esc(_time(event))}</time></header>'
                f'<div class="text">{esc(event["text"])}</div>{badges}</article>')
    name = "Subagent" if event["kind"] == "subagent" else event["tool"]
    changes = "".join(f' <span class="diff">{esc(f["path"])} <ins>+{f["added"]}</ins> <del>-{f["removed"]}</del></span>'
                      for f in event.get("files", []))
    error = ' <span class="error">error</span>' if event.get("error") else ""
    summary = (f'<summary>{anchor} <span class="tool">{esc(name)}</span> '
               f'<code>{esc(_first_line(event["input_summary"]))}</code>{changes}{error}</summary>')
    inner = ""
    if "\n" in event.get("input_summary", "").strip():
        inner += f'<pre class="input">{esc(event["input_summary"])}</pre>'
    if event.get("children"):
        inner += f'<div class="children">{_events_html(event["children"], by_event, depth + 1)}</div>'
    if event.get("output"):
        clamp = " clamp" if event.get("output_truncated") else ""
        inner += f'<pre class="out{clamp}">{esc(event["output"])}</pre>'
        inner += '<button class="more" type="button">Show all</button>' if clamp else ""
    is_open = " open" if badges else ""
    return f'<details id="{event["id"]}" class="{classes}"{is_open}>{summary}{badges}{inner}</details>'


def _events_html(events, by_event, depth=0):
    parts, i = [], 0
    while i < len(events):
        event, j = events[i], i
        if event["kind"] == "tool_call":
            while j + 1 < len(events) and events[j + 1]["kind"] == "tool_call" and events[j + 1]["tool"] == event["tool"]:
                j += 1
        if j > i:
            run = events[i:j + 1]
            axes = set().union(*(_axes(e, by_event) for e in run))
            dots = "".join(f'<span class="dot axis-{a}" title="{a}"></span>' for a in sorted(axes))
            is_open = " open" if axes else ""
            parts.append(f'<details class="ev group {_axis_classes(axes)}"{is_open}><summary>'
                         f'{esc(event["tool"])} ×{len(run)} {dots}</summary>'
                         + "".join(_event_html(e, by_event, depth) for e in run) + "</details>")
        else:
            parts.append(_event_html(event, by_event, depth))
        i = j + 1
    return "".join(parts)


def _goal_html(facts, badges, goal):
    m = facts["metrics"]
    tokens = m["input_tokens"] + m["cache_read_tokens"] + m["cache_write_tokens"] + m["output_tokens"]
    cost = f"${m['cost_usd_estimate']:.2f}" + ("+" if m.get("unpriced_messages") else "")
    figures = {
        "correctness": [
            ("Test or build runs", m["verification_runs"]),
            ("Accepted unverified", sum(1 for b in badges if b["type"] == "no_verification_since_edit")),
            ("Changed", f"+{m['lines_added']} / -{m['lines_removed']} in {m['files_written']} files")],
        "cost": [("Estimated cost", cost), ("Tokens", f"{tokens:,}")],
        "autonomy": [
            ("Developer turns", m["developer_turns"]),
            ("Denied tool calls", m["denials"]),
            ("Unclear requests", sum(1 for b in badges if b["type"] == "unclear_request"))],
        "steps_context": [
            ("Tool calls", m["tool_calls"]),
            ("Peak context", f"{m['peak_context_tokens']:,} tokens"),
            ("Compactions", m["compactions"])],
    }
    cards = []
    for key in CRITERIA:
        entry = (goal or {}).get(key)
        level = f' {esc(entry["level"])}' if entry else ""
        light = (f'<a class="goal-light" href="#{esc(entry["event_id"])}"><span class="level">{esc(entry["level"])}'
                 f'</span> {esc(entry["reason"])}</a>' if entry else "")
        rows = "".join(f"<li><span>{esc(k)}</span> <strong>{esc(str(v))}</strong></li>" for k, v in figures[key])
        cards.append(f'<li class="goal-card{level}"><h3>{esc(CRITERION_LABELS[key])}</h3>'
                     f'<ul class="figures">{rows}</ul>{light}</li>')
    return f'<p class="goal-line">{esc(GOAL_LINE)}</p><ul class="goal">{"".join(cards)}</ul>'


def _summary_html(summary):
    def link(event_id):
        return f' <a class="anchor" href="#{esc(event_id)}">{esc(event_id)}</a>'

    cards = [
        ("What is the task?", esc(summary["task"]) + link(summary["task_event"])),
        ("Centaur or cyborg?", f'<strong>{esc(summary["style"].title())}</strong>. {esc(summary["style_reason"])}'),
        ("How was the strategy checked?", esc(summary["strategy_check"]) + link(summary["strategy_event"])),
        ("What happened?", esc(summary["story"])),
    ]
    return ('<section class="summary" id="summary"><h2>Summary</h2><dl>'
            + "".join(f"<div><dt>{q}</dt><dd>{a}</dd></div>" for q, a in cards) + "</dl></section>")


def _header(facts, mode, annotations, badges):
    s = facts["session"]
    meta = " · ".join(esc(x) for x in (s["started_at"][:10], s["model"], s["branch"], s["cwd"]) if x)
    html = (f'<header class="report-head"><p class="eyebrow">Conversation review</p><h1>{esc(s["title"])}</h1>'
            f'<p class="meta">{meta}</p>')
    if mode == "export":
        return html + f'<p class="meta">{esc(_metrics_line(facts["metrics"]))}</p></header>'
    verdict = annotations.get("verdict") if mode == "review" else None
    if verdict:
        html += '<ul class="verdict">' + "".join(
            f'<li class="{esc(verdict[a]["level"])}"><a href="#{esc(verdict[a]["event_id"])}">'
            f'<strong>{AXIS_LABELS[a]}</strong><span class="level">{esc(verdict[a]["level"])}</span></a>'
            f'<p>{esc(verdict[a]["reason"])}</p></li>' for a in AXES) + "</ul>"
    html += _goal_html(facts, badges, annotations.get("goal") if mode == "review" else None)
    return html + _summary_html(annotations["summary"]) + "</header>"


def _sidebar(phases, badges, mode):
    html = '<aside class="sidebar"><nav>'
    if mode != "export":
        html += '<p><a href="#summary">Summary</a></p>'
    html += ('<h2>Phases</h2><ol>'
             + "".join(f'<li><a href="#phase-{n}">{esc(p["title"])}</a></li>' for n, p in enumerate(phases, 1))
             + "</ol>")
    if mode == "review":
        html += '<p><a href="#improvements">Improvements</a></p>'
    if mode != "export":
        counts = Counter(b["axis"] for b in badges)
        html += ('<h2>Badges</h2><div class="filters"><button type="button" data-filter="" aria-pressed="true">All</button>'
                 + "".join(f'<button type="button" class="axis-{a}" data-filter="{a}" aria-pressed="false">'
                           f'{AXIS_LABELS[a]} <span>{counts.get(a, 0)}</span></button>' for a in AXES) + "</div>")
    return html + "</nav></aside>"


def _phases_html(events, phases, by_event):
    index = {e["id"]: i for i, e in enumerate(events)}
    parts = []
    for n, phase in enumerate(phases, 1):
        chunk = events[index[phase["first_event"]]: index[phase["last_event"]] + 1]
        parts.append(f'<section class="phase" id="phase-{n}"><h2><span class="phase-n">{n}</span> {esc(phase["title"])}</h2>')
        if phase.get("summary"):
            parts.append(f'<p class="phase-summary">{esc(phase["summary"])}</p>')
        parts.append(_events_html(chunk, by_event) + "</section>")
    return "".join(parts)


def _improvements_html(items):
    return ('<section class="improvements" id="improvements"><h2>Improvements</h2><ol>'
            + "".join(f'<li><p><a href="#{esc(i["event_id"])}">{esc(i["event_id"])}</a> '
                      f'<span class="tag axis-{i["axis"]}">{AXIS_LABELS[i["axis"]]}</span> '
                      f'<span class="tag criterion">{esc(CRITERION_LABELS[i["criterion"]])}</span> '
                      f'{esc(i["problem"])}</p>'
                      f'<p class="label">A prompt that would have avoided it</p>'
                      f'<blockquote>{esc(i["better_prompt"])}</blockquote></li>' for i in items)
            + "</ol></section>")


def render_html(facts, annotations, mode):
    events = facts["events"]
    if mode == "export":
        phases = ([{"title": "Transcript", "first_event": events[0]["id"], "last_event": events[-1]["id"]}]
                  if events else [])
        badges, annotations = [], {}
    else:
        phases = annotations["phases"]
        badges = facts["factual_badges"] + [dict(b, judgment=True) for b in annotations.get("badges", [])]
    by_event = defaultdict(list)
    for badge in badges:
        by_event[badge["event_id"]].append(badge)
    warnings = "".join(f'<aside class="warning">{esc(w)}</aside>' for w in facts["warnings"])
    body = (_header(facts, mode, annotations, badges)
            + _sidebar(phases, badges, mode)
            + f'<main class="timeline">{warnings}{_phases_html(events, phases, by_event)}'
            + (_improvements_html(annotations.get("improvements", [])) if mode == "review" else "")
            + (f'<p class="closing">{esc(CLOSING_LINE)}</p>' if mode != "export" else "")
            + "</main>")
    values = {"TITLE": esc(facts["session"]["title"]), "BODY": body}
    return re.sub(r"\{\{(TITLE|BODY)\}\}", lambda match: values[match.group(1)], TEMPLATE)


CHROME_CANDIDATES = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


class PdfError(Exception):
    pass


def find_chrome():
    custom = os.environ.get("CHROME_PATH")
    if custom and os.path.exists(custom):
        return custom
    for candidate in CHROME_CANDIDATES:
        found = shutil.which(candidate) or (candidate if os.path.isabs(candidate) and os.path.exists(candidate) else None)
        if found:
            return found
    return None


def print_pdf(html, pdf_path):
    chrome = find_chrome()
    if not chrome:
        raise PdfError("no Chrome or Chromium found. Install Google Chrome, or set CHROME_PATH to a "
                       "Chromium-based browser.")
    pdf_path = Path(pdf_path).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "print.html"
        page.write_text(html.replace("<details", "<details open"), encoding="utf-8")
        result = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer", "--print-to-pdf-no-header",
             f"--print-to-pdf={pdf_path}", page.as_uri()],
            capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not pdf_path.exists():
        raise PdfError(f"Chrome could not print the PDF: {result.stderr.strip()[:500]}")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _map_strings(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    return value


def redact_annotations(annotations, home=None):
    """Claude writes annotations from a live, unredacted context: mask what it may have quoted."""
    redactor = Redactor(str(Path.home()) if home is None else home)
    for text in _strings(annotations):
        redactor.scan(text)
    return _map_strings(annotations, redactor.apply)


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--annotations")
    parser.add_argument("--out", required=True, help="Output path without extension")
    parser.add_argument("--pdf", action="store_true")
    args = parser.parse_args(argv)

    facts, annotations = _load(args.facts), None
    if args.mode != "export":
        if not args.annotations:
            parser.error("--annotations is required for audit and review")
        annotations = _load(args.annotations)
        errors = validate(facts, annotations, args.mode)
        annotations = redact_annotations(annotations) if not errors else annotations
        if errors:
            print("Invalid annotations:\n" + "\n".join(f"- {e}" for e in errors), file=sys.stderr)
            return 1
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    html = render_html(facts, annotations, args.mode)
    if args.mode == "export":
        target, content = Path(args.out + ".md"), render_markdown(facts)
    else:
        target, content = Path(args.out + ".html"), html
    target.write_text(content, encoding="utf-8")
    print(target)
    if args.pdf:
        try:
            print_pdf(html, args.out + ".pdf")
            print(args.out + ".pdf")
        except (PdfError, subprocess.TimeoutExpired) as error:
            print(f"PDF skipped: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
