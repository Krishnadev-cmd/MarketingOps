"""The checker on its own: no database, just text and rules."""

from app.services.compliance import Rule, phrase_pattern, scan

DISCLAIMER = "Investments are subject to market risks."
RULES = [
    Rule(1, "prohibited", "guarantee*", "block", reason="Returns can never be promised."),
    Rule(2, "prohibited", "risk-free", "block"),
    Rule(3, "prohibited", "assured return*", "block"),
    Rule(4, "prohibited", "double your money", "block"),
    Rule(5, "prohibited", "best returns", "warn"),
    Rule(6, "disclaimer", DISCLAIMER, "block"),
    Rule(7, "prohibited", "whatsapp only", "block", channels=["whatsapp"]),
]


def run(body, title="Title", channel="linkedin", type_="post"):
    return scan(title, body, channel, type_, RULES)


def kinds(report):
    return sorted((f["kind"], f.get("match", "")) for f in report["findings"])


def test_clean_text_passes():
    report = run(f"Start early. {DISCLAIMER}")
    assert report["passed"] and report["findings"] == []


def test_the_four_classic_phrases_are_blocked():
    body = f"Guaranteed growth, risk-free, assured returns, double your money. {DISCLAIMER}"
    report = run(body)
    assert not report["passed"]
    assert report["blocking"] == 4


def test_spelling_variants_are_caught():
    for text in ["RISK FREE", "risk_free", "riskfree", "Risk-Free", "a guarantee", "we guarantees", "Assured  Return"]:
        assert phrase_pattern_hits(text), text


def phrase_pattern_hits(text):
    report = run(f"{text}. {DISCLAIMER}")
    return report["blocking"] >= 1


def test_whole_words_only():
    report = run(f"We reassured returning clients. {DISCLAIMER}")
    assert report["passed"]


def test_invisible_characters_do_not_hide_a_phrase():
    report = run(f"Guar​anteed income. {DISCLAIMER}")
    assert report["blocking"] == 1
    f = report["findings"][0]
    # Offsets point at the original text, invisible character included.
    assert f["match"] == "Guar​anteed"


def test_offsets_highlight_the_right_span():
    body = f"Hello. Our risk-free plan. {DISCLAIMER}"
    f = run(body)["findings"][0]
    assert body[f["start"]:f["end"]] == "risk-free"


def test_title_is_scanned_too():
    report = run(f"{DISCLAIMER}", title="Guaranteed income")
    assert report["findings"][0]["field"] == "title"


def test_missing_disclaimer_blocks():
    report = run("Start early.")
    assert not report["passed"]
    assert report["findings"][0]["kind"] == "disclaimer"


def test_disclaimer_ignores_case_and_punctuation():
    report = run("Start early.\n\ninvestments ARE subject to market risks")
    assert report["passed"]


def test_warnings_do_not_block():
    report = run(f"The best returns in town. {DISCLAIMER}")
    assert report["passed"] and report["warnings"] == 1


def test_channel_specific_rules():
    assert run(f"WhatsApp only offer. {DISCLAIMER}", channel="linkedin")["passed"]
    assert not run(f"WhatsApp only offer. {DISCLAIMER}", channel="whatsapp", type_="status")["passed"]


def test_linkedin_length_limit_blocks():
    report = run("a" * 3001 + DISCLAIMER)
    assert any(f["kind"] == "length" and f["severity"] == "block" for f in report["findings"])


def test_bad_rule_phrase_is_rejected():
    try:
        phrase_pattern("!!!")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
