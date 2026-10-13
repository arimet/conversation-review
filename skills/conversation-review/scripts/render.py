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
    if not phases:
        errors.append("phases: at least one phase is required")
        return
    expected = 0
    for i, phase in enumerate(phases):
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
    for key in SUMMARY_FIELDS:
        if not summary.get(key):
            errors.append(f"summary.{key}: required")
    if summary.get("style") and summary["style"] not in STYLES:
        errors.append("summary.style: must be centaur, cyborg or mixed")
    for key in ("task_event", "strategy_event"):
        if summary.get(key) and summary[key] not in all_ids:
            errors.append(f"summary.{key}: unknown event {summary[key]!r}")


def _validate_lights(section, name, keys, all_ids, errors):
    for key in keys:
        entry = section.get(key) or {}
        if entry.get("level") not in LEVELS:
            errors.append(f"{name}.{key}.level: must be green, orange or red")
        if not entry.get("reason"):
            errors.append(f"{name}.{key}.reason: required")
        if entry.get("event_id") not in all_ids:
            errors.append(f"{name}.{key}.event_id: unknown event {entry.get('event_id')!r}")


def validate(facts, annotations, mode):
    errors = []
    all_ids = {e["id"] for e in walk(facts["events"])}
    _validate_phases(annotations.get("phases") or [], [e["id"] for e in facts["events"]], errors)
    _validate_summary(annotations.get("summary"), all_ids, errors)
    for i, badge in enumerate(annotations.get("badges") or []):
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
        _validate_lights(annotations.get("verdict") or {}, "verdict", AXES, all_ids, errors)
        _validate_lights(annotations.get("goal") or {}, "goal", CRITERIA, all_ids, errors)
        items = annotations.get("improvements") or []
        if not 3 <= len(items) <= 5:
            errors.append(f"improvements: expected 3 to 5 items, got {len(items)}")
        for i, item in enumerate(items):
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
