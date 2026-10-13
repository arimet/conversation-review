"""Secret detection and masking for conversation-review.

Two passes keep numbering stable across reruns: `scan` every string first (numbers follow the
order of first appearance), then `apply` to each string. Ids passed in `keep` stay visible.
"""
import math
import re
from collections import Counter

PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
]
HIGH_ENTROPY = re.compile(r"[A-Za-z0-9+_\-]{24,}={0,2}")
CAT_N_PREFIX = re.compile(r"^\s*\d+\t")
ENV_FILE = re.compile(r"(^|/)\.env[^/]*$")
MIN_ENV_VALUE = 8


def entropy(value):
    counts = Counter(value)
    return -sum(c / len(value) * math.log2(c / len(value)) for c in counts.values())


def looks_random(value):
    # ponytail: entropy heuristic, misses low-entropy passwords; the developer adds those with --mask
    return (entropy(value) > 4.0 and any(c.isdigit() for c in value)
            and any(c.islower() for c in value) and any(c.isupper() for c in value))


def preview(value):
    return f"{value[:4]}…{value[-3:]}" if len(value) > 10 else "…"


class Redactor:
    def __init__(self, home="", keep=(), masks=()):
        home = home.rstrip("/")
        # Claude Code also writes the home path dash-encoded in its own folders: /Users/jane -> -Users-jane
        encoded = re.sub(r"[^A-Za-z0-9]", "-", home)
        self.home = re.compile(re.escape(home) + r"(?![A-Za-z0-9_.\-])") if home else None
        self.encoded_home = re.compile(re.escape(encoded) + r"(?![A-Za-z0-9_.])") if home else None
        self.keep = set(keep)
        self.secrets = {}  # value -> {"id", "type", "event_ids"}, in order of first appearance
        for n, value in enumerate((m for m in masks if m), 1):
            self.secrets[value] = {"id": f"mask#{n}", "type": "custom", "event_ids": []}

    def _register(self, value, type_, event_id):
        entry = self.secrets.get(value)
        if entry is None:
            n = sum(1 for e in self.secrets.values() if e["id"].startswith("secret#")) + 1
            entry = self.secrets[value] = {"id": f"secret#{n}", "type": type_, "event_ids": []}
        if event_id and event_id not in entry["event_ids"]:
            entry["event_ids"].append(event_id)

    def learn_env_file(self, output, event_id):
        for line in (output or "").splitlines():
            line = CAT_N_PREFIX.sub("", line).strip()
            if line.startswith("#") or "=" not in line:
                continue
            value = line.split("=", 1)[1].strip().strip("'\"")
            if len(value) >= MIN_ENV_VALUE:
                self._register(value, "env_value", event_id)

    def scan(self, text, event_id=None):
        if not text:
            return
        for value, entry in self.secrets.items():
            if value in text and event_id and event_id not in entry["event_ids"]:
                entry["event_ids"].append(event_id)
        for type_, pattern in PATTERNS:
            for match in pattern.finditer(text):
                self._register(match.group(0), type_, event_id)
        for match in HIGH_ENTROPY.finditer(text):
            value = match.group(0)
            if looks_random(value) and not any(value in known or known in value for known in self.secrets):
                self._register(value, "high_entropy", event_id)

    def apply(self, text):
        if not text:
            return text
        for value in sorted(self.secrets, key=len, reverse=True):
            entry = self.secrets[value]
            if entry["id"] not in self.keep:
                text = text.replace(value, f"[REDACTED:{entry['type']}#{entry['id'].split('#')[1]}]")
        if not self.home:
            return text
        return self.encoded_home.sub("-~", self.home.sub("~", text))

    def report(self):
        return [{"id": e["id"], "type": e["type"], "preview": preview(value), "event_ids": e["event_ids"],
                 "kept": e["id"] in self.keep}
                for value, e in self.secrets.items() if e["event_ids"]]
