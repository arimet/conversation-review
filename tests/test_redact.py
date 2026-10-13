import unittest

import fixtures  # noqa: F401  (puts the scripts directory on sys.path)
from redact import Redactor


class RedactorTest(unittest.TestCase):
    def redact(self, text, **kwargs):
        redactor = Redactor(home="/Users/dev", **kwargs)
        redactor.scan(text, "e1")
        return redactor.apply(text), redactor

    def test_known_formats(self):
        jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
               "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
        for value, type_ in [("sk-ant-api03-" + "Ab1" * 10, "api_key"), ("ghp_" + "a1B2" * 9, "github_token"),
                             ("AKIAABCDEFGHIJKLMNOP", "aws_key"), ("xoxb-1234567890-abcdef", "slack_token"),
                             (jwt, "jwt"), ("jane.doe@example.com", "email")]:
            with self.subTest(type_=type_):
                out, _ = self.redact(f"value: {value} end")
                self.assertEqual(out, f"value: [REDACTED:{type_}#1] end")

    def test_private_key_block(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
        out, _ = self.redact(f"key:\n{key}\nbye")
        self.assertEqual(out, "key:\n[REDACTED:private_key#1]\nbye")

    def test_random_looking_strings_are_flagged(self):
        out, _ = self.redact("token=Zq8vN3xLr7Tw2YpK9sDf4GhJ")
        self.assertEqual(out, "token=[REDACTED:high_entropy#1]")

    def test_hashes_uuids_and_identifiers_are_not_flagged(self):
        text = ("commit 3f2a9c1e5b7d4f6a8c0e2b4d6f8a0c2e4b6d8f0a uuid 690ddc43-c980-447a-9419-6abacc79c400 "
                "name conversation_review_renderer_test")
        out, _ = self.redact(text)
        self.assertEqual(out, text)

    def test_same_secret_keeps_its_number_and_tracks_events(self):
        redactor = Redactor(home="")
        a, b = "sk-" + "a1B2c3D4" * 3, "ghp_" + "Q1w2E3r4" * 5
        redactor.scan(a, "e1")
        redactor.scan(f"{b} and {a}", "e2")
        self.assertEqual(redactor.apply(f"{a} {b}"), "[REDACTED:api_key#1] [REDACTED:github_token#2]")
        self.assertEqual([(r["id"], r["event_ids"]) for r in redactor.report()],
                         [("secret#1", ["e1", "e2"]), ("secret#2", ["e2"])])

    def test_keep_leaves_a_false_positive_visible(self):
        out, redactor = self.redact("contact jane.doe@example.com", keep={"secret#1"})
        self.assertEqual(out, "contact jane.doe@example.com")
        self.assertTrue(redactor.report()[0]["kept"])

    def test_custom_masks_do_not_shift_detected_numbers(self):
        out, _ = self.redact("Project Falcon uses sk-" + "a1B2c3D4" * 3, masks=["Falcon"])
        self.assertEqual(out, "Project [REDACTED:custom#1] uses [REDACTED:api_key#1]")

    def test_env_file_values_are_learned(self):
        redactor = Redactor(home="")
        redactor.learn_env_file("1\tDB_PASSWORD=hunter2-very-secret\n2\tPORT=3000\n3\t# COMMENT=ignored-value", "e3")
        self.assertEqual(redactor.apply("pw hunter2-very-secret port 3000"), "pw [REDACTED:env_value#1] port 3000")

    def test_home_is_shortened_on_path_boundaries_only(self):
        out, redactor = self.redact("/Users/dev/work and /Users/developer/x and /Users/dev")
        self.assertEqual(out, "~/work and /Users/developer/x and ~")
        self.assertEqual(redactor.report(), [])


    def test_dash_encoded_home_in_claude_paths_is_shortened(self):
        out, _ = self.redact("/tmp/claude-501/-Users-dev-work/x and ~/.claude/projects/-Users-dev-work-mail-app/a "
                             "and -Users-developer-x")
        self.assertEqual(out, "/tmp/claude-501/-~-work/x and ~/.claude/projects/-~-work-mail-app/a "
                              "and -Users-developer-x")

    def test_a_longer_secret_containing_a_known_one_is_masked_whole(self):
        redactor = Redactor(home="")
        short, longer = "Zq8vN3xLr7Tw2YpK9sDf4GhJ", "Zq8vN3xLr7Tw2YpK9sDf4GhJextraTail9X"
        redactor.scan(short, "e1")
        redactor.scan(longer, "e2")
        self.assertNotIn("extraTail", redactor.apply(longer))

    def test_keep_accepts_a_whole_type(self):
        out, redactor = self.redact("blob Zq8vN3xLr7Tw2YpK9sDf4GhJ and sk-" + "a1B2c3D4" * 3, keep={"high_entropy"})
        self.assertEqual(out, "blob Zq8vN3xLr7Tw2YpK9sDf4GhJ and [REDACTED:api_key#1]")
        self.assertEqual([r["kept"] for r in redactor.report()], [False, True])

if __name__ == "__main__":
    unittest.main()
