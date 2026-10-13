import unittest

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
        self.assertIn('<span class="tag criterion">Steps &amp; context</span>', html)
        self.assertIn('<span class="tag axis-evidence">Evidence</span>', html)

    def test_text_is_escaped_and_rendered_once(self):
        facts = sample_facts()
        facts["session"]["title"] = "Title {{BODY}}"
        html = self.html("audit", facts)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)", html)
        self.assertEqual(html.count('class="timeline"'), 1)

    def test_consecutive_calls_to_the_same_tool_are_grouped(self):
        self.assertIn("Read ×2", self.html())

    def test_badged_events_get_filter_classes(self):
        self.assertRegex(self.html(), r'<details id="e6" class="[^"]*has-control')


if __name__ == "__main__":
    unittest.main()
