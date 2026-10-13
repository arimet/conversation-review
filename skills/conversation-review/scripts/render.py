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
from collections import defaultdict
from html import escape as esc
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import OUTPUT_PREVIEW_LINES as PREVIEW_LINES, _first_line, _map_strings, _strings, walk  # noqa: E402
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


def _code(text):
    if not text:
        return ""
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def _cost(m, verbose=False):
    """$2.29, with + (or the count, verbose) when some messages used a model with no known price."""
    unpriced = m.get("unpriced_messages")
    note = (f" (+{unpriced} unpriced messages)" if verbose else "+") if unpriced else ""
    return f"${m['cost_usd_estimate']:.2f}{note}"


def _tokens_in(m):
    return m["input_tokens"] + m["cache_read_tokens"] + m["cache_write_tokens"]


def _metrics_line(m):
    return (f"Cost (estimate): {_cost(m, verbose=True)} · Tokens: {_tokens_in(m):,} in / {m['output_tokens']:,} out · "
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
GOAL_LINE = ("The largest sensible task, done correctly, at the lowest cost, with minimal human dependency, "
             "in few steps and with as little context as possible.")
CLOSING_LINE = "The conversation is the most important artifact."
LEVEL_ICONS = {"green": "check", "orange": "alert", "red": "x"}
TOOL_ICONS = {"Bash": "terminal", "Read": "file", "Write": "file-plus", "Edit": "pencil", "MultiEdit": "pencil",
              "NotebookEdit": "pencil", "Grep": "search", "Glob": "search", "WebFetch": "globe",
              "WebSearch": "globe", "Agent": "agent", "Task": "agent"}


MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")  # http(s) only: no javascript: links
MD_HEADING = re.compile(r"(#{1,6})\s+(.*)")
MD_BULLET = re.compile(r"[-*+]\s+(.*)")
MD_NUMBER = re.compile(r"\d+[.)]\s+(.*)")
MD_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}")


def _inline(text):
    """Inline Markdown on already-escaped text: `code`, **bold**, [links](https://...)."""
    out = []
    for part in re.split(r"(`[^`\n]+`)", text):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{part[1:-1]}</code>")
        else:
            out.append(MD_LINK.sub(r'<a href="\2" rel="noopener">\1</a>', MD_BOLD.sub(r"<strong>\1</strong>", part)))
    return "".join(out)


def _cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _starts_block(line):
    s = line.strip()
    return (not s or s.startswith(("```", "|")) or MD_HEADING.match(s) or MD_BULLET.match(s)
            or MD_NUMBER.match(s) or s in ("---", "***"))


def _markdown(text):
    """Small, safe Markdown for message bodies. The text is escaped first, so no raw HTML gets through."""
    # ponytail: covers what agents write (paragraphs, lists, tables, fences, headings); nesting is flattened
    lines, html, i = esc(text).split("\n"), [], 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            code = "\n".join(lines[i + 1:j])
            html.append(f'<pre class="code">{code}</pre>')
            i = j + 1
        elif not s:
            i += 1
        elif MD_HEADING.match(s):
            html.append(f"<h4>{_inline(MD_HEADING.match(s).group(2))}</h4>")
            i += 1
        elif s.startswith("|") and i + 1 < len(lines) and MD_TABLE_RULE.match(lines[i + 1]):
            head = "".join(f"<th>{_inline(c)}</th>" for c in _cells(s))
            j, rows = i + 2, []
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(lines[j])) + "</tr>")
                j += 1
            html.append(f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
                        f'<tbody>{"".join(rows)}</tbody></table></div>')
            i = j
        elif MD_BULLET.match(s) or MD_NUMBER.match(s):
            pattern, tag = (MD_BULLET, "ul") if MD_BULLET.match(s) else (MD_NUMBER, "ol")
            items = []
            while i < len(lines) and pattern.match(lines[i].strip()):
                items.append(f"<li>{_inline(pattern.match(lines[i].strip()).group(1))}</li>")
                i += 1
            html.append(f"<{tag}>{''.join(items)}</{tag}>")
        elif s in ("---", "***"):
            html.append("<hr>")
            i += 1
        else:
            para = [s]
            i += 1
            while i < len(lines) and not _starts_block(lines[i]):
                para.append(lines[i].strip())
                i += 1
            html.append(f"<p>{'<br>'.join(_inline(x) for x in para)}</p>")
    return "".join(html)


GLOSSARY = [
    ("The three questions", [
        ("intention", "Intent", "Did the developer state the problem clearly: the expected result, its limits, and when "
                                "to stop?"),
        ("evidence", "Evidence", "What did the developer rely on to accept the result: a test run, a build, a read of "
                                 "the code, or nothing?"),
        ("control", "Control", "Did the developer keep control of the scope, the context, the tools and the "
                               "permissions the agent used?"),
    ]),
    ("The goal", [
        ("correctness", "Correctly", "The result is right, and something in the session proves it."),
        ("cost", "Cost", "Spend proportionate to the task, estimated from token usage and public API prices."),
        ("autonomy", "Human dependency", "How often the developer had to step in, correct or unblock the agent. "
                                         "Less is better."),
        ("steps_context", "Steps & context", "How many steps the work took and how much context it filled. "
                                             "Fewer and less is better."),
    ]),
    ("Conversation style", [
        ("centaur", "Centaur", "The developer keeps the plan and hands the agent long, bounded tasks: the human "
                               "decides, the agent executes."),
        ("cyborg", "Cyborg", "Developer and agent work in short, interleaved steps, the developer steering every move."),
        ("mixed", "Mixed", "The session switches between the centaur and the cyborg styles."),
        ("strategy", "Strategy check", "How the technical approach was validated before code was written: a reviewed "
                                       "plan, compared alternatives, a throwaway spike."),
    ]),
    ("Verdicts", [
        ("green", "Met", "Nothing on this point changed the outcome."),
        ("orange", "Partly met", "A problem the developer caught, or one with little effect on the result."),
        ("red", "Not met", "A problem that shaped the result."),
    ]),
    ("Badges", [
        ("detected", "Detected", "Computed by the script from the transcript: a fact, not an opinion."),
        ("judgment", "Written by Claude", "A judgment Claude wrote while reviewing. Each one links to the moment it "
                                          "is about."),
        ("outside_scope", "Outside scope", "The agent read or wrote outside the repository: another folder, the home "
                                           "directory, node_modules."),
        ("destructive_command", "Destructive command", "A command that deletes data or rewrites history, such as "
                                                       "rm -rf, git push --force or git reset --hard."),
        ("permission", "Permission", "A tool call was refused, or tool calls ran without asking because auto mode "
                                     "was on."),
        ("cost_spike", "Cost spike", "One developer turn cost more than a quarter of the whole session."),
        ("no_verification_since_edit", "No verification since edit", "Edits were accepted, committed or left at the "
                                                                      "end with no test, build or re-read since."),
        ("unclear_request", "Unclear request", "The request left out the expected result or its limits, or had to be "
                                               "reframed."),
        ("accepted_without_proof", "Accepted without proof", "A result was accepted with nothing showing it works."),
        ("proof_present", "Proof present", "A test, a build or a manual check backed the acceptance."),
        ("unrequested_action", "Unrequested action", "The agent edited, installed, committed or pushed when it was "
                                                     "asked for a plan, a question or a strategy."),
    ]),
    ("Figures", [
        ("verification_runs", "Test or build runs", "Commands that ran tests, a build or a linter. A commit whose "
                                                    "message mentions tests does not count."),
        ("accepted_unverified", "Accepted unverified", "Times edits were accepted with no check since the last change."),
        ("changed", "Changed", "Lines added and removed, and the number of files touched."),
        ("estimated_cost", "Estimated cost", "Token usage multiplied by public API prices. A + means some messages "
                                             "used a model with no known price."),
        ("tokens", "Tokens", "Every token sent to and returned by the model, cache reads included."),
        ("developer_turns", "Developer turns", "Messages the developer typed. Compaction summaries and system "
                                               "messages are not counted."),
        ("denials", "Denied tool calls", "Tool calls the developer or a hook refused."),
        ("unclear_requests", "Unclear requests", "Requests Claude marked as unclear."),
        ("tool_calls", "Tool calls", "Every tool the agent used: shell commands, file reads and writes, searches, "
                                     "subagents."),
        ("peak_context", "Peak context", "The largest amount of context sent to the model in a single call."),
        ("compactions", "Compactions", "Times Claude Code summarized the conversation to free context."),
        ("event_id", "e12", "Every moment of the conversation has a number. Click one to jump to it, or copy the "
                            "page link to cite it in a pull request comment."),
    ]),
]
TERMS = {key: (label, text) for _, entries in GLOSSARY for key, label, text in entries}
AXIS_LABELS = {key: TERMS[key][0] for key in AXES}
CRITERION_LABELS = {key: TERMS[key][0] for key in CRITERIA}
LEVEL_LABELS = {key: TERMS[key][0] for key in LEVELS}


def _term(key, label=None):
    """A defined word: dotted underline, definition on hover or focus, click jumps to the glossary."""
    if key not in TERMS:
        return esc(label or key)
    return f'<span class="term" tabindex="0" data-term="{key}">{esc(label or TERMS[key][0])}</span>'


def _glossary_html():
    groups = "".join(
        f'<div class="gloss-group"><h3>{esc(title)}</h3><dl>'
        + "".join(f'<div id="term-{key}"><dt>{esc(label)}</dt><dd>{esc(text)}</dd></div>' for key, label, text in entries)
        + "</dl></div>" for title, entries in GLOSSARY)
    return (f'<section class="glossary" id="glossary"><h2 class="section-title">How to read this report</h2>'
            f'<p class="glossary-intro">Hover any dotted word for its definition. Blue marks what the script detected '
            f'in the transcript, purple what Claude wrote while reviewing.</p><div class="gloss-grid">{groups}</div>'
            f'</section>')


def _pretty_model(model):
    """claude-opus-5-5[1m] -> Claude Opus 5.5; anything else is shown as is."""
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d+))?", model or "")
    if not m:
        return model or ""
    version = m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
    return f"Claude {m.group(1).title()} {version}"


def _pretty_date(timestamp):
    try:
        return datetime.strptime(timestamp[:10], "%Y-%m-%d").strftime("%-d %b %Y")
    except ValueError:
        return timestamp[:10]


def _icon(name, cls="icon"):
    return f'<svg class="{cls}" aria-hidden="true"><use href="#i-{name}"/></svg>'


def _anchor(event_id):
    return f'<a class="anchor" href="#{esc(event_id)}">{esc(event_id)}</a>'


def _axes(event, by_event):
    axes = {b["axis"] for b in by_event.get(event["id"], [])}
    for child in event.get("children", []):
        axes |= _axes(child, by_event)
    return axes


def _badge(badge):
    judgment = bool(badge.get("judgment"))
    kind = "judgment" if judgment else "detected"
    positive = " positive" if badge["type"] == "proof_present" else ""
    icon = "check" if positive else ("sparkle" if judgment else "scan")
    return (f'<div class="badge {kind}{positive}">'
            f'<p class="badge-head">{_icon(icon)}<strong>{_term(badge["axis"], AXIS_LABELS[badge["axis"]])}</strong>'
            f'<span>{_term(badge["type"], badge["type"].replace("_", " "))}</span>'
            f'<span class="origin">{_term(kind, "Claude" if judgment else "Detected")}</span></p>'
            f'<p class="badge-note">{esc(badge.get("note", ""))}</p></div>')


def _message_html(event, by_event, depth, classes, badges):
    who = _label(event, depth)
    if event["kind"] == "system":
        return (f'<div id="{event["id"]}" class="{classes}"><div class="rail-icon">{_icon("fold")}</div>'
                f'<p class="system-line">{esc(event["text"])} {_anchor(event["id"])}</p>{badges}</div>')
    # Purple and the sparkle stay Claude-the-reviewer's mark; the agent in the transcript is ink.
    avatar = ("avatar-dev", "person") if event["kind"] == "user_message" and not depth else ("avatar-agent", "agent")
    return (f'<article id="{event["id"]}" class="{classes}">'
            f'<div class="avatar {avatar[0]}">{_icon(avatar[1])}</div>'
            f'<div class="comment-col"><div class="comment">'
            f'<header class="comment-head"><strong>{who}</strong><time>{esc(_time(event))}</time>'
            f'{_anchor(event["id"])}</header>'
            f'<div class="text md">{_markdown(event["text"])}</div></div>{badges}</div></article>')


def _event_html(event, by_event, depth):
    classes = f'ev {event["kind"].replace("_", "-")}'
    badges = "".join(_badge(b) for b in by_event.get(event["id"], []))
    if event["kind"] in ("user_message", "assistant_message", "system"):
        return _message_html(event, by_event, depth, classes, badges)
    name = "Subagent" if event["kind"] == "subagent" else event["tool"]
    changes = "".join(f'<span class="diff" title="{esc(f["path"])}"><ins>+{f["added"]}</ins><del>−{f["removed"]}</del>'
                      f'<span class="path">{esc(f["path"])}</span></span>' for f in event.get("files", []))
    error = '<span class="error">failed</span>' if event.get("error") else ""
    summary = (f'<summary class="event-row">{_icon(TOOL_ICONS.get(event["tool"], "tool"), "icon rail-icon")}'
               f'<span class="tool">{esc(name)}</span>'
               f'<code class="call">{esc(_first_line(event["input_summary"]))}</code>{changes}{error}'
               f'{_anchor(event["id"])}</summary>')
    inner = badges
    if "\n" in event.get("input_summary", "").strip():
        inner += f'<pre class="input">{esc(event["input_summary"])}</pre>'
    if event.get("children"):
        inner += f'<div class="thread nested">{_events_html(event["children"], by_event, depth + 1)}</div>'
    if event.get("output"):
        clamp = " clamp" if event.get("output_truncated") else ""
        inner += f'<pre class="out{clamp}">{esc(event["output"])}</pre>'
        inner += '<button class="more" type="button">Show all</button>' if clamp else ""
    is_open = " open" if badges else ""
    return (f'<details id="{event["id"]}" class="{classes}"{is_open}>{summary}'
            f'<div class="event-body">{inner}</div></details>')


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
            flags = "".join(f'<span class="flag">{AXIS_LABELS[a]}</span>' for a in sorted(axes))
            is_open = " open" if axes else ""
            parts.append(f'<details class="ev group"{is_open}><summary class="event-row">'
                         f'{_icon(TOOL_ICONS.get(event["tool"], "tool"), "icon rail-icon")}'
                         f'<span class="tool">{esc(event["tool"])} ×{len(run)}</span>'
                         f'<span class="range">{run[0]["id"]}–{run[-1]["id"]}</span>{flags}</summary>'
                         f'<div class="group-body">' + "".join(_event_html(e, by_event, depth) for e in run)
                         + "</div></details>")
        else:
            parts.append(_event_html(event, by_event, depth))
        i = j + 1
    return "".join(parts)


def _masthead(facts, mode, annotations):
    s, m = facts["session"], facts["metrics"]
    count = sum(1 for _ in walk(facts["events"]))
    meta = [f'<span class="path">{esc(s["cwd"])}</span>'] if s["cwd"] else []
    if s["branch"] and s["branch"] != "HEAD":
        meta.append(f'<span class="branch">{_icon("branch")}{esc(s["branch"])}</span>')
    meta += [f"<span>{esc(x)}</span>" for x in (_pretty_model(s["model"]), _pretty_date(s["started_at"]),
                                                 f"{count} events", _cost(m)) if x]
    meta_html = "".join(meta)
    tips = [("improvements", "Tips", len(annotations.get("improvements", [])))] if mode == "review" else []
    tabs = ([("conversation", "Conversation", count)] if mode == "export"
            else [("overview", "Overview", None)] + tips + [("conversation", "Conversation", count)])
    tab_html = "".join(f'<a href="#{t}" data-tab="{t}">{label}'
                       + (f'<span class="counter">{n}</span>' if n is not None else "") + "</a>"
                       for t, label, n in tabs)
    help_link = (f'<a class="help-link" href="#glossary">{_icon("help")}How to read this report</a>'
                 if mode != "export" else "")
    return (f'<header class="masthead"><div class="masthead-inner"><h1>{esc(s["title"])}</h1>'
            f'<p class="meta">{meta_html}{help_link}</p></div></header>'
            f'<nav class="tabs" aria-label="Sections"><div class="tabs-inner">{tab_html}</div></nav>')


def _review_html(verdict):
    rows = "".join(
        f'<li class="check {esc(verdict[a]["level"])}">{_icon(LEVEL_ICONS[verdict[a]["level"]], "icon status")}'
        f'<div><p class="check-title"><strong>{_term(a, AXIS_LABELS[a])}</strong>'
        f'<span class="level">{_term(verdict[a]["level"], LEVEL_LABELS[verdict[a]["level"]])}</span>'
        f'{_anchor(verdict[a]["event_id"])}</p>'
        f'<p>{esc(verdict[a]["reason"])}</p></div></li>' for a in AXES)
    return (f'<section class="box review-box judgment-box"><header class="box-head">{_icon("sparkle")}'
            f'<strong>Claude reviewed this conversation</strong></header><ul class="verdict">{rows}</ul></section>')


def _goal_html(facts, badges, goal):
    m = facts["metrics"]
    figures = {  # (glossary key, value): the key gives both the label and the tooltip
        "correctness": [
            ("verification_runs", m["verification_runs"]),
            ("accepted_unverified", sum(1 for b in badges if b["type"] == "no_verification_since_edit")),
            ("changed", f"+{m['lines_added']} / −{m['lines_removed']} in {m['files_written']} files")],
        "cost": [("estimated_cost", _cost(m)), ("tokens", f"{_tokens_in(m) + m['output_tokens']:,}")],
        "autonomy": [
            ("developer_turns", m["developer_turns"]),
            ("denials", m["denials"]),
            ("unclear_requests", sum(1 for b in badges if b["type"] == "unclear_request"))],
        "steps_context": [
            ("tool_calls", m["tool_calls"]),
            ("peak_context", f"{m['peak_context_tokens']:,} tokens"),
            ("compactions", m["compactions"])],
    }
    rows = []
    for key in CRITERIA:
        entry = (goal or {}).get(key)
        level = f' {esc(entry["level"])}' if entry else ""
        icon = _icon(LEVEL_ICONS[entry["level"]], "icon status") if entry else _icon("dot", "icon status")
        light = (f'<p class="goal-light">{_icon("sparkle")}<span class="level">'
                 f'{_term(entry["level"], LEVEL_LABELS[entry["level"]])}</span> {esc(entry["reason"])} '
                 f'{_anchor(entry["event_id"])}</p>' if entry else "")
        figs = "".join(f"<div><dt>{_term(k)}</dt><dd>{esc(str(v))}</dd></div>" for k, v in figures[key])
        rows.append(f'<li class="goal-card{level}">{icon}<div class="goal-main">'
                    f'<p class="check-title"><strong>{_term(key, CRITERION_LABELS[key])}</strong></p>{light}</div>'
                    f'<dl class="figures">{figs}</dl></li>')
    return (f'<section class="box goal-box"><header class="box-head"><strong>Goal</strong>'
            f'<span class="muted">{esc(GOAL_LINE)}</span></header><ul class="goal">{"".join(rows)}</ul></section>')


def _summary_html(summary):
    cards = [
        ("What is the task?", esc(summary["task"]) + " " + _anchor(summary["task_event"])),
        ("Centaur or cyborg?", f'<strong>{_term(summary["style"], summary["style"].title())}.</strong> '
                               f'{esc(summary["style_reason"])}'),
        (f'How was the {_term("strategy", "strategy")} checked?', esc(summary["strategy_check"]) + " " + _anchor(summary["strategy_event"])),
        ("What happened?", esc(summary["story"])),
    ]
    return (f'<section class="box summary judgment-box" id="summary"><header class="box-head">{_icon("sparkle")}'
            f'<strong>Summary</strong><span class="muted">Written by Claude</span></header><dl class="qa">'
            + "".join(f"<div><dt>{q}</dt><dd>{a}</dd></div>" for q, a in cards) + "</dl></section>")


def _sidebar(facts, phases, badges, mode):
    order = {e["id"]: n for n, e in enumerate(walk(facts["events"]))}
    items = "".join(f'<li><a href="#phase-{n}"><span>{esc(p["title"])}</span>'
                    f'<span class="range">{esc(p["first_event"])}</span></a></li>' for n, p in enumerate(phases, 1))
    html = f'<aside class="sidebar"><nav class="side-nav">'
    if mode != "export":
        rows = "".join(
            f'<li><a class="badge-link {"judgment" if b.get("judgment") else "detected"}" href="#{esc(b["event_id"])}">'
            f'<span class="key"></span><span class="badge-label">{AXIS_LABELS[b["axis"]]} · '
            f'{esc(b["type"].replace("_", " "))}</span><span class="range">{esc(b["event_id"])}</span></a></li>'
            for b in sorted(badges, key=lambda b: order.get(b["event_id"], 0)))
        html += (f'<div class="side-block"><h2>Badges</h2><p class="hint">Click one to jump to the moment.</p>'
                 f'<ul class="side-list">{rows or "<li class=hint>No badges.</li>"}</ul>'
                 f'<p class="legend"><span class="key detected"></span>{_term("detected", "Detected")}'
                 f'<span class="key judgment"></span>{_term("judgment", "Written by Claude")}</p></div>')
    raw_link = ('<li><a href="#raw"><span>Raw session</span><span class="range">jsonl</span></a></li>'
                if facts.get("raw") else "")
    help_item = ('<li><a href="#glossary"><span>How to read this report</span><span class="range">?</span></a></li>'
                 if mode != "export" else "")
    html += f'<div class="side-block"><h2>Phases</h2><ul class="side-list">{items}{help_item}{raw_link}</ul></div>'
    return html + "</nav></aside>"


def _phases_html(events, phases, by_event):
    index = {e["id"]: i for i, e in enumerate(events)}
    parts = []
    for n, phase in enumerate(phases, 1):
        chunk = events[index[phase["first_event"]]: index[phase["last_event"]] + 1]
        summary = f'<p>{esc(phase["summary"])}</p>' if phase.get("summary") else ""
        parts.append(f'<section class="phase" id="phase-{n}"><header class="phase-head">'
                     f'<h3>{esc(phase["title"])}</h3><span class="range">{esc(phase["first_event"])}–'
                     f'{esc(phase["last_event"])}</span>{summary}</header>'
                     f'<div class="thread">{_events_html(chunk, by_event)}</div></section>')
    return "".join(parts)


def _improvements_html(items):
    rows = "".join(
        f'<li class="tip"><p class="tip-head">{_anchor(i["event_id"])}'
        f'<span class="tag">{_term(i["axis"])}</span><span class="tag criterion">{_term(i["criterion"])}</span></p>'
        f'<p class="problem">{esc(i["problem"])}</p>'
        f'<div class="suggested"><p class="suggested-head">Suggested prompt'
        f'<button class="copy" type="button">{_icon("copy")}Copy</button></p>'
        f'<pre class="prompt">{esc(i["better_prompt"])}</pre></div></li>' for i in items)
    return (f'<section class="box judgment-box tips" id="improvements"><header class="box-head">{_icon("sparkle")}'
            f'<strong>What to do differently next time</strong><span class="counter">{len(items)}</span></header>'
            f'<ol class="suggestions">{rows}</ol></section>')


def _raw_html(raw):
    if not raw:
        return ""
    count = sum(len(f["lines"]) for f in raw)
    size = sum(len(line) for f in raw for line in f["lines"])
    parts = []
    for f in raw:
        note = ('<p class="hint">Cut short: the report keeps the beginning of long sessions. The full file stays '
                'in ~/.claude/projects.</p>' if f["truncated"] else "")
        parts.append(f'<h3 class="raw-file">{esc(f["file"])}<span class="range">{len(f["lines"])} lines</span></h3>'
                     f'{note}<pre class="raw">{esc(chr(10).join(f["lines"]))}</pre>')
    return (f'<details class="raw-session" id="raw"><summary class="raw-summary">{_icon("file")}'
            f'<strong>Raw session</strong><span class="muted">{count} lines · {size / 1024:,.0f} KB · '
            f'JSONL, redacted like the rest of the report</span></summary>{"".join(parts)}</details>')


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
    warnings = "".join(f'<p class="warning">{_icon("alert")}{esc(w)}</p>' for w in facts["warnings"])
    if mode == "export":
        overview = f'<p class="metrics-line">{esc(_metrics_line(facts["metrics"]))}</p>'
    else:
        overview = ((_review_html(annotations["verdict"]) if mode == "review" else "")
                    + (_improvements_html(annotations.get("improvements", [])) if mode == "review" else "")
                    + _goal_html(facts, badges, annotations.get("goal") if mode == "review" else None)
                    + _summary_html(annotations["summary"]))
    body = (_masthead(facts, mode, annotations)
            + '<div class="layout"><main class="timeline">'
            + f'<section id="overview" class="overview">{warnings}{overview}</section>'
            + f'<section id="conversation"><h2 class="section-title">Conversation</h2>'
            + f'{_phases_html(events, phases, by_event)}</section>'
            + (_glossary_html() if mode != "export" else "")
            + _raw_html(facts.get("raw"))
            + (f'<p class="closing"><strong>{esc(CLOSING_LINE)}</strong> conversation-review is a skill made by '
               f'Anthony RIMET, inspired by Marek Kalnik\'s talk on conversation review (Dev With AI 2026).</p>'
               if mode != "export" else "")
            + "</main>" + _sidebar(facts, phases, badges, mode) + "</div>")
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
        if errors:
            print("Invalid annotations:\n" + "\n".join(f"- {e}" for e in errors), file=sys.stderr)
            return 1
        annotations = redact_annotations(annotations)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    html = render_html(facts, annotations, args.mode) if args.mode != "export" or args.pdf else ""
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
