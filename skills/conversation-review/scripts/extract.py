#!/usr/bin/env python3
"""Turn a Claude Code session transcript into redacted facts for a conversation review."""
import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import date, datetime
from pathlib import Path

OUTPUT_PREVIEW_LINES = 20
AGENT_TOOLS = ("Agent", "Task")
PATH_KEYS = ("file_path", "notebook_path", "path")

REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
LOCAL_OUTPUT_TAGS = re.compile(r"</?local-command-std(?:out|err)>")


def load_jsonl(path):
    rows, warnings = [], []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                warnings.append(f"{Path(path).name}: skipped unreadable line {n}")
    return rows, warnings


def walk(events):
    for event in events:
        yield event
        yield from walk(event.get("children") or [])


def _relative(path, cwd):
    root = (cwd or "").rstrip("/")
    if path and root and (path == root or path.startswith(root + "/")):
        return os.path.relpath(path, root)
    return path


def _user_text(content):
    if isinstance(content, str):
        parts = [content]
    else:
        parts = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image":
                parts.append("[image]")
    text = REMINDER.sub("", "\n".join(parts))
    name = COMMAND_NAME.search(text)
    if name:
        args = COMMAND_ARGS.search(text)
        text = f"{name.group(1)} {args.group(1) if args else ''}"
    return LOCAL_OUTPUT_TAGS.sub("", text).strip()


def _result_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, dict):
            parts.append(f"[{block.get('type', 'content')}]")
    return "\n".join(parts)


def _tool_summary(name, tool_input, cwd):
    if not isinstance(tool_input, dict):
        return str(tool_input)[:200]
    if name == "Bash":
        return tool_input.get("command", "")
    if name in ("Grep", "Glob"):
        where = tool_input.get("path")
        return tool_input.get("pattern", "") + (f" in {_relative(where, cwd)}" if where else "")
    if name in AGENT_TOOLS:
        return tool_input.get("description", "")
    for key in PATH_KEYS:
        if isinstance(tool_input.get(key), str):
            return _relative(tool_input[key], cwd)
    for key in ("url", "query", "skill", "description"):
        if tool_input.get(key):
            return str(tool_input[key])
    return json.dumps(tool_input, ensure_ascii=False)[:200]


def _file_changes(result, cwd):
    if not isinstance(result, dict) or not result.get("filePath"):
        return []
    added = removed = 0
    for hunk in result.get("structuredPatch") or []:
        for line in hunk.get("lines", []):
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    if result.get("type") == "create" and not added:
        added = len((result.get("content") or "").splitlines())
    if not (added or removed):
        return []
    return [{"path": _relative(result["filePath"], cwd), "added": added, "removed": removed}]


def _event(kind, row, **fields):
    return {"id": None, "kind": kind, "timestamp": row.get("timestamp", ""), **fields}


def parse_rows(rows, cwd, usage, inherited_turn=None):
    """Turns transcript rows into events. Appends (message_id, model, usage, turn) tuples to `usage`.

    Main-thread user messages own the cost of the turn they start. Subagent transcripts pass the
    main-thread turn that spawned them as `inherited_turn`, so their cost lands on the developer's turn.
    """
    events, tools, turn = [], {}, inherited_turn
    for row in rows:
        kind, message = row.get("type"), row.get("message") or {}
        if kind == "user" and not row.get("isMeta"):
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                event = tools.get(block.get("tool_use_id"))
                if event is None:
                    continue
                event["output"] = _result_text(block.get("content"))
                event["output_truncated"] = len(event["output"].splitlines()) > OUTPUT_PREVIEW_LINES
                if block.get("is_error"):
                    event["error"] = True
                event["files"] = _file_changes(row.get("toolUseResult"), cwd)
                if row.get("permissionDecision"):
                    event["permission"] = row["permissionDecision"]
            text = _user_text(content)
            if text:
                event = _event("user_message", row, text=text)
                if inherited_turn is None:
                    event["turn_cost"] = 0.0
                    turn = event
                events.append(event)
        elif kind == "assistant":
            usage.append((message.get("id"), message.get("model"), message.get("usage"), turn))
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    events.append(_event("assistant_message", row, text=block["text"].strip()))
                elif block.get("type") == "tool_use":
                    name = block.get("name", "")
                    event = _event("subagent" if name in AGENT_TOOLS else "tool_call", row, tool=name,
                                   input_summary=_tool_summary(name, block.get("input"), cwd), output="",
                                   output_truncated=False, files=[], children=[])
                    event["_tool_use_id"] = block.get("id")
                    event["_input"] = block.get("input") if isinstance(block.get("input"), dict) else {}
                    event["_turn"] = turn
                    tools[block.get("id")] = event
                    events.append(event)
        elif kind == "system" and row.get("subtype") == "compact_boundary":
            events.append(_event("system", row, text="Context compacted"))
    return events


def attach_subagents(session_path, events, cwd, usage, warnings):
    sub_dir = Path(session_path).with_suffix("") / "subagents"
    pending = sorted(sub_dir.glob("agent-*.meta.json")) if sub_dir.is_dir() else []
    while pending:  # several passes: a subagent may be spawned by another subagent
        by_tool = {e.get("_tool_use_id"): e for e in walk(events)}
        attached = []
        for meta_path in pending:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                warnings.append(f"{meta_path.name}: unreadable subagent metadata")
                attached.append(meta_path)
                continue
            parent = by_tool.get(meta.get("toolUseId"))
            transcript = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".jsonl")
            if parent is None or not transcript.exists():
                continue
            rows, more = load_jsonl(transcript)
            warnings.extend(more)
            parent["kind"] = "subagent"
            parent["children"] = parse_rows(rows, cwd, usage, parent.get("_turn") or {"turn_cost": 0.0})
            attached.append(meta_path)
        if not attached:
            break
        pending = [p for p in pending if p not in attached]


def _title(rows):
    titles = [r["customTitle"] for r in rows if r.get("type") == "custom-title" and r.get("customTitle")]
    if titles:
        return titles[-1]
    for row in rows:
        if row.get("type") == "user" and not row.get("isMeta"):
            text = _user_text((row.get("message") or {}).get("content"))
            if text:
                return text.splitlines()[0][:80]
    return ""


def session_info(path, rows):
    models = Counter(
        r["message"].get("model") for r in rows
        if r.get("type") == "assistant" and isinstance(r.get("message"), dict)
        and r["message"].get("model") not in (None, "", "<synthetic>")
    )
    stamped = [r["timestamp"] for r in rows if r.get("timestamp")]
    return {
        "id": Path(path).stem,
        "title": _title(rows) or Path(path).stem,
        "model": models.most_common(1)[0][0] if models else "",
        "branch": next((r["gitBranch"] for r in rows if r.get("gitBranch")), ""),
        "cwd": next((r["cwd"] for r in rows if r.get("cwd")), ""),
        "started_at": stamped[0] if stamped else "",
        "ended_at": stamped[-1] if stamped else "",
    }


def parse_session(path):
    path = Path(path)
    rows, warnings = load_jsonl(path)
    session = session_info(path, rows)
    usage = []
    events = parse_rows(rows, session["cwd"], usage)
    attach_subagents(path, events, session["cwd"], usage, warnings)
    for n, event in enumerate(walk(events), 1):
        event["id"] = f"e{n}"
    return session, events, usage, warnings
