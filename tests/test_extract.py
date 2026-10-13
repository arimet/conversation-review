import unittest

from fixtures import CWD, SessionBuilder, TmpDirCase, marek_session, subagent_session
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


if __name__ == "__main__":
    unittest.main()
