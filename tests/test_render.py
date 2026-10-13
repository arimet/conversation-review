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


if __name__ == "__main__":
    unittest.main()
