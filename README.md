# conversation-review

Review the conversation, not only the diff.

When an agent writes the code, the decisions that matter are taken in the conversation between the developer and the agent. This Claude Code plugin turns a session into a document a peer can read next to the pull request, using the three questions of Marek Kalnik's conversation review:

- **Intention**: did the developer state the problem clearly?
- **Evidence**: what did the developer rely on to accept the result?
- **Control**: did the developer keep control of scope, context, tools and permissions?

## What it produces

| Command | Output |
|---|---|
| `/conversation-review export` | Redacted Markdown transcript |
| `/conversation-review audit` | HTML report: the four questions (task, centaur or cyborg, strategy check, what happened), the goal figures, and a timeline grouped in phases with badges |
| `/conversation-review review` | The audit, plus a traffic light per axis and per goal criterion, and 3 to 5 tagged improvements |

Every report judges the conversation against one goal: get the largest sensible task done correctly, at the lowest cost, with minimal human dependency, in few steps with as little context as possible.

Add `--pdf` to any mode to also print a PDF (needs Chrome, Chromium, Brave or Edge). Files go to `.conversations/` at the root of your repository, ready to attach to a pull request.

## Install

```
/plugin marketplace add marmelab/conversation-review
/plugin install conversation-review@conversation-review
```

## Use

```
/conversation-review                 # review the current session
/conversation-review audit list      # pick a past session of this project
/conversation-review export --pdf
```

## Privacy

Before anything is written, the plugin detects API keys, tokens, private keys, values read from `.env` files, email addresses and random-looking strings, and replaces them with `[REDACTED:<type>#<n>]`. Your home directory becomes `~`. You review the list, unmask false positives and add strings it cannot guess (client names, internal hosts). Read the file before you commit it.

## How it works

`extract.py` reads the session transcript in `~/.claude/projects/`, computes metrics and factual badges (reads outside the repo, destructive commands, permissions, cost spikes, edits accepted without a check) and redacts secrets. Claude reads a digest of the session and writes the phases, judgment badges, verdict and improvements. `render.py` checks that every claim points to a real event, then renders the file. Python 3.9+ standard library only.

## Development

```
python3 -m unittest discover -s tests -v
```

## Credits

The Intention / Evidence / Control framework comes from Marek Kalnik (Theodo), presented at Dev With AI 2026 in Paris.

## License

MIT
