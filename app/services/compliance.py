"""The compliance checker. A pure function: text + rules in, a report out.

It never touches the database, so it is trivial to test and safe to call on
every keystroke from the editor. The workflow calls it again at submit time
and at publish time, with whatever rules are active *then*.

Three kinds of finding:

    prohibited   a banned phrase appears ("guaranteed", "risk-free"...)
    disclaimer   a required disclaimer is missing
    length       the channel will reject or truncate text this long

Each finding has a severity. "block" stops submit and publish; "warn" is shown
to the writer and the reviewer but does not stop anything.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Characters people (or copy-paste) use to slip past a filter: "guar​anteed".
INVISIBLE = {"​", "‌", "‍", "⁠", "﻿", "­"}

# Hard limits come from the platforms; soft limits are what reads well.
# (field, max characters, severity, why)
LENGTH_LIMITS: dict[tuple[str, str], list[tuple[str, int, str, str]]] = {
    ("linkedin", "post"): [("body", 3000, "block", "LinkedIn rejects posts over 3,000 characters.")],
    ("whatsapp", "status"): [("body", 700, "warn", "Status captions over 700 characters get cut off.")],
    ("whatsapp", "template"): [("body", 1024, "block", "WhatsApp template bodies are limited to 1,024 characters.")],
    ("website", "banner"): [("title", 80, "warn", "Banner headlines over 80 characters wrap on mobile.")],
    ("website", "hero"): [("title", 90, "warn", "Hero headlines over 90 characters push the button below the fold.")],
}


@dataclass
class Rule:
    id: int
    kind: str          # "prohibited" or "disclaimer"
    text: str
    severity: str = "block"
    channels: list[str] = field(default_factory=list)   # empty = all channels
    reason: str = ""

    def applies_to(self, channel: str) -> bool:
        return not self.channels or channel in self.channels


# ---------------------------------------------------------------------------
# Normalising text without losing positions
# ---------------------------------------------------------------------------
def normalise(text: str) -> tuple[str, list[int]]:
    """Lowercase, fold look-alike characters (NFKC), drop invisible characters.

    Returns the cleaned text and, for every character in it, the index of the
    character it came from in the original. That map lets the editor highlight
    exactly the right span of what the writer typed.
    """
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        if ch in INVISIBLE:
            continue
        for c in unicodedata.normalize("NFKC", ch).lower():
            chars.append(c)
            origin.append(i)
    return "".join(chars), origin


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Turn a rule phrase into a forgiving regex.

    "risk-free"   matches  risk-free, risk free, risk_free, RISK  FREE, riskfree
    "guarantee*"  matches  guarantee, guaranteed, guarantees   (* = rest of the word)
    Whole words only, so "assured returns" does not fire inside "reassured returnsheet".
    """
    words = re.findall(r"[a-z0-9]+\*?", unicodedata.normalize("NFKC", phrase).lower())
    if not words:
        raise ValueError(f"Rule phrase has no letters or digits: {phrase!r}")
    parts = [re.escape(w.rstrip("*")) + (r"[a-z0-9]*" if w.endswith("*") else "") for w in words]
    gap = r"[\s\-_.]*"   # spaces, hyphens, underscores or dots between the words, or nothing
    return re.compile(r"(?<![a-z0-9])" + gap.join(parts) + r"(?![a-z0-9])")


def words_of(text: str) -> str:
    """'Investments are subject to market risks.' -> ' investments are subject to market risks '"""
    return " " + " ".join(re.findall(r"[a-z0-9]+", normalise(text)[0])) + " "


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def scan(title: str, body: str, channel: str, content_type: str, rules: list[Rule]) -> dict:
    findings: list[dict] = []
    fields = {"title": title or "", "body": body or ""}

    # 1. Prohibited phrases, in the title and the body.
    for rule in rules:
        if rule.kind != "prohibited" or not rule.applies_to(channel):
            continue
        pattern = phrase_pattern(rule.text)
        for field_name, original in fields.items():
            clean, origin = normalise(original)
            for m in pattern.finditer(clean):
                start, end = origin[m.start()], origin[m.end() - 1] + 1
                findings.append({
                    "kind": "prohibited",
                    "rule_id": rule.id,
                    "severity": rule.severity,
                    "field": field_name,
                    "start": start,
                    "end": end,
                    "match": original[start:end],
                    # Blocking rules forbid; warnings ask for care, so the wording differs.
                    "message": (f'"{original[start:end]}" is not allowed. {rule.reason}' if rule.severity == "block"
                                else f'"{original[start:end]}" needs care. {rule.reason}').strip(),
                })

    # 2. Required disclaimers must appear in the body, word for word (case and
    #    punctuation do not matter).
    body_words = words_of(fields["body"])
    for rule in rules:
        if rule.kind != "disclaimer" or not rule.applies_to(channel):
            continue
        if words_of(rule.text) not in body_words:
            findings.append({
                "kind": "disclaimer",
                "rule_id": rule.id,
                "severity": rule.severity,
                "field": "body",
                "text": rule.text,
                "message": (f"Add the required disclaimer: “{rule.text}”" if rule.severity == "block"
                            else f"Consider adding: “{rule.text}”"),
            })

    # 3. Channel length limits.
    for field_name, limit, severity, why in LENGTH_LIMITS.get((channel, content_type), []):
        length = len(fields[field_name])
        if length > limit:
            findings.append({
                "kind": "length",
                "rule_id": None,
                "severity": severity,
                "field": field_name,
                "message": f"{field_name.capitalize()} is {length:,} characters. {why}",
            })

    blocking = [f for f in findings if f["severity"] == "block"]
    return {
        "passed": not blocking,
        "blocking": len(blocking),
        "warnings": len(findings) - len(blocking),
        "findings": findings,
    }


def summary(report: dict) -> str:
    """One line for audit logs and job results: why something was blocked."""
    blocked = [f["message"] for f in report["findings"] if f["severity"] == "block"]
    return " | ".join(blocked) if blocked else "No blocking issues."
