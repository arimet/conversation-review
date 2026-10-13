# conversation-review

**Review the conversation, not only the diff.**

When an agent writes the code, the decisions that matter are taken in the conversation between the developer and the agent: what was asked, what the agent did without asking, what was checked before saying "ok, commit". The diff shows none of it.

conversation-review is a Claude Code plugin that turns a session into a report a teammate can read next to the pull request.

![A conversation review: Claude's verdict on Intent, Evidence and Control, then what to do differently next time, with copyable prompts](docs/images/overview.webp)

## The framework

The report applies the conversation review framework Marek Kalnik (Theodo) presented at Dev With AI 2026. It judges a session against one goal: **get the largest sensible task done correctly, at the lowest cost, with minimal human dependency, in few steps and with as little context as possible.**

Three questions carry the review:

- **Intent**: did the developer state the problem clearly?
- **Evidence**: what did the developer rely on to accept the result?
- **Control**: did the developer keep control of scope, context, tools and permissions?

And every report answers four more: what is the task, was the conversation *centaur* (the developer holds the plan and delegates) or *cyborg* (short interleaved steps), how was the technical strategy checked, and what went on.

## What is in a report

| | |
|---|---|
| **Claude's review** | A verdict per question (Met, Partly met, Not met), each one linked to the moment that decided it. |
| **What to do differently next time** | 3 to 5 tips, each tied to a moment of the conversation, with the prompt that would have avoided the problem and a Copy button. |
| **Goal** | Measured figures for each goal criterion: test runs, edits accepted unverified, cost, tokens, developer turns, denied tool calls, peak context, compactions. |
| **Summary** | The four questions answered in a few sentences. |
| **Conversation** | The whole session as a thread, split into phases. Tool calls fold into one line. Badges sit on the moments that matter. |
| **How to read this report** | A glossary. Every dotted word in the report shows its definition on hover. |
| **Raw session** | The session JSONL, redacted, folded at the end. |

Blue marks what the script **detected** in the transcript (a read outside the repo, a destructive command, edits accepted with no test since). Purple marks what **Claude wrote** while reviewing. Every claim links to an event (`#e12`), so a reviewer can cite it in a PR comment.

![The conversation as a thread: detected badges in blue, Claude's judgments in purple, tool calls folded into one line](docs/images/conv.webp)

![Hovering a word of the framework shows its definition](docs/images/tip.webp)

## Install

```
/plugin marketplace add arimet/conversation-review
/plugin install conversation-review@conversation-review
```

To try it without installing, start Claude Code with the plugin loaded for one session:

```bash
claude --plugin-dir /path/to/conversation-review
```

## Use

```
/conversation-review                  # review the current session
/conversation-review audit list       # pick a past session of this project; no verdict, no tips
/conversation-review export --pdf     # redacted Markdown transcript, plus a PDF
```

| Mode | Output |
|---|---|
| `review` (default) | HTML report: verdict, tips, goal figures and their verdicts, summary, conversation, glossary, raw session |
| `audit` | The same report without verdicts or tips: facts and summary only |
| `export` | Redacted Markdown transcript, no analysis |

Add `--pdf` to any mode to also print a PDF (needs Chrome, Chromium, Brave or Edge). Files go to `.conversations/` at the root of your repository, ready to attach to a pull request.

## Privacy

Before anything is written, the plugin finds API keys, tokens, private keys, values read from `.env` files, email addresses and random-looking strings, and replaces them with `[REDACTED:<type>#<n>]`. Your home directory becomes `~`, including in the folder names Claude Code builds from it. You see the list, unmask false positives (one id, or a whole type), and add the strings it cannot guess, such as client names or internal hosts. Claude's own annotations go through the same masking. Read the file before you commit it.

## How it works

1. `extract.py` reads the session transcript in `~/.claude/projects/` (and its subagents), computes the figures and the detected badges, and redacts everything into `facts.json` plus a short `digest.txt`.
2. Claude reads the digest and writes `annotations.json`: phases, judgment badges, verdicts, tips, summary.
3. `render.py` checks that every claim points to an event that exists, then writes the HTML, Markdown or PDF.

Python 3.9+ standard library only. The report is one self-contained HTML file that works offline.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The screenshots above come from a made-up session. Rebuild them with `python3 docs/demo/build_demo.py <output dir>`.

## Credits

conversation-review is a skill made by Anthony RIMET, inspired by Marek Kalnik's talk on conversation review at Dev With AI 2026.

## License

MIT
