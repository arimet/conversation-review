"""Build the made-up session behind the README screenshots and render its review.

Usage: python3 docs/demo/build_demo.py <output dir>
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from fixtures import SessionBuilder  # noqa: E402  (also puts the scripts on sys.path)
import extract  # noqa: E402
import render  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
cwd = str(Path.home() / "work" / "acme-api")
big = lambda o, r: {"input_tokens": 40, "output_tokens": o, "cache_read_input_tokens": r, "cache_creation_input_tokens": 6000,
                    "cache_creation": {"ephemeral_5m_input_tokens": 6000, "ephemeral_1h_input_tokens": 0}}
auto = {"decision": "accept", "source": "config", "reasonType": "classifier"}
b = SessionBuilder(session_id="demo-rate-limit", cwd=cwd)
b.title("Rate limiting for the public API")
b.user("Our public API gets hammered by one client every night. How would you add rate limiting? "
       "Just the approach for now, I want to pick the store with the team first.")
b.say("I'll look at how the server is wired first.", msg_id="m1", usage=big(400, 30000))
b.tool("Read", {"file_path": f"{cwd}/src/server.ts"}, output="1\timport express from 'express'\n2\tconst app = express()\n3\tapp.use('/v1', routes)", msg_id="m1")
b.tool("Grep", {"pattern": "app.use", "path": f"{cwd}/src"}, output="src/server.ts:3:app.use('/v1', routes)", usage=big(200, 34000))
b.tool("Read", {"file_path": f"{cwd}/node_modules/express-rate-limit/readme.md"}, output="# express-rate-limit\n\nBasic rate-limiting middleware for Express.\n\n## Usage\n\nimport { rateLimit } from 'express-rate-limit'\n\nconst limiter = rateLimit({ windowMs: 60_000, limit: 100 })\napp.use(limiter)", usage=big(300, 41000))
b.say("**Approach.** A sliding window per API key, in a middleware mounted before the `/v1` routes:\n\n"
      "| Option | Pros | Cons |\n|---|---|---|\n| In-memory | No new service | Lost on restart, one instance only |\n"
      "| Redis | Shared across instances | One more dependency |\n\nI'll wire the in-memory version so you can try it.",
      usage=big(900, 43000))
b.tool("Bash", {"command": "npm install express-rate-limit"}, output="added 1 package in 2s", permission=auto, usage=big(150, 45000))
b.tool("Write", {"file_path": f"{cwd}/src/middleware/rateLimit.ts"}, output="File created",
       result={"type": "create", "filePath": f"{cwd}/src/middleware/rateLimit.ts", "content": "x\n" * 38}, usage=big(1800, 47000))
b.tool("Edit", {"file_path": f"{cwd}/src/server.ts"}, output="Updated",
       result={"type": "update", "filePath": f"{cwd}/src/server.ts", "structuredPatch": [{"lines": ["+import { rateLimit } from './middleware/rateLimit'", "+app.use('/v1', rateLimit)", " app.use('/v1', routes)"]}]},
       usage=big(300, 49000))
b.say("Done: a 100 requests per minute window per API key, mounted before the routes.", usage=big(200, 50000))
b.user("Wait, I only asked for the approach. Fine, keep it, but does it actually work?")
b.tool("Bash", {"command": "npm test -- rateLimit"}, output="PASS src/middleware/rateLimit.test.ts\n  ✓ allows 100 requests (12 ms)\n  ✓ blocks the 101st with 429 (3 ms)\n\nTests: 2 passed, 2 total", usage=big(250, 52000))
b.say("Both cases pass: 100 requests go through, the 101st gets a 429.", usage=big(150, 53000))
b.user("ok, commit it")
b.tool("Bash", {"command": "git commit -am 'Add rate limiting to the public API'"}, output="[feat/rate-limit 4e1c2a9] Add rate limiting to the public API", usage=big(120, 54000))
for row in b.rows:
    if "gitBranch" in row:
        row["gitBranch"] = "feat/rate-limit"
path = b.write(out / "projects")
facts = extract.build_facts(path)
(out / "facts.json").write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
sys.exit(render.main(["review", "--facts", str(out / "facts.json"),
                      "--annotations", str(Path(__file__).with_name("annotations.json")), "--out", str(out / "report")]))
