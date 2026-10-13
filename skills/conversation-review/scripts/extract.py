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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import ENV_FILE, ENV_IN_COMMAND, Redactor  # noqa: E402

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
        return str(tool_input)
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
    return json.dumps(tool_input, ensure_ascii=False)


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
        # Compaction summaries are written by Claude Code, not the developer; compact_boundary marks the moment.
        if kind == "user" and not row.get("isMeta") and not row.get("isCompactSummary"):
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


def _title(rows, limit=80):
    titles = [r["customTitle"] for r in rows if r.get("type") == "custom-title" and r.get("customTitle")]
    if titles:
        return titles[-1]
    for row in rows:
        if row.get("type") == "user" and not row.get("isMeta") and not row.get("isCompactSummary"):
            text = _user_text((row.get("message") or {}).get("content"))
            if text:
                return text.splitlines()[0][:limit] if limit else text.splitlines()[0]
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
        "title": _title(rows, limit=None) or Path(path).stem,  # truncated after redaction, in build_facts
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


# USD per million tokens: (input, output, cache read). Cache writes cost 1.25x input (5 min) or 2x input (1 h).
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}


def price_for(model):
    """Longest known id that prefixes `model`, so 'claude-opus-5-5[1m]' prices as Opus 5.5, not Opus 5."""
    matches = [known for known in PRICES if model and model.startswith(known)]
    return PRICES[max(matches, key=len)] if matches else None


def message_cost(model, usage):
    price = price_for(model)
    if price is None:
        return None
    input_price, output_price, read_price = price
    creation = usage.get("cache_creation") or {}
    write_1h = creation.get("ephemeral_1h_input_tokens", 0)
    write_5m = creation.get("ephemeral_5m_input_tokens", usage.get("cache_creation_input_tokens", 0) - write_1h)
    return (usage.get("input_tokens", 0) * input_price
            + usage.get("output_tokens", 0) * output_price
            + usage.get("cache_read_input_tokens", 0) * read_price
            + write_5m * input_price * 1.25
            + write_1h * input_price * 2) / 1_000_000


VERIFY = re.compile(r"\b(test|tests|pytest|jest|vitest|mocha|phpunit|rspec|unittest|tsc|lint|eslint|build|"
                    r"cargo\s+check|go\s+vet|make)\b", re.I)
SHIP = re.compile(r"\bgit\s+(commit|push)\b")
DENIED = "The user doesn't want to proceed"


def _command(event):
    return (event.get("_input") or {}).get("command", "") if event.get("tool") == "Bash" else ""


def _is_verification(event):
    """A test, build or lint run. A commit whose message mentions tests is not one."""
    command = _command(event)
    return bool(command and VERIFY.search(command) and not SHIP.search(command))


def _is_denied(event):
    return ((event.get("permission") or {}).get("decision") == "reject"
            or event.get("output", "").startswith(DENIED))


def compute_metrics(events, usage):
    seen, cost, unpriced, peak = set(), 0.0, 0, 0
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    for message_id, model, tokens, turn in usage:
        # One API message is split into one row per content block, each repeating the same usage.
        if not tokens or model == "<synthetic>" or (message_id and message_id in seen):
            continue
        seen.add(message_id)
        peak = max(peak, tokens.get("input_tokens", 0) + tokens.get("cache_read_input_tokens", 0)
                   + tokens.get("cache_creation_input_tokens", 0))
        totals["input_tokens"] += tokens.get("input_tokens", 0)
        totals["output_tokens"] += tokens.get("output_tokens", 0)
        totals["cache_read_tokens"] += tokens.get("cache_read_input_tokens", 0)
        totals["cache_write_tokens"] += tokens.get("cache_creation_input_tokens", 0)
        message = message_cost(model, tokens)
        if message is None:
            unpriced += 1
            continue
        cost += message
        if turn is not None:
            turn["turn_cost"] += message
    tools = [e for e in walk(events) if e["kind"] in ("tool_call", "subagent")]
    changes = [f for e in tools for f in e.get("files", [])]
    return {
        "cost_usd_estimate": round(cost, 4),
        "unpriced_messages": unpriced,
        **totals,
        "tool_calls": len(tools),
        "files_written": len({f["path"] for f in changes}),
        "lines_added": sum(f["added"] for f in changes),
        "lines_removed": sum(f["removed"] for f in changes),
        "developer_turns": sum(1 for e in events if e["kind"] == "user_message"),
        "verification_runs": sum(1 for e in tools if _is_verification(e)),
        "denials": sum(1 for e in tools if _is_denied(e)),
        "compactions": sum(1 for e in walk(events) if e["kind"] == "system"),
        "peak_context_tokens": peak,
    }


DESTRUCTIVE = re.compile(
    r"\brm\s+-[a-z]*(rf|fr)|\bgit\s+push\b.*\s(--force|-f)\b|\bgit\s+reset\s+--hard\b|"
    r"\bgit\s+clean\s+-[a-z]*f|\bgit\s+branch\s+-D\b|\bdrop\s+(table|database)\b", re.I)
ACCEPT = re.compile(r"^\s*(ok|okay|lgtm|looks good|great|perfect|nice|thanks|thank you|ship it|go ahead|"
                    r"commit|push|merge|parfait|merci|top|go)\b", re.I)
# ponytail: a path-looking token after ':' or '/' is skipped, so URLs are ignored; quoted regexes may false-positive
SHELL_PATH = re.compile(r"(?<![\w.:/])(~/[^\s'\"|;&)]+|/[^\s'\"|;&)]+|\.\./[^\s'\"|;&)]*)")
SAFE_PREFIXES = ("/dev/", "/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")
# A shell token starting with "/" is only a path when its first segment is a real root, not a regex like /\p{M}/gu.
SHELL_ROOTS = {"Users", "home", "root", "etc", "var", "opt", "usr", "bin", "sbin", "Library", "System",
               "Applications", "Volumes", "private", "srv", "mnt", "tmp", "dev"}
HARNESS_DIR = "/.claude/projects/"  # transcripts, tool results and memory written by Claude Code itself
AUTO_REASONS = {
    "classifier": "Tool calls auto-approved by the auto mode classifier",
    "mode": "Tool calls auto-approved by the permission mode",
}
COST_SPIKE_SHARE = 0.25
MIN_TURNS_FOR_SPIKE = 4


def _is_outside(path, cwd):
    if "node_modules" in path.split("/"):
        return True
    expanded = os.path.expanduser(path)
    if expanded.startswith(SAFE_PREFIXES) or expanded == "/dev/null" or HARNESS_DIR in expanded:
        return False
    root = cwd.rstrip("/")
    full = os.path.normpath(os.path.join(root, expanded))
    return not (full == root or full.startswith(root + "/"))


def _outside_paths(event, cwd):
    tool_input = event.get("_input") or {}
    if event.get("tool") == "Bash":
        command = tool_input.get("command", "")
        candidates = [p for p in SHELL_PATH.findall(command)
                      if not p.startswith("/") or p.split("/")[1] in SHELL_ROOTS]
        candidates += [w for w in command.split() if "node_modules" in w]
    else:
        candidates = [tool_input[k] for k in PATH_KEYS if isinstance(tool_input.get(k), str)]
    return list(dict.fromkeys(p for p in candidates if _is_outside(p, cwd)))


def _edits(event):
    return any(f["added"] or f["removed"] for e in walk([event]) for f in e.get("files", []))


def verification_badges(events, cwd):
    """Main thread only: an acceptance after an edit needs a test, a build or a read of an edited file."""
    badges, dirty = [], set()
    for event in events:
        if event["kind"] in ("tool_call", "subagent") and _edits(event):
            dirty |= {f["path"] for e in walk([event]) for f in e.get("files", [])}
            continue
        if not dirty:
            continue
        command = _command(event)
        read_path = (event.get("_input") or {}).get("file_path", "") if event.get("tool") == "Read" else ""
        if _is_verification(event) or _relative(read_path, cwd) in dirty:
            dirty = set()
        elif (event["kind"] == "user_message" and ACCEPT.match(event["text"])) or SHIP.search(command):
            badges.append({"event_id": event["id"], "axis": "evidence", "type": "no_verification_since_edit",
                           "note": "Accepted without a test, build or read of the edited files since the last edit"})
            dirty = set()
    if dirty and events:
        badges.append({"event_id": events[-1]["id"], "axis": "evidence", "type": "no_verification_since_edit",
                       "note": "Session ended with unverified edits"})
    return badges


def factual_badges(events, cwd, total_cost):
    badges, auto_seen = [], set()

    def add(event, type_, note):
        badges.append({"event_id": event["id"], "axis": "control", "type": type_, "note": note})

    for event in walk(events):
        if event["kind"] not in ("tool_call", "subagent"):
            continue
        paths = _outside_paths(event, cwd)
        if paths:
            add(event, "outside_scope", f"{event['tool']} {', '.join(paths)}")
        command = _command(event)
        if command and DESTRUCTIVE.search(command):
            add(event, "destructive_command", command)
        permission = event.get("permission") or {}
        if _is_denied(event):
            add(event, "permission", f"Denied ({permission.get('source', 'user')})")
        elif permission.get("reasonType") in AUTO_REASONS and permission["reasonType"] not in auto_seen:
            auto_seen.add(permission["reasonType"])
            add(event, "permission", AUTO_REASONS[permission["reasonType"]])
    turns = [e for e in events if e["kind"] == "user_message"]
    if len(turns) >= MIN_TURNS_FOR_SPIKE and total_cost > 0:
        for turn in turns:
            if turn["turn_cost"] > COST_SPIKE_SHARE * total_cost:
                add(turn, "cost_spike",
                    f"${turn['turn_cost']:.2f} of ${total_cost:.2f} ({turn['turn_cost'] / total_cost:.0%})")
    return badges + verification_badges(events, cwd)


PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"
TEXT_FIELDS = ("text", "input_summary", "output")
TITLE_MAX = 80
NOTE_MAX = 200


def _clip(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"
DIGEST_TEXT = 300


class SessionNotFound(Exception):
    pass


def build_facts(path, keep=(), masks=(), home=None):
    session, events, usage, warnings = parse_session(path)
    metrics = compute_metrics(events, usage)
    badges = factual_badges(events, session["cwd"], metrics["cost_usd_estimate"])
    redactor = Redactor(str(Path.home()) if home is None else home, keep, masks)
    for value in session.values():
        redactor.scan(value if isinstance(value, str) else "")
    for event in walk(events):
        reads_env = (event.get("tool") == "Read" and ENV_FILE.search((event.get("_input") or {}).get("file_path", ""))
                     or ENV_IN_COMMAND.search(_command(event)))
        if reads_env:
            redactor.learn_env_file(event.get("output", ""), event["id"])
        for key in TEXT_FIELDS:
            redactor.scan(event.get(key) or "", event["id"])
        for change in event.get("files", []):
            redactor.scan(change["path"], event["id"])
    for badge in badges:
        redactor.scan(badge["note"], badge["event_id"])
    for event in walk(events):
        for key in [k for k in event if k.startswith("_")]:
            del event[key]
        if "turn_cost" in event:
            event["turn_cost"] = round(event["turn_cost"], 4)
        for key in TEXT_FIELDS:
            if isinstance(event.get(key), str):
                event[key] = redactor.apply(event[key])
        for change in event.get("files", []):
            change["path"] = redactor.apply(change["path"])
    # Redact first, then truncate: a secret or home path cut in half would no longer match.
    for badge in badges:
        badge["note"] = _clip(redactor.apply(badge["note"]), NOTE_MAX)
    session = {k: redactor.apply(v) if isinstance(v, str) else v for k, v in session.items()}
    session["title"] = _clip(session["title"], TITLE_MAX)
    return {
        "session": session,
        "metrics": metrics,
        "events": events,
        "factual_badges": badges,
        "redactions": redactor.report(),
        "warnings": [redactor.apply(w) for w in warnings],
    }


def _digest_line(event):
    if event["kind"] in ("user_message", "assistant_message", "system"):
        who = {"user_message": "USER", "assistant_message": "AGENT", "system": "SYSTEM"}[event["kind"]]
        body = event["text"]
    else:
        who = "SUBAGENT" if event["kind"] == "subagent" else "TOOL"
        body = f"{event['tool']}: {event['input_summary']}"
        body += "".join(f" [{f['path']} +{f['added']}/-{f['removed']}]" for f in event.get("files", []))
        body += " -> ERROR" if event.get("error") else ""
    body = " ".join(body.split())
    return f"{who} {body[:DIGEST_TEXT]}{'…' if len(body) > DIGEST_TEXT else ''}"


def digest(facts):
    s, m = facts["session"], facts["metrics"]
    lines = [
        f"# {s['title']}",
        f"model={s['model']} branch={s['branch']} cost~${m['cost_usd_estimate']} tool_calls={m['tool_calls']} "
        f"files={m['files_written']} +{m['lines_added']}/-{m['lines_removed']}",
        "",
        "Factual badges:",
    ]
    lines += [f"  {b['event_id']} {b['axis']}/{b['type']}: {' '.join(b['note'].split())[:DIGEST_TEXT]}"
              for b in facts["factual_badges"]] or ["  (none)"]
    lines += ["", "Events (open one in full with: extract.py show --facts <facts.json> <id>):"]

    def emit(events, depth):
        for event in events:
            lines.append("  " * depth + f"{event['id']} {_digest_line(event)}")
            emit(event.get("children") or [], depth + 1)

    emit(facts["events"], 0)
    return "\n".join(lines) + "\n"


def output_name(session):
    title = unicodedata.normalize("NFKD", session["title"]).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", title).strip("-")[:60].strip("-") or session["id"][:8]
    return f"{session['started_at'][:10] or date.today().isoformat()}-{slug}"


def project_dir(cwd, projects_dir=None):
    return Path(projects_dir or PROJECTS_DIR) / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def list_sessions(cwd, projects_dir=None):
    directory = project_dir(cwd, projects_dir)
    paths = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    sessions = []
    for path in paths:
        stat = path.stat()
        sessions.append({
            "id": path.stem,
            "path": str(path),
            "title": _title(load_jsonl(path)[0]) or path.stem,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "size_kb": stat.st_size // 1024,
        })
    return sessions


def find_session(cwd, session=None, projects_dir=None):
    if session and session.endswith(".jsonl") and Path(session).exists():
        return Path(session)
    sessions = list_sessions(cwd, projects_dir)
    matches = [s for s in sessions if not session or s["id"].startswith(session)]
    if not matches:
        raise SessionNotFound(f"No session {session or ''} found in {project_dir(cwd, projects_dir)}".strip())
    return Path(matches[0]["path"])


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    list_cmd = commands.add_parser("list", help="List recent sessions of a project")
    list_cmd.add_argument("--cwd", default=os.getcwd())
    list_cmd.add_argument("--limit", type=int, default=15)
    extract_cmd = commands.add_parser("extract", help="Write facts.json and digest.txt")
    extract_cmd.add_argument("--cwd", default=os.getcwd())
    extract_cmd.add_argument("--session", help="Session id prefix or .jsonl path (default: most recent)")
    extract_cmd.add_argument("--out", required=True)
    extract_cmd.add_argument("--keep", action="append", default=[], help="Redaction id (secret#3) or type (high_entropy) to leave visible")
    extract_cmd.add_argument("--mask", action="append", default=[], help="Extra string to redact")
    show_cmd = commands.add_parser("show", help="Print one event in full")
    show_cmd.add_argument("--facts", required=True)
    show_cmd.add_argument("event_id")
    args = parser.parse_args(argv)

    if args.command == "list":
        _print_json(list_sessions(args.cwd)[: args.limit])
        return 0
    if args.command == "show":
        facts = json.loads(Path(args.facts).read_text(encoding="utf-8"))
        event = next((e for e in walk(facts["events"]) if e["id"] == args.event_id), None)
        if event is None:
            print(f"Unknown event {args.event_id}", file=sys.stderr)
            return 1
        _print_json(event)
        return 0
    try:
        path = find_session(args.cwd, args.session)
    except SessionNotFound as error:
        print(error, file=sys.stderr)
        for s in list_sessions(args.cwd)[:10]:
            print(f"  {s['id']}  {s['modified']}  {s['title']}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    facts = build_facts(path, args.keep, args.mask)
    (out / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "digest.txt").write_text(digest(facts), encoding="utf-8")
    _print_json({"session": str(path), "facts": str(out / "facts.json"), "digest": str(out / "digest.txt"),
                 "title": facts["session"]["title"], "output_name": output_name(facts["session"]),
                 "redactions": facts["redactions"], "warnings": facts["warnings"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
