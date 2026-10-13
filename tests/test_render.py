import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fixtures import TmpDirCase, sample_annotations, sample_facts
import render


class ValidateTest(unittest.TestCase):
    def errors(self, annotations, mode):
        return render.validate(sample_facts(), annotations, mode)

    def test_valid_annotations_pass(self):
        for mode in ("audit", "review"):
            self.assertEqual(self.errors(sample_annotations(mode), mode), [])

    def test_unknown_event_ids_are_named(self):
        ann = sample_annotations()
        ann["badges"][0]["event_id"] = "e99"
        ann["verdict"]["control"]["event_id"] = "e42"
        errors = self.errors(ann, "review")
        self.assertIn("badges[0].event_id: unknown event 'e99'", errors)
        self.assertIn("verdict.control.event_id: unknown event 'e42'", errors)

    def test_phases_must_be_contiguous_and_complete(self):
        ann = sample_annotations("audit")
        ann["phases"][1]["first_event"] = "e6"
        self.assertIn("phases[1].first_event: expected 'e3' (phases must be contiguous and cover every event), "
                      "got 'e6'", self.errors(ann, "audit"))
        ann = sample_annotations("audit")
        ann["phases"].pop()
        self.assertIn("phases[1].last_event: must be 'e9', the last event", self.errors(ann, "audit"))
        ann = sample_annotations("audit")
        ann["phases"][1]["last_event"] = "e5"
        self.assertIn("phases[1].last_event: 'e5' is not a top-level event id", self.errors(ann, "audit"))

    def test_badge_types_are_checked_per_axis(self):
        ann = sample_annotations("audit")
        ann["badges"][0]["type"] = "proof_present"
        self.assertIn("badges[0].type: 'proof_present' is not a control badge (allowed: unrequested_action)",
                      self.errors(ann, "audit"))

    def test_mode_rules(self):
        self.assertIn("verdict: not allowed in audit mode", self.errors(sample_annotations("review"), "audit"))
        ann = sample_annotations()
        ann["improvements"] = ann["improvements"][:2]
        self.assertIn("improvements: expected 3 to 5 items, got 2", self.errors(ann, "review"))
        ann = sample_annotations()
        ann["verdict"]["evidence"]["level"] = "yellow"
        self.assertIn("verdict.evidence.level: must be green, orange or red", self.errors(ann, "review"))

    def test_summary_is_required_and_checked(self):
        ann = sample_annotations("audit")
        del ann["summary"]
        self.assertIn("summary: required (task, style, strategy check, story)", self.errors(ann, "audit"))
        ann = sample_annotations("audit")
        ann["summary"].update(style="robot", task_event="e99", story="")
        errors = self.errors(ann, "audit")
        self.assertIn("summary.style: must be centaur, cyborg or mixed", errors)
        self.assertIn("summary.task_event: unknown event 'e99'", errors)
        self.assertIn("summary.story: required", errors)

    def test_goal_and_improvement_tags(self):
        ann = sample_annotations("audit")
        ann["goal"] = sample_annotations()["goal"]
        self.assertIn("goal: not allowed in audit mode", self.errors(ann, "audit"))
        ann = sample_annotations()
        del ann["goal"]["autonomy"]
        ann["improvements"][0]["criterion"] = "speed"
        ann["improvements"][1]["axis"] = "scope"
        errors = self.errors(ann, "review")
        self.assertIn("goal.autonomy.level: must be green, orange or red", errors)
        self.assertIn("improvements[0].criterion: must be one of correctness, cost, autonomy, steps_context", errors)
        self.assertIn("improvements[1].axis: must be one of intention, evidence, control", errors)


class MarkdownTest(unittest.TestCase):
    def test_transcript(self):
        md = render.render_markdown(sample_facts())
        self.assertTrue(md.startswith("# Email sorting strategy\n"))
        self.assertIn("### e1 · Developer · 10:05", md)
        self.assertIn("- `e8` **Write** `src/sort.ts` (src/sort.ts +3/-0)", md)
        self.assertIn("  - `e4` **Main agent → subagent**: Locate tests", md)
        self.assertIn("  - `e5` **Glob** `**/*.test.ts`", md)
        self.assertIn("… 10 more lines", md)
        self.assertIn("Cost (estimate): $17.20", md)

    def test_no_badges_in_export(self):
        md = render.render_markdown(sample_facts())
        for word in ("outside_scope", "no_verification", "Evidence", "Control"):
            self.assertNotIn(word, md)


class HtmlTest(unittest.TestCase):
    def html(self, mode="audit", facts=None):
        annotations = None if mode == "export" else sample_annotations(mode)
        return render.render_html(facts or sample_facts(), annotations, mode)

    def test_every_event_and_phase_has_an_anchor(self):
        html = self.html()
        for i in range(1, 10):
            self.assertIn(f'id="e{i}"', html)
        self.assertIn('id="phase-3"', html)

    def test_audit_has_badges_but_no_verdict(self):
        html = self.html("audit")
        self.assertIn("unrequested action", html)
        self.assertIn("outside scope", html)
        self.assertNotIn('class="verdict"', html)
        self.assertNotIn('id="improvements"', html)

    def test_review_adds_verdict_and_improvements(self):
        html = self.html("review")
        self.assertIn('class="verdict"', html)
        self.assertIn('id="improvements"', html)
        self.assertIn("Run the tests and show me the output before I commit.", html)

    def test_export_has_no_badges_or_filters(self):
        html = self.html("export")
        self.assertNotIn('class="badge', html)
        self.assertNotIn('class="filters"', html)
        self.assertNotIn('id="summary"', html)
        self.assertIn('id="e9"', html)

    def test_audit_shows_summary_and_goal_figures_without_lights(self):
        html = self.html("audit")
        self.assertIn('id="summary"', html)
        self.assertIn("Centaur or cyborg?", html)
        self.assertIn("Not checked: the agent wrote code before any plan was shown.", html)
        self.assertIn("Human dependency", html)
        self.assertIn("152,000 tokens", html)
        self.assertNotIn('class="goal-light"', html)
        self.assertIn("The conversation is the most important artifact.", html)

    def test_review_adds_goal_lights_and_improvement_tags(self):
        html = self.html("review")
        self.assertIn('class="goal-card red"', html)
        self.assertIn('class="goal-light"', html)
        self.assertIn('<span class="tag criterion"><span class="term" tabindex="0" data-term="steps_context">'
                      'Steps &amp; context</span></span>', html)
        self.assertIn('<span class="tag"><span class="term" tabindex="0" data-term="evidence">Evidence</span></span>',
                      html)

    def test_text_is_escaped_and_rendered_once(self):
        facts = sample_facts()
        facts["session"]["title"] = "Title {{BODY}}"
        html = self.html("audit", facts)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)", html)
        self.assertEqual(html.count('class="timeline"'), 1)

    def test_consecutive_calls_to_the_same_tool_are_grouped(self):
        self.assertIn("Read ×2", self.html())


class MarkdownMessageTest(unittest.TestCase):
    def test_messages_render_safe_markdown(self):
        html = render._markdown(
            "Mon choix : **Gallica**, avec `curl`.\n\n"
            "| # | API |\n|---|-----|\n| 1 | Gallica <b> |\n\n"
            "- un\n- deux\n\n"
            "```bash\nnpm test\n```\n"
            "## Plan\nVoir [la doc](https://example.org) <script>alert(1)</script>")
        self.assertIn("<strong>Gallica</strong>", html)
        self.assertIn("<code>curl</code>", html)
        self.assertIn("<table><thead><tr><th>#</th><th>API</th></tr></thead>", html)
        self.assertIn("<td>Gallica &lt;b&gt;</td>", html)
        self.assertIn("<ul><li>un</li><li>deux</li></ul>", html)
        self.assertIn('<pre class="code">npm test</pre>', html)
        self.assertIn("<h4>Plan</h4>", html)
        self.assertIn('<a href="https://example.org" rel="noopener">la doc</a>', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_javascript_links_are_not_rendered(self):
        self.assertNotIn("href", render._markdown("[x](javascript:alert(1))"))


class CallLineTest(unittest.TestCase):
    def test_leading_cd_is_dropped_from_the_call_line(self):
        self.assertEqual(render._first_line("cd /private/tmp/x/scratchpad && npm test\necho done"), "npm test")
        self.assertEqual(render._first_line("cd src; ls"), "ls")
        self.assertEqual(render._first_line("ls -la"), "ls -la")


class HeaderAndSidebarTest(unittest.TestCase):
    def test_model_and_date_are_readable(self):
        self.assertEqual(render._pretty_model("claude-opus-5-5[1m]"), "Claude Opus 5.5")
        self.assertEqual(render._pretty_model("claude-haiku-4-5"), "Claude Haiku 4.5")
        self.assertEqual(render._pretty_model("gpt-x"), "gpt-x")
        self.assertEqual(render._pretty_date("2026-09-24T19:36:00.000Z"), "24 Sep 2026")

    def test_sidebar_lists_badges_as_links_and_tips_come_before_the_goal(self):
        html = render.render_html(sample_facts(), sample_annotations(), "review")
        self.assertIn('<a class="badge-link detected" href="#e6">', html)
        self.assertIn('<a class="badge-link judgment" href="#e8">', html)
        self.assertLess(html.index('id="improvements"'), html.index('class="box goal-box"'))
        self.assertNotIn("data-filter", html)


class RawSessionTest(unittest.TestCase):
    def test_raw_session_is_the_last_collapsible_block(self):
        facts = sample_facts()
        facts["raw"] = [{"file": "sess-1.jsonl", "lines": ['{"type": "user", "text": "<b>hi</b>"}'], "truncated": False}]
        html = render.render_html(facts, sample_annotations(), "review")
        self.assertIn('<details class="raw-session" id="raw">', html)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;", html)
        self.assertLess(html.index('id="raw"'), html.index('class="closing"'))
        self.assertGreater(html.index('id="raw"'), html.index('id="conversation"'))

    def test_reports_without_raw_still_render(self):
        self.assertNotIn('id="raw"', render.render_html(sample_facts(), sample_annotations(), "review"))


class GlossaryTest(unittest.TestCase):
    def test_terms_have_tooltips_backed_by_the_glossary(self):
        import re
        html = render.render_html(sample_facts(), sample_annotations(), "review")
        self.assertIn('<a class="help-link" href="#glossary">', html)
        self.assertIn('id="glossary"', html)
        self.assertIn('<span class="term" tabindex="0" data-term="centaur">Centaur</span>', html)
        self.assertIn('data-term="control"', html)
        self.assertIn('data-term="peak_context"', html)
        used = set(re.findall(r'data-term="(\w+)"', html))
        defined = set(re.findall(r'id="term-(\w+)"', html))
        self.assertTrue(used)
        self.assertEqual(used - defined, set())

    def test_glossary_sits_after_the_conversation(self):
        html = render.render_html(sample_facts(), sample_annotations("audit"), "audit")
        self.assertGreater(html.index('id="glossary"'), html.index('id="conversation"'))

    def test_export_has_no_glossary(self):
        self.assertNotIn('id="glossary"', render.render_html(sample_facts(), None, "export"))


class CliTest(TmpDirCase):
    def write(self, name, value):
        path = Path(self.tmp) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = render.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_export_writes_markdown(self):
        target = str(Path(self.tmp) / "2026-10-13-v1.2-fix")
        code, out, _ = self.run_main(["export", "--facts", self.write("facts.json", sample_facts()), "--out", target])
        self.assertEqual(code, 0)
        self.assertTrue(Path(target + ".md").exists())
        self.assertEqual(out.strip(), target + ".md")

    def test_review_writes_html(self):
        target = str(Path(self.tmp) / "report")
        code, _, _ = self.run_main(["review", "--facts", self.write("facts.json", sample_facts()),
                                    "--annotations", self.write("ann.json", sample_annotations()), "--out", target])
        self.assertEqual(code, 0)
        self.assertIn('class="verdict"', Path(target + ".html").read_text(encoding="utf-8"))

    def test_invalid_annotations_exit_1_and_name_the_field(self):
        ann = sample_annotations()
        ann["badges"][0]["event_id"] = "e99"
        code, _, err = self.run_main(["review", "--facts", self.write("facts.json", sample_facts()),
                                      "--annotations", self.write("ann.json", ann),
                                      "--out", str(Path(self.tmp) / "report")])
        self.assertEqual(code, 1)
        self.assertIn("- badges[0].event_id: unknown event 'e99'", err)
        self.assertFalse((Path(self.tmp) / "report.html").exists())

    def test_missing_browser_keeps_the_html(self):
        target = str(Path(self.tmp) / "report")
        with mock.patch.dict(os.environ, {"CHROME_PATH": ""}), mock.patch.object(render, "CHROME_CANDIDATES", []):
            code, _, err = self.run_main(["audit", "--facts", self.write("facts.json", sample_facts()),
                                          "--annotations", self.write("ann.json", sample_annotations("audit")),
                                          "--out", target, "--pdf"])
        self.assertEqual(code, 0)
        self.assertTrue(Path(target + ".html").exists())
        self.assertIn("PDF skipped", err)
        self.assertIn("CHROME_PATH", err)

    @unittest.skipUnless(render.find_chrome(), "no Chrome or Chromium installed")
    def test_pdf_is_printed(self):
        pdf = Path(self.tmp) / "report.pdf"
        render.print_pdf(render.render_html(sample_facts(), sample_annotations(), "review"), pdf)
        self.assertEqual(pdf.read_bytes()[:4], b"%PDF")



class ShapeTest(unittest.TestCase):
    def test_wrong_shapes_are_named_instead_of_crashing(self):
        cases = [
            ({"verdict": {"intention": "green"}}, "verdict.intention: must be an object"),
            ({"phases": ["x"]}, "phases[0]: must be an object"),
            ({"summary": "text"}, "summary: must be an object"),
            ({"badges": [None]}, "badges[0]: must be an object"),
            ({"improvements": ["x", "y", "z"]}, "improvements[0]: must be an object"),
            ({"goal": []}, "goal: must be an object"),
        ]
        for patch, message in cases:
            with self.subTest(message=message):
                ann = dict(sample_annotations(), **patch)
                self.assertIn(message, render.validate(sample_facts(), ann, "review"))
        self.assertEqual(render.validate(sample_facts(), [], "review"), ["annotations: must be a JSON object"])


class RedactAnnotationsTest(unittest.TestCase):
    def test_annotation_text_is_redacted(self):
        ann = sample_annotations()
        ann["summary"]["story"] = "The developer pasted sk-" + "a1B2c3D4" * 3 + " and read /Users/dev/notes.txt."
        ann["improvements"][0]["better_prompt"] = "Mail jane.doe@example.com first."
        clean = render.redact_annotations(ann, home="/Users/dev")
        self.assertEqual(clean["summary"]["story"], "The developer pasted [REDACTED:api_key#1] and read ~/notes.txt.")
        self.assertEqual(clean["improvements"][0]["better_prompt"], "Mail [REDACTED:email#2] first.")
        self.assertEqual(clean["summary"]["task_event"], "e1")

if __name__ == "__main__":
    unittest.main()
