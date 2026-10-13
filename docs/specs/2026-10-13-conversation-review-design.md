# conversation-review: design

Date: 2026-10-13
Status: draft, awaiting review

## Purpose

Code review is moving away from the diff. When an agent writes 500 files, the decisions that matter were taken earlier, in the conversation between the developer and the agent. This skill applies Marek Kalnik's *conversation review* framework (Dev With AI, Paris, 2026) to Claude Code sessions.

The framework asks three questions:

- **Intention**: did the developer state the problem clearly?
- **Evidence**: what did the developer rely on to accept the result?
- **Control**: did the developer keep control of scope, context, tools and permissions?

The framework judges a conversation against one goal: get the largest sensible task done **correctly**, at the **lowest cost**, with **minimal human dependency**, in **few steps** with as little context as possible. Its conclusion: the conversation becomes the most important artifact.

A report answers four questions:

1. What is the task?
2. What did the conversation look like: centaur (the developer keeps the plan and delegates long, bounded chunks) or cyborg (developer and agent interleave in short steps)?
3. How was the technical strategy checked?
4. What went on during the conversation?

**Primary reader**: a peer or lead who reviews a pull request alongside the conversation that produced it. The developer generates the report and ships it with the PR, or sends it by hand.

## Scope

In scope for v1:

- Claude Code sessions only (JSONL under `~/.claude/projects/`, CLI and desktop app).
- One session per report. Subagent transcripts are nested under the tool call that spawned them.
- Three modes: `export`, `audit`, `review`.
- Automatic redaction, validated by the developer before anything is written.
- Optional PDF through an installed Chrome/Chromium.
- All generated text and UI labels in English.

Out of scope for v1: other agents (Codex, Cursor), multi-session reports, hosted publishing, i18n, numeric scores.

## Invocation

```
/conversation-review [export|audit|review] [session-id|list] [--pdf] [--out <dir>]
```

- No session id: the current session.
- `list`: the skill shows the project's recent sessions (title, date, size) and the developer picks one.
- `--out`: defaults to `.conversations/` at the repository root.

## Modes

| Mode | Output | Summary (4 questions) | Goal grid | Phases and badges | Verdict per axis | Improvements |
|---|---|---|---|---|---|---|
| `export` | `.md` (+ `.pdf`) | no | no | no | no | no |
| `audit` | `.html` (+ `.pdf`) | yes | figures only | yes | no | no |
| `review` | `.html` (+ `.pdf`) | yes | figures + light per criterion | yes | yes | yes |

File name: `<YYYY-MM-DD>-<session-slug>.<ext>`, where the slug comes from the session title.

## Architecture

```
session.jsonl (+ subagents/*.jsonl)
   │
   ▼
scripts/extract.py ──► facts.json   normalized, redacted timeline + metrics
   │                   digest.txt   one line per event, for Claude to read
   ▼
Claude shows the redaction list ──► developer validates or adjusts
   │
   ├─ export:       render.py md   facts.json                  ──► .md
   └─ audit/review: Claude reads digest.txt, writes annotations.json
                    render.py html facts.json annotations.json ──► .html
                    (--pdf: Chrome headless prints the HTML)
```

Python 3 standard library only. No third-party dependency.

### Units

| Unit | Does | Depends on |
|---|---|---|
| `scripts/extract.py` | Parses JSONL, redacts, numbers events, computes metrics and factual badges, writes `facts.json` and `digest.txt` | stdlib |
| `scripts/render.py` | Validates `annotations.json` against `facts.json`, renders Markdown or HTML, optionally prints PDF | stdlib, Chrome for PDF |
| `scripts/template.html` | Self-contained HTML/CSS/JS shell filled by `render.py` | none |
| `skills/conversation-review/SKILL.md` | Drives the flow: find the session, run extract, confirm redactions, annotate, render, clean up | the two scripts |

### `facts.json`

```json
{
  "session": { "id": "", "title": "", "model": "", "branch": "", "cwd": "~/...", "started_at": "", "ended_at": "" },
  "metrics": { "cost_usd_estimate": 0, "unpriced_messages": 0, "input_tokens": 0, "output_tokens": 0,
               "cache_read_tokens": 0, "cache_write_tokens": 0, "tool_calls": 0, "files_written": 0,
               "lines_added": 0, "lines_removed": 0, "developer_turns": 0, "verification_runs": 0,
               "denials": 0, "compactions": 0, "peak_context_tokens": 0 },
  "events": [
    { "id": "e1", "kind": "user_message|assistant_message|tool_call|subagent|system",
      "timestamp": "", "text": "", "tool": "", "input_summary": "", "output": "", "output_truncated": true,
      "files": [{ "path": "", "added": 0, "removed": 0 }], "children": [] }
  ],
  "factual_badges": [ { "event_id": "e12", "axis": "control", "type": "outside_scope", "note": "Read node_modules/foo/index.js" } ],
  "redactions": [ { "id": "secret#1", "type": "api_key", "preview": "sk-…a3f", "event_ids": ["e7"] } ],
  "warnings": []
}
```

- Thinking blocks are left out. They are not part of what the developer saw.
- `cost_usd_estimate` comes from token usage multiplied by a per-model price table in `extract.py`. The report labels it as an estimate.
- Outputs longer than 20 lines are stored in full but flagged `output_truncated` for display.
- Goal figures: `developer_turns` counts main-thread developer messages; `verification_runs` counts Bash calls that run tests, builds or linters (not commits); `denials` counts refused tool calls; `compactions` counts context compactions; `peak_context_tokens` is the largest input (fresh + cache read + cache write) sent in one API call.

### Factual badges (computed by `extract.py`)

| Axis | Type | Rule |
|---|---|---|
| control | `outside_scope` | A Read/Grep/Glob/Bash path resolves outside the session `cwd` (for example `node_modules`, `~`) |
| control | `destructive_command` | Bash matches `rm -rf`, `git push --force`, `git reset --hard`, `DROP TABLE`, and similar |
| control | `permission` | A permission was denied, or the session ran in auto or bypass mode (from the `permissionDecision` field and the permission mode, when the transcript records it) |
| control | `cost_spike` | A single turn costs more than 25% of the session total |
| evidence | `no_verification_since_edit` | The developer accepts (commit, "ok", end of session) with no test run, build or file read since the last edit |

### Judgment badges and annotations (written by Claude)

`annotations.json`:

```json
{
  "summary": {
    "task": "", "task_event": "e1",
    "style": "centaur|cyborg|mixed", "style_reason": "",
    "strategy_check": "", "strategy_event": "e5",
    "story": ""
  },
  "phases": [ { "title": "Request", "first_event": "e1", "last_event": "e3", "summary": "" } ],
  "badges": [ { "event_id": "e1", "axis": "intention", "type": "unclear_request", "note": "" } ],
  "verdict": {
    "intention": { "level": "green|orange|red", "reason": "", "event_id": "e1" },
    "evidence":  { "level": "", "reason": "", "event_id": "" },
    "control":   { "level": "", "reason": "", "event_id": "" }
  },
  "goal": {
    "correctness":   { "level": "green|orange|red", "reason": "", "event_id": "" },
    "cost":          { "level": "", "reason": "", "event_id": "" },
    "autonomy":      { "level": "", "reason": "", "event_id": "" },
    "steps_context": { "level": "", "reason": "", "event_id": "" }
  },
  "improvements": [ { "event_id": "e1", "axis": "intention", "criterion": "autonomy", "problem": "", "better_prompt": "" } ]
}
```

`summary` answers the four questions and is required in `audit` and `review`. `story` is 2 to 4 sentences. `strategy_check` says how the technical strategy was validated before implementation (plan requested and reviewed, alternatives compared, throwaway spike), or starts with "Not checked" when it was not. The centaur/cyborg style describes how the work was split; it is not a judgment.

Judgment badge types:

| Axis | Type | Meaning |
|---|---|---|
| intention | `unclear_request` | The request did not state the expected outcome, or the developer had to reframe it ("no, I meant…") |
| evidence | `accepted_without_proof` | The developer accepted a result with no proof (confirms or adds detail to the factual badge) |
| evidence | `proof_present` | Positive badge: a test, build or manual check backed the acceptance |
| control | `unrequested_action` | The agent wrote code, installed, committed or pushed when the request was for a strategy, a question or a plan |

`verdict`, `goal` and `improvements` are only present in `review` mode. `improvements` holds 3 to 5 items, ranked by impact; each names the axis it addresses and the goal criterion it improves (`correctness`, `cost`, `autonomy`, `steps_context`).

`render.py` rejects the file if any event id (`event_id`, `first_event`, `last_event`, `task_event`, `strategy_event`) does not exist in `facts.json`, if `summary` is missing or incomplete, if `style` is not `centaur`, `cyborg` or `mixed`, if phases overlap or leave events uncovered, if an improvement has an unknown axis or criterion, or if `verdict`/`goal`/`improvements` appear in `audit` mode. The error names the faulty field so Claude can fix it.

## HTML layout

Single self-contained file: inline CSS and JS, works offline, light and dark themes.

- **Header**: session title, date, model, branch, repo, and the goal in one line ("the largest sensible task, correctly, at the lowest cost, with minimal human dependency, in few steps"). In `review` mode, three traffic lights (Intent, Evidence, Control) with one sentence each, linked to the event.
- **Goal grid**: four boxes. Correctly: verification runs, files and lines changed. Cost: estimated cost, tokens. Human dependency: developer turns, denials, `unclear_request` badges. Steps & context: tool calls, peak context, compactions. In `review` mode each box adds a light and one sentence from `goal`.
- **Summary**: four question/answer cards (Task, Centaur or cyborg, Strategy check, What happened), each linked to its event when it has one.
- **Sidebar**: phase list, badge counts per axis acting as filters ("show only Control").
- **Timeline**: grouped by phase. Developer and agent messages shown in full. Tool calls on one line, expandable; consecutive calls to the same tool grouped (`Read ×14`). Outputs truncated at 20 lines with "Show all". Subagents collapsed under their parent call. Badges inline, next to their event.
- **Improvements** (`review` only): ranked list, each item linking to its event, with an axis pill and a goal-criterion pill, the problem and the prompt that would have avoided it.
- **Raw session** (last, collapsed, never printed): the session JSONL and its subagent files, every string redacted like the rest of the report; transcript ids, request ids and thinking signatures are left untouched; capped at 5 MB with a note when cut.
- **Footer line**: "The conversation is the most important artifact."
- **Anchors**: every event has `#e<n>` so a reviewer can link to a moment in a PR comment.
- **Print CSS**: everything expanded, sidebar hidden. The PDF uses this.

The Markdown export is a flat chronological transcript: messages in full, tool calls as one line, outputs truncated, no phases or badges.

## Redaction

Done in `extract.py`, before any file is written.

- Detected: known key formats (`sk-`, `ghp_`, `gho_`, `AKIA`, `xox[bp]-`, JWT, `-----BEGIN … KEY-----`), values read from `.env*` and `*.pem` files, email addresses, high-entropy strings of 24+ characters, and `$HOME` (replaced by `~`, not listed).
- Replaced by `[REDACTED:<type>#<n>]`. The same secret keeps the same number throughout the session.
- The skill shows the redaction list (type, masked preview, events). The developer can unmask false positives (`--keep secret#3`) or add strings (`--mask "<string>"`). `extract.py` reruns with those options.
- Markdown, HTML and PDF are all rendered from the redacted `facts.json`. No renderer reads the raw JSONL.
- Intermediate files (`facts.json`, `digest.txt`, `annotations.json`) are deleted at the end. Only the deliverables stay in `.conversations/`.

## Error handling

| Situation | Behavior |
|---|---|
| Session not found | List recent sessions for the project |
| Truncated JSONL (session still running, partial last line) | Skip the line, add a warning shown in the report |
| Unknown event id or invalid annotations | `render.py` exits non-zero naming the field; Claude fixes and reruns |
| `--pdf` without Chrome/Chromium | HTML is still produced; message says how to install Chrome |
| Unknown model in price table | Cost shown as "unknown" for those turns; tokens still counted |

## Testing

Standard library `unittest`, no framework.

- Fixtures in `tests/fixtures/`: 2 or 3 anonymized session JSONL files, one reproducing Marek's case (strategy requested, code written, reads in `node_modules`), one with a subagent, one with planted secrets.
- `extract.py`: metrics (lines, tokens, cost), factual badges, subagent nesting, redaction (no planted secret appears anywhere in `facts.json`, `digest.txt` or rendered output).
- `extract.py` goal figures: developer turns, verification runs (a commit whose message mentions tests does not count), denials, compactions, peak context.
- `render.py`: invalid event id fails, overlapping phases fail, `audit` with `verdict` or `goal` fails, missing `summary` fails, unknown style, axis or criterion fails, HTML contains the expected anchors, the summary cards and the goal grid, review adds lights to the grid and pills to improvements, Markdown has no badges.
- Claude's judgment is not tested automatically. It is checked by hand on real sessions.

## Repository layout

```
conversation-review/
  .claude-plugin/plugin.json
  skills/conversation-review/SKILL.md
  scripts/extract.py
  scripts/render.py
  scripts/template.html
  tests/fixtures/*.jsonl
  tests/test_extract.py
  tests/test_render.py
  README.md
```

Open source. Code, docs and generated text in English. Commits carry no AI attribution and no AI tool files are versioned.
