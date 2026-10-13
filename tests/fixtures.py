"""Synthetic Claude Code transcripts and sample facts shared by the tests."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "conversation-review" / "scripts"))

CWD = "/Users/dev/work/mail-app"
HOME = "/Users/dev"

DEFAULT_USAGE = {
    "input_tokens": 10,
    "output_tokens": 100,
    "cache_read_input_tokens": 1000,
    "cache_creation_input_tokens": 0,
    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
}

SECRET_KEY = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 2
GH_TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
ENV_PASSWORD = "hunter2-very-secret"


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name


class SessionBuilder:
    """Writes transcripts shaped like ~/.claude/projects/<project>/<session>.jsonl."""

    def __init__(self, session_id="sess-1", cwd=CWD, model="claude-opus-5-5"):
        self.session_id = session_id
        self.cwd = cwd
        self.model = model
        self.rows = []
        self.subagents = {}  # agent_id -> (tool_use_id, SessionBuilder)
        self._n = 0

    def _row(self, type_, **fields):
        self._n += 1
        row = {
            "type": type_,
            "uuid": f"u{self._n}",
            "parentUuid": f"u{self._n - 1}" if self._n > 1 else None,
            "isSidechain": False,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "gitBranch": "feature/sort-emails",
            "timestamp": f"2026-10-13T10:{self._n // 60:02d}:{self._n % 60:02d}.000Z",
        }
        row.update(fields)
        self.rows.append(row)

    def _assistant(self, content, msg_id, usage, model):
        return {
            "model": model or self.model,
            "id": msg_id or f"msg_{self._n + 1}",
            "type": "message",
            "role": "assistant",
            "content": content,
            "usage": usage or DEFAULT_USAGE,
        }

    def title(self, text):
        self.rows.append({"type": "custom-title", "customTitle": text, "sessionId": self.session_id})
        return self

    def raw(self, row):
        self.rows.append(row)
        return self

    def user(self, content, **extra):
        self._row("user", message={"role": "user", "content": content}, **extra)
        return self

    def say(self, text, msg_id=None, usage=None, model=None):
        self._row("assistant", message=self._assistant([{"type": "text", "text": text}], msg_id, usage, model))
        return self

    def think(self, text, msg_id=None):
        self._row("assistant", message=self._assistant([{"type": "thinking", "thinking": text}], msg_id, None, None))
        return self

    def tool(self, name, input, output="", msg_id=None, usage=None, result=None, is_error=False,
             permission=None, model=None):
        tool_id = f"toolu_{self._n + 1:03d}"
        self._row("assistant", message=self._assistant(
            [{"type": "tool_use", "id": tool_id, "name": name, "input": input}], msg_id, usage, model))
        fields = {
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": is_error}]},
            "toolUseResult": result if result is not None else {},
        }
        if permission:
            fields["permissionDecision"] = permission
        self._row("user", **fields)
        return tool_id

    def subagent(self, description, agent_id="a1"):
        """Adds an Agent tool call and returns the builder for the subagent's own transcript."""
        tool_id = self.tool("Agent", {"description": description, "prompt": "...", "subagent_type": "Explore"},
                            output="Subagent report", result={"agentId": agent_id, "status": "completed"})
        child = SessionBuilder(self.session_id, self.cwd, self.model)
        self.subagents[agent_id] = (tool_id, child)
        return child

    def write(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.session_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in self.rows), encoding="utf-8")
        if self.subagents:
            sub_dir = directory / self.session_id / "subagents"
            sub_dir.mkdir(parents=True, exist_ok=True)
            for agent_id, (tool_id, child) in self.subagents.items():
                rows = [dict(r, isSidechain=True, agentId=agent_id) for r in child.rows]
                (sub_dir / f"agent-{agent_id}.jsonl").write_text(
                    "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
                (sub_dir / f"agent-{agent_id}.meta.json").write_text(
                    json.dumps({"toolUseId": tool_id, "agentType": "Explore"}), encoding="utf-8")
        return path


def marek_session():
    """A strategy is requested; the agent reads outside the repo, writes code, and the developer ships it."""
    b = SessionBuilder()
    b.title("Email sorting strategy")
    b.user("Propose a strategy to sort my emails with a local model. Only a strategy, no code.")
    b.think("I should look around")
    b.say("Let me look at the project first.", msg_id="m1")
    b.tool("Read", {"file_path": f"{CWD}/node_modules/ollama/index.js"}, output="1\tmodule.exports = {}", msg_id="m1")
    b.tool("Read", {"file_path": f"{HOME}/.zshrc"}, output="1\texport PATH=/usr/bin")
    b.tool("Write", {"file_path": f"{CWD}/src/sort.ts", "content": "a\nb\nc\n"}, output="File created",
           result={"type": "create", "filePath": f"{CWD}/src/sort.ts", "content": "a\nb\nc\n", "structuredPatch": []})
    b.tool("Edit", {"file_path": f"{CWD}/src/mail.ts", "old_string": "old", "new_string": "new1\nnew2"},
           output="Updated", result={"type": "update", "filePath": f"{CWD}/src/mail.ts", "structuredPatch": [
               {"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 3, "lines": [" keep", "-old", "+new1", "+new2"]}]})
    b.say("Done, I implemented the sorter.")
    b.user("ok commit")
    b.tool("Bash", {"command": "git commit -am 'Add sorter'"}, output="[main abc123] Add sorter")
    return b


def subagent_session():
    b = SessionBuilder(session_id="sess-sub")
    b.user("Find where tests live, then add one.")
    child = b.subagent("Locate tests")
    child.user("Locate tests")
    child.tool("Glob", {"pattern": "**/*.test.ts"}, output="src/a.test.ts")
    child.say("Tests are in src/")
    b.say("Tests are in src/.")
    return b


def secrets_session():
    b = SessionBuilder(session_id="sess-secrets")
    b.user(f"Use my key {SECRET_KEY} and email me at jane.doe@example.com")
    b.tool("Read", {"file_path": f"{CWD}/.env"}, output=f"1\tDB_PASSWORD={ENV_PASSWORD}\n2\tPORT=3000")
    b.tool("Bash", {"command": f"curl -H 'Authorization: Bearer {GH_TOKEN}' https://api.github.com/user"}, output="{}")
    b.say(f"Config is in {HOME}/work/mail-app/.env")
    return b


_SAMPLE_FACTS = {
    "session": {"id": "sess-1", "title": "Email sorting strategy", "model": "claude-opus-5-5",
                "branch": "feature/sort-emails", "cwd": "~/work/mail-app",
                "started_at": "2026-10-13T10:00:00.000Z", "ended_at": "2026-10-13T10:30:00.000Z"},
    "metrics": {"cost_usd_estimate": 17.2, "unpriced_messages": 0, "input_tokens": 1000, "output_tokens": 248000,
                "cache_read_tokens": 5000, "cache_write_tokens": 0, "tool_calls": 5, "files_written": 1,
                "lines_added": 3, "lines_removed": 0, "developer_turns": 2, "verification_runs": 0, "denials": 0,
                "compactions": 0, "peak_context_tokens": 152000},
    "events": [
        {"id": "e1", "kind": "user_message", "timestamp": "2026-10-13T10:05:00.000Z",
         "text": "Propose a strategy. <script>alert(1)</script>"},
        {"id": "e2", "kind": "assistant_message", "timestamp": "2026-10-13T10:06:00.000Z", "text": "Let me look around."},
        {"id": "e3", "kind": "subagent", "timestamp": "2026-10-13T10:07:00.000Z", "tool": "Agent",
         "input_summary": "Locate tests", "output": "Tests are in src/", "output_truncated": False, "files": [],
         "children": [
             {"id": "e4", "kind": "user_message", "timestamp": "2026-10-13T10:07:00.000Z", "text": "Locate tests"},
             {"id": "e5", "kind": "tool_call", "timestamp": "2026-10-13T10:07:30.000Z", "tool": "Glob",
              "input_summary": "**/*.test.ts", "output": "src/a.test.ts", "output_truncated": False,
              "files": [], "children": []}]},
        {"id": "e6", "kind": "tool_call", "timestamp": "2026-10-13T10:08:00.000Z", "tool": "Read",
         "input_summary": "node_modules/ollama/index.js", "output": "\n".join(f"line {i}" for i in range(30)),
         "output_truncated": True, "files": [], "children": []},
        {"id": "e7", "kind": "tool_call", "timestamp": "2026-10-13T10:09:00.000Z", "tool": "Read",
         "input_summary": "src/mail.ts", "output": "1\tcode", "output_truncated": False, "files": [], "children": []},
        {"id": "e8", "kind": "tool_call", "timestamp": "2026-10-13T10:10:00.000Z", "tool": "Write",
         "input_summary": "src/sort.ts", "output": "File created", "output_truncated": False,
         "files": [{"path": "src/sort.ts", "added": 3, "removed": 0}], "children": []},
        {"id": "e9", "kind": "user_message", "timestamp": "2026-10-13T10:20:00.000Z", "text": "ok commit"},
    ],
    "factual_badges": [
        {"event_id": "e6", "axis": "control", "type": "outside_scope", "note": "Read node_modules/ollama/index.js"},
        {"event_id": "e9", "axis": "evidence", "type": "no_verification_since_edit", "note": "Accepted without a test"},
    ],
    "redactions": [],
    "warnings": [],
}


def sample_facts():
    return copy.deepcopy(_SAMPLE_FACTS)


def sample_annotations(mode="review"):
    ann = {
        "summary": {
            "task": "Propose a strategy to sort emails with a local model.", "task_event": "e1",
            "style": "centaur", "style_reason": "The developer delegated the whole task in one message.",
            "strategy_check": "Not checked: the agent wrote code before any plan was shown.", "strategy_event": "e8",
            "story": "The developer asked for a strategy. The agent explored the project, then wrote src/sort.ts. "
                     "The developer committed it without running anything.",
        },
        "phases": [
            {"title": "Request", "first_event": "e1", "last_event": "e2", "summary": "A strategy was requested."},
            {"title": "Implementation", "first_event": "e3", "last_event": "e8", "summary": "The agent wrote code."},
            {"title": "Delivery", "first_event": "e9", "last_event": "e9", "summary": ""},
        ],
        "badges": [{"event_id": "e8", "axis": "control", "type": "unrequested_action",
                    "note": "Code written; only a strategy was asked."}],
    }
    if mode == "review":
        ann["verdict"] = {
            "intention": {"level": "green", "reason": "The request was explicit.", "event_id": "e1"},
            "evidence": {"level": "orange", "reason": "Accepted without running anything.", "event_id": "e9"},
            "control": {"level": "red", "reason": "Code was written without consent.", "event_id": "e8"},
        }
        ann["goal"] = {
            "correctness": {"level": "red", "reason": "Nothing was run before the commit.", "event_id": "e9"},
            "cost": {"level": "orange", "reason": "$17 for a strategy request.", "event_id": "e8"},
            "autonomy": {"level": "green", "reason": "One instruction, no correction needed.", "event_id": "e1"},
            "steps_context": {"level": "orange", "reason": "Reads in node_modules filled the context.", "event_id": "e6"},
        }
        ann["improvements"] = [
            {"event_id": "e1", "axis": "intention", "criterion": "cost", "problem": "No stop condition.",
             "better_prompt": "Propose a strategy. Do not edit files."},
            {"event_id": "e8", "axis": "control", "criterion": "steps_context", "problem": "Edits were not stopped.",
             "better_prompt": "Stop. Revert src/sort.ts and give me the plan only."},
            {"event_id": "e9", "axis": "evidence", "criterion": "correctness", "problem": "Nothing was checked.",
             "better_prompt": "Run the tests and show me the output before I commit."},
        ]
    return ann
