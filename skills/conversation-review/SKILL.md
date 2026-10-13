---
name: conversation-review
description: Export, audit or review a Claude Code session so a peer can read the conversation behind a pull request, using the Intention / Evidence / Control framework. Use when the user asks to export a session or conversation, attach a conversation to a PR, audit how a session went, run a conversation review, get a verdict or tips on how they prompted the agent, or invokes /conversation-review.
---

# Conversation review

Turns a Claude Code session into a document a peer reviews next to the pull request it produced. It applies the three questions of Marek Kalnik's conversation review:

- **Intention**: did the developer state the problem clearly?
- **Evidence**: what did the developer rely on to accept the result?
- **Control**: did the developer keep control of scope, context, tools and permissions?

It judges the conversation against one goal: get the largest sensible task done correctly, at the lowest cost, with minimal human dependency, in few steps with as little context as possible. Every report answers four questions: What is the task? Was the conversation centaur or cyborg? How was the technical strategy checked? What went on?

`<skill-dir>` below is the base directory shown when this skill loads. Run the scripts with `python3`. Everything you write into the report is in English, whatever the language of the session.

## 1. Read the request

`/conversation-review [export|audit|review] [session-id|list] [--pdf] [--out <dir>]`

| Mode | Produces |
|---|---|
| `export` | Redacted Markdown transcript, no analysis |
| `audit` | HTML report: the four-question summary, goal figures, timeline in phases with badges; no verdict |
| `review` (default) | The audit plus a traffic light per axis and per goal criterion, and 3 to 5 tagged improvements |

- `list`: run `python3 <skill-dir>/scripts/extract.py list --cwd "$PWD"`, show a numbered table (title, modified, size), and ask which session to use.
- No session given: the most recent session of the current project, which is the one running now.
- Output directory: `--out`, else `<repo root>/.conversations` (`git rev-parse --show-toplevel`, falling back to the current directory). Call it `<out>`.

## 2. Extract

```bash
python3 <skill-dir>/scripts/extract.py extract --cwd "$PWD" [--session <id>] --out <out>/.work
```

It prints JSON with `title`, `output_name`, `redactions` and `warnings`.

## 3. Confirm redactions (never skip)

Show the redactions grouped by type with a count per type. List every entry individually (id, masked preview, events) except `high_entropy`, which can run to hundreds of base64 blobs and identifiers: show its count and five examples. Then ask the developer with AskUserQuestion:

- **Continue**: render as is.
- **Unmask**: some entries are false positives. Rerun step 2 with one `--keep <id>` per entry, or `--keep <type>` for a whole type (for example `--keep high_entropy`).
- **Mask more**: client names, internal hosts, codenames. Rerun step 2 with one `--mask "<string>"` per string.

Show the table again after a rerun and ask again. When the list is empty, still ask, and say that names, internal hosts and codenames are not detected automatically. Do not render before the developer chooses Continue.

## 4. Render

### export

```bash
python3 <skill-dir>/scripts/render.py export --facts <out>/.work/facts.json --out <out>/<output_name> [--pdf]
```

### audit and review

1. Read `<out>/.work/digest.txt`. It lists metrics, factual badges and one line per event (`e12 TOOL Bash: npm test`).
2. Open any event you are about to judge: `python3 <skill-dir>/scripts/extract.py show --facts <out>/.work/facts.json e12`.
3. Write `<out>/.work/annotations.json` following the rules below.
4. Render:

```bash
python3 <skill-dir>/scripts/render.py <mode> --facts <out>/.work/facts.json --annotations <out>/.work/annotations.json --out <out>/<output_name> [--pdf]
```

If it exits with `Invalid annotations`, fix each listed field and rerun.

## 5. Clean up and report

```bash
rm -rf <out>/.work
```

Tell the developer which files were written. In `review` mode, add the three verdict lines. Remind them that the file is redacted but that they should read it before committing it.

## Writing annotations.json

```json
{
  "summary": {
    "task": "One sentence.", "task_event": "e1",
    "style": "centaur", "style_reason": "One sentence.",
    "strategy_check": "One or two sentences.", "strategy_event": "e7",
    "story": "Two to four sentences."
  },
  "phases": [{"title": "Request", "first_event": "e1", "last_event": "e4", "summary": "One sentence."}],
  "badges": [{"event_id": "e9", "axis": "control", "type": "unrequested_action", "note": "One sentence."}],
  "verdict": {
    "intention": {"level": "green", "reason": "One sentence.", "event_id": "e1"},
    "evidence": {"level": "orange", "reason": "One sentence.", "event_id": "e30"},
    "control": {"level": "red", "reason": "One sentence.", "event_id": "e9"}
  },
  "goal": {
    "correctness": {"level": "orange", "reason": "One sentence.", "event_id": "e30"},
    "cost": {"level": "green", "reason": "One sentence.", "event_id": "e12"},
    "autonomy": {"level": "green", "reason": "One sentence.", "event_id": "e1"},
    "steps_context": {"level": "red", "reason": "One sentence.", "event_id": "e9"}
  },
  "improvements": [{"event_id": "e1", "axis": "intention", "criterion": "autonomy", "problem": "One sentence.", "better_prompt": "The prompt the developer could have typed."}]
}
```

`summary` is required in `audit` and `review`. `verdict`, `goal` and `improvements` exist only in `review` mode.

### Summary: the four questions

- **task**: what the developer asked for, in their terms, one sentence. `task_event` is the message that states it.
- **style**: `centaur` when the developer keeps the plan and delegates long, bounded chunks; `cyborg` when developer and agent interleave in short steps, the developer steering each one; `mixed` when the session switches. Neither is better: `style_reason` says what in the session shows it.
- **strategy_check**: how the developer validated the technical strategy before implementation: asked for a plan and reviewed it, compared alternatives, ran a throwaway spike, read the agent's design. When nothing like that happened, start with "Not checked:" and say what happened instead. `strategy_event` points to the moment the strategy was fixed or implementation started.
- **story**: what went on, two to four sentences, in order. The phases give the detail.

### Phases

- 3 to 7 phases, in order, covering every top-level event (ids at the start of a digest line, without indentation) with no gap and no overlap.
- Name them after what happened: Request, Exploration, Planning, Implementation, Verification, Delivery, or a more precise title ("Rewriting the parser").
- The summary says what changed in that phase in one sentence.

### Judgment badges

The digest already lists factual badges (out-of-scope reads, destructive commands, permissions, cost spikes, unverified acceptance). Do not repeat them. Add a judgment badge only where it says something new:

| Axis | Type | When |
|---|---|---|
| intention | `unclear_request` | The request leaves out the expected result or its limits, or the developer has to reframe ("no, I meant…") |
| evidence | `accepted_without_proof` | The developer accepts a result with nothing that shows it works; the note says what would have shown it |
| evidence | `proof_present` | A test run, a build or a manual check backs the acceptance |
| control | `unrequested_action` | The agent edits, installs, commits or pushes when the request was a question, a plan or a strategy |

### Verdict (review only)

- **green**: nothing on this axis changed the outcome.
- **orange**: a problem the developer caught or that had little effect.
- **red**: a problem that shaped the result: code nobody asked for, changes shipped without checking, work outside the agreed scope.
- `reason` is one sentence a reviewer can check by clicking `event_id`.

### Goal (review only)

The report shows the measured figures next to each criterion. Your light says whether the session met the goal on that criterion, given the size of the task:

| Criterion | Ask | Figures shown |
|---|---|---|
| `correctness` | Is the result right, and is there proof? | test or build runs, edits accepted unverified, lines changed |
| `cost` | Was the spend proportionate to the task? | estimated cost, tokens |
| `autonomy` | Could the agent work without being rescued? | developer turns, denied tool calls, unclear requests |
| `steps_context` | Was it done in few steps, with little context? | tool calls, peak context, compactions |

Use the same green / orange / red scale as the verdict. `event_id` points to the moment that decided the light.

### Improvements (review only)

- 3 to 5, ranked by impact on the outcome.
- `axis` is the review question the tip addresses (`intention`, `evidence`, `control`); `criterion` is the goal criterion it improves (`correctness`, `cost`, `autonomy`, `steps_context`).
- `problem` names what happened at that event, without blame.
- `better_prompt` is a prompt the developer could have typed at that moment, ready to copy.

### Rules

- Every claim points to an event id that exists. Open an event with `show` before writing about its output.
- Quote only from `digest.txt` or `show`, never from your own memory of the session: those are redacted, your context is not. `render.py` masks known secret formats and home paths in annotations as a second net, not as the first.
- Quote the developer's words when it helps the reader.
- Write for a peer who reads the PR: plain sentences, specific facts.
