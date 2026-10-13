import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from fixtures import (CWD, ENV_PASSWORD, GH_TOKEN, HOME, SECRET_KEY, SessionBuilder, TmpDirCase,
                      marek_session, secrets_session, subagent_session)
import extract


class ParseTest(TmpDirCase):
    def parse(self, builder):
        return extract.parse_session(builder.write(self.tmp))

    def test_events_follow_the_conversation_and_skip_thinking(self):
        _, events, _, _ = self.parse(marek_session())
        self.assertEqual([(e["kind"], e.get("tool") or e["text"][:20]) for e in events], [
            ("user_message", "Propose a strategy t"),
            ("assistant_message", "Let me look at the p"),
            ("tool_call", "Read"), ("tool_call", "Read"), ("tool_call", "Write"), ("tool_call", "Edit"),
            ("assistant_message", "Done, I implemented "),
            ("user_message", "ok commit"),
            ("tool_call", "Bash"),
        ])
        self.assertEqual([e["id"] for e in events], [f"e{i}" for i in range(1, 10)])

    def test_tool_calls_carry_relative_input_output_and_line_counts(self):
        _, events, _, _ = self.parse(marek_session())
        read, _, write, edit = events[2:6]
        self.assertEqual(read["input_summary"], "node_modules/ollama/index.js")
        self.assertEqual(read["output"], "1\tmodule.exports = {}")
        self.assertFalse(read["output_truncated"])
        self.assertEqual(write["files"], [{"path": "src/sort.ts", "added": 3, "removed": 0}])
        self.assertEqual(edit["files"], [{"path": "src/mail.ts", "added": 2, "removed": 1}])

    def test_errors_are_flagged(self):
        b = SessionBuilder()
        b.user("go")
        b.tool("Bash", {"command": "npm test"}, output="1 failing", is_error=True)
        _, events, _, _ = self.parse(b)
        self.assertTrue(events[1]["error"])

    def test_session_info(self):
        session, _, _, _ = self.parse(marek_session())
        self.assertEqual(session["id"], "sess-1")
        self.assertEqual(session["title"], "Email sorting strategy")
        self.assertEqual(session["model"], "claude-opus-5-5")
        self.assertEqual(session["branch"], "feature/sort-emails")
        self.assertEqual(session["cwd"], CWD)
        self.assertTrue(session["started_at"].startswith("2026-10-13T10:00"))

    def test_title_falls_back_to_first_user_message(self):
        b = SessionBuilder()
        b.user("Fix the login bug\nmore details")
        session, _, _, _ = self.parse(b)
        self.assertEqual(session["title"], "Fix the login bug")

    def test_subagent_transcript_is_nested_under_its_call(self):
        _, events, _, _ = self.parse(subagent_session())
        agent = events[1]
        self.assertEqual(agent["kind"], "subagent")
        self.assertEqual(agent["input_summary"], "Locate tests")
        self.assertEqual([c["kind"] for c in agent["children"]], ["user_message", "tool_call", "assistant_message"])
        self.assertEqual([c["id"] for c in agent["children"]], ["e3", "e4", "e5"])
        self.assertEqual(events[2]["id"], "e6")

    def test_meta_messages_reminders_commands_and_unknown_rows(self):
        b = SessionBuilder()
        b.raw({"type": "attachment", "attachment": {}}).raw({"type": "file-history-snapshot"})
        b.user("hidden", isMeta=True)
        b.user("Real question<system-reminder>internal</system-reminder>")
        b.user("<command-message>review</command-message>\n<command-name>/review</command-name>\n"
               "<command-args>src</command-args>")
        _, events, _, _ = self.parse(b)
        self.assertEqual([e["text"] for e in events], ["Real question", "/review src"])

    def test_partial_last_line_is_skipped_with_a_warning(self):
        path = marek_session().write(self.tmp)
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"type": "user", "mess')
        _, events, _, warnings = extract.parse_session(path)
        self.assertEqual(len(events), 9)
        self.assertEqual(len(warnings), 1)
        self.assertIn("skipped unreadable line", warnings[0])


class MetricsTest(TmpDirCase):
    def metrics(self, builder):
        _, events, usage, _ = extract.parse_session(builder.write(self.tmp))
        return extract.compute_metrics(events, usage), events

    def test_split_assistant_messages_are_counted_once(self):
        m, _ = self.metrics(marek_session())
        self.assertEqual(m["output_tokens"], 700)
        self.assertAlmostEqual(m["cost_usd_estimate"], 0.0157, places=4)
        self.assertEqual(m["tool_calls"], 5)
        self.assertEqual(m["files_written"], 2)
        self.assertEqual((m["lines_added"], m["lines_removed"]), (5, 1))

    def test_cache_writes_and_model_suffixes_are_priced(self):
        b = SessionBuilder(model="claude-opus-5-5[1m]")
        b.user("hi")
        b.say("hello", usage={"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 1_000_000,
                              "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                 "ephemeral_1h_input_tokens": 1_000_000}})
        m, _ = self.metrics(b)
        self.assertEqual(m["cost_usd_estimate"], 8.0)
        self.assertEqual(m["cache_write_tokens"], 1_000_000)

    def test_unknown_models_are_counted_but_not_priced(self):
        b = SessionBuilder(model="claude-unknown-9")
        b.user("hi")
        b.say("hello")
        m, _ = self.metrics(b)
        self.assertEqual((m["cost_usd_estimate"], m["unpriced_messages"], m["output_tokens"]), (0.0, 1, 100))

    def test_cost_is_attributed_to_the_developer_turn(self):
        _, events = self.metrics(marek_session())
        first, second = [e for e in events if e["kind"] == "user_message"]
        self.assertAlmostEqual(first["turn_cost"], 6 * 0.00224)
        self.assertAlmostEqual(second["turn_cost"], 0.00224)

    def test_subagent_usage_counts_toward_the_parent_turn(self):
        m, events = self.metrics(subagent_session())
        self.assertAlmostEqual(events[0]["turn_cost"], m["cost_usd_estimate"], places=4)
        self.assertNotIn("turn_cost", events[1]["children"][0])

    def test_goal_figures(self):
        m, _ = self.metrics(marek_session())
        self.assertEqual((m["developer_turns"], m["verification_runs"], m["denials"], m["compactions"],
                          m["peak_context_tokens"]), (2, 0, 0, 0, 1010))
        b = SessionBuilder()
        b.user("fix it")
        b.tool("Bash", {"command": "npm test"})
        b.tool("Bash", {"command": "git commit -am 'fix tests'"})
        b.tool("Write", {"file_path": f"{CWD}/a"}, output="The user doesn't want to proceed with this tool use.",
               is_error=True)
        b.raw({"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-13T11:00:00.000Z"})
        b.user("go on")
        m, _ = self.metrics(b)
        self.assertEqual((m["developer_turns"], m["verification_runs"], m["denials"], m["compactions"]), (2, 1, 1, 1))


class BadgesTest(TmpDirCase):
    def badges(self, builder):
        session, events, usage, _ = extract.parse_session(builder.write(self.tmp))
        metrics = extract.compute_metrics(events, usage)
        return extract.factual_badges(events, session["cwd"], metrics["cost_usd_estimate"])

    def of_type(self, badges, type_):
        return [(b["event_id"], b["note"]) for b in badges if b["type"] == type_]

    def test_marek_session(self):
        badges = self.badges(marek_session())
        self.assertEqual(self.of_type(badges, "outside_scope"), [
            ("e3", f"Read {CWD}/node_modules/ollama/index.js"), ("e4", f"Read {HOME}/.zshrc")])
        self.assertEqual(self.of_type(badges, "no_verification_since_edit"), [
            ("e8", "Accepted without a test, build or read of the edited files since the last edit")])

    def test_bash_paths(self):
        b = SessionBuilder()
        b.user("go")
        b.tool("Bash", {"command": "curl https://api.github.com/user 2>/dev/null"})
        b.tool("Bash", {"command": f"cd {CWD} && ls src"})
        b.tool("Bash", {"command": "cat ../other-repo/.env"})
        b.tool("Bash", {"command": "ls ~/Library"})
        self.assertEqual([e for e, _ in self.of_type(self.badges(b), "outside_scope")], ["e4", "e5"])

    def test_shell_regexes_and_harness_files_are_not_out_of_scope(self):
        b = SessionBuilder()
        b.user("go")
        b.tool("Bash", {"command": "node -e \"s.replace(/\\p{M}/gu, '').replace(/ABEILLE|ANIMAL/, '')\" | grep '/issue>'"})
        b.tool("Bash", {"command": "cut -c1-70 ~/.claude/projects/-x/abc/tool-results/b9.txt"})
        b.tool("Write", {"file_path": f"{HOME}/.claude/projects/-x/memory/notes.md"})
        b.tool("Bash", {"command": "cat /etc/hosts /Users/someone/secret.txt"})
        self.assertEqual(self.of_type(self.badges(b), "outside_scope"),
                         [("e5", "Bash /etc/hosts, /Users/someone/secret.txt")])

    def test_destructive_commands(self):
        b = SessionBuilder()
        b.user("clean up")
        b.tool("Bash", {"command": "rm -rf build"})
        b.tool("Bash", {"command": "git push --force origin main"})
        b.tool("Bash", {"command": "git reset --hard HEAD~1"})
        b.tool("Bash", {"command": "rm build/a.txt && git push origin main"})
        self.assertEqual([e for e, _ in self.of_type(self.badges(b), "destructive_command")], ["e2", "e3", "e4"])

    def test_permissions(self):
        auto = {"decision": "accept", "source": "config", "reasonType": "classifier"}
        b = SessionBuilder()
        b.user("go")
        b.tool("Bash", {"command": "ls"}, permission=auto)
        b.tool("Bash", {"command": "pwd"}, permission=auto)
        b.tool("Write", {"file_path": f"{CWD}/a"}, output="The user doesn't want to proceed with this tool use.",
               is_error=True)
        b.tool("Write", {"file_path": f"{CWD}/b"}, is_error=True,
               permission={"decision": "reject", "source": "hook", "reasonType": "hook"})
        self.assertEqual(self.of_type(self.badges(b), "permission"), [
            ("e2", "Tool calls auto-approved by the auto mode classifier"),
            ("e4", "Denied (user)"),
            ("e5", "Denied (hook)")])

    def test_cost_spike_needs_four_turns(self):
        big = {"input_tokens": 0, "output_tokens": 100_000, "cache_read_input_tokens": 0,
               "cache_creation_input_tokens": 0}
        b = SessionBuilder()
        for text in ("a", "b", "c"):
            b.user(text).say("ok")
        b.user("d").say("long", usage=big)
        self.assertEqual([e for e, _ in self.of_type(self.badges(b), "cost_spike")], ["e7"])
        short = SessionBuilder()
        short.user("a").say("long", usage=big)
        self.assertEqual(self.of_type(self.badges(short), "cost_spike"), [])

    def test_verification_clears_pending_edits(self):
        b = SessionBuilder()
        b.user("add a sorter")
        b.tool("Write", {"file_path": f"{CWD}/src/a.ts"},
               result={"type": "create", "filePath": f"{CWD}/src/a.ts", "content": "x\n"})
        b.tool("Bash", {"command": "npm test"})
        b.user("ok commit")
        b.tool("Edit", {"file_path": f"{CWD}/src/b.ts"},
               result={"type": "update", "filePath": f"{CWD}/src/b.ts", "structuredPatch": [{"lines": ["+y"]}]})
        b.tool("Read", {"file_path": f"{CWD}/src/b.ts"})
        b.user("great, push")
        b.tool("Edit", {"file_path": f"{CWD}/src/c.ts"},
               result={"type": "update", "filePath": f"{CWD}/src/c.ts", "structuredPatch": [{"lines": ["-z"]}]})
        self.assertEqual(self.of_type(self.badges(b), "no_verification_since_edit"),
                         [("e8", "Session ended with unverified edits")])

    def test_commit_after_edit_is_flagged_even_if_its_message_mentions_tests(self):
        b = SessionBuilder()
        b.user("fix")
        b.tool("Edit", {"file_path": f"{CWD}/src/a.ts"},
               result={"type": "update", "filePath": f"{CWD}/src/a.ts", "structuredPatch": [{"lines": ["+x"]}]})
        b.tool("Bash", {"command": "git commit -am 'fix tests'"})
        self.assertEqual([e for e, _ in self.of_type(self.badges(b), "no_verification_since_edit")], ["e3"])


class BuildFactsTest(TmpDirCase):
    def facts(self, builder, **kwargs):
        return extract.build_facts(builder.write(self.tmp), home=HOME, **kwargs)

    def test_no_secret_survives(self):
        facts = self.facts(secrets_session())
        dump = json.dumps(facts, ensure_ascii=False) + extract.digest(facts)
        for secret in (SECRET_KEY, GH_TOKEN, ENV_PASSWORD, "jane.doe@example.com", HOME + "/"):
            self.assertNotIn(secret, dump)
        self.assertEqual(sorted({r["type"] for r in facts["redactions"]}),
                         ["api_key", "email", "env_value", "github_token"])
        self.assertIn("[REDACTED:github_token#", facts["events"][2]["input_summary"])
        self.assertEqual(facts["session"]["cwd"], "~/work/mail-app")

    def test_keep_and_mask(self):
        first = self.facts(secrets_session())
        email_id = next(r["id"] for r in first["redactions"] if r["type"] == "email")
        facts = self.facts(secrets_session(), keep=[email_id], masks=["mail-app"])
        dump = json.dumps(facts)
        self.assertIn("jane.doe@example.com", dump)
        self.assertNotIn("mail-app", dump)
        self.assertEqual(email_id, next(r["id"] for r in facts["redactions"] if r["type"] == "email"))

    def test_badge_notes_are_redacted_and_internal_fields_stripped(self):
        facts = self.facts(marek_session())
        self.assertEqual(facts["factual_badges"][0]["note"], "Read ~/work/mail-app/node_modules/ollama/index.js")
        for event in extract.walk(facts["events"]):
            self.assertEqual([k for k in event if k.startswith("_")], [])

    def test_digest_lists_badges_and_caps_long_lines(self):
        b = marek_session()
        b.tool("Bash", {"command": "cat <<'EOF'\n" + "x" * 50_000 + "\nEOF"})
        text = extract.digest(self.facts(b))
        self.assertIn("e3 control/outside_scope: Read ~/work/mail-app/node_modules/ollama/index.js", text)
        self.assertIn("e1 USER Propose a strategy", text)
        self.assertTrue(all(len(line) <= extract.DIGEST_TEXT + 40 for line in text.splitlines()))

    def test_digest_badge_lines_stay_on_one_line(self):
        b = SessionBuilder()
        b.user("clean")
        b.tool("Bash", {"command": "rm -rf build &&\nmkdir build"})
        text = extract.digest(self.facts(b))
        self.assertIn("e2 control/destructive_command: rm -rf build && mkdir build", text)

    def test_text_is_redacted_before_it_is_truncated(self):
        b = SessionBuilder()
        b.user("a" * 75 + f" {HOME}/x")
        b.tool("Bash", {"command": "rm -rf build && " + "x" * 180 + f" {HOME}/work/file"})
        b.tool("mcp__notes__save", {"payload": "a" * 190 + f"{HOME}/x"})
        facts = self.facts(b)
        self.assertNotIn("/Us", facts["session"]["title"])
        self.assertNotIn("/Us", facts["factual_badges"][0]["note"])
        self.assertNotIn("/Us", facts["events"][2]["input_summary"])

    def test_output_name(self):
        session = {"id": "abc12345-x", "title": "Stratégie: tri des e-mails!", "started_at": "2026-10-13T10:00:00Z"}
        self.assertEqual(extract.output_name(session), "2026-10-13-strategie-tri-des-e-mails")


class CliTest(TmpDirCase):
    def run_main(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = extract.main(argv)
        return code, buffer.getvalue()

    def test_extract_then_show(self):
        path = marek_session().write(self.tmp)
        out = Path(self.tmp) / "work"
        code, stdout = self.run_main(["extract", "--session", str(path), "--out", str(out)])
        self.assertEqual(code, 0)
        summary = json.loads(stdout)
        self.assertEqual(summary["output_name"], "2026-10-13-email-sorting-strategy")
        self.assertTrue((out / "facts.json").exists() and (out / "digest.txt").exists())
        code, stdout = self.run_main(["show", "--facts", str(out / "facts.json"), "e5"])
        self.assertEqual((code, json.loads(stdout)["tool"]), (0, "Write"))

    def test_list_and_find_sessions(self):
        projects = Path(self.tmp) / "projects"
        directory = extract.project_dir(CWD, projects)
        SessionBuilder(session_id="old").user("Old work").write(directory)
        marek_session().write(directory)
        os.utime(directory / "old.jsonl", (1, 1))
        sessions = extract.list_sessions(CWD, projects)
        self.assertEqual([(s["id"], s["title"]) for s in sessions],
                         [("sess-1", "Email sorting strategy"), ("old", "Old work")])
        self.assertEqual(extract.find_session(CWD, None, projects), directory / "sess-1.jsonl")
        self.assertEqual(extract.find_session(CWD, "old", projects), directory / "old.jsonl")
        with self.assertRaises(extract.SessionNotFound):
            extract.find_session(CWD, "nope", projects)


if __name__ == "__main__":
    unittest.main()
