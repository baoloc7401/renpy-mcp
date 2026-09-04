"""Tests for the lint-output parser.

Real Ren'Py lint runs are gated on the SDK being available, so these
tests feed canned stdout strings into ``parse_lint_output`` directly —
that way the parser stays exercised regardless of whether ``RENPY_SDK``
is set.
"""

from __future__ import annotations

from renpy_mcp.project.lint_parse import (
    aggregate_findings,
    normalize_finding_message,
    parse_lint_output,
)


CLEAN_OUTPUT = """ï»¿Ren'Py 8.6.0.25112108 lint report, generated at: Sun Apr 26 02:28:31 2026


Statistics:

The game contains 10 dialogue blocks, containing 130 words and 709 characters,
for an average of 13.0 words and 71 characters per block.

The game contains 0 menus, 2 images, and 24 screens.


Lint is not a substitute for thorough testing. Remember to update Ren'Py
before releasing. New releases fix bugs and improve compatibility.
"""

DIRTY_OUTPUT = """ï»¿Ren'Py 8.6.0 lint report, generated at: anytime

game/script.rpy:5 The jump is to nonexistent label 'nowhere'.
It is advised to set config.check_conflicting_properties to True.

game/screens.rpy:42 Image bg missing uses file 'images/totally_missing.png', which is not loadable.

game/script.rpy:10 The label dup is defined twice, at File "game/script.rpy", line 11:

and File "game/script.rpy", line 14:


Statistics:

The game contains 1 dialogue blocks.

3 errors, 0 warnings, 0 informational messages, 0 obsolete creator-defined names.

Lint is not a substitute for thorough testing.
"""


def test_clean_output_yields_no_findings():
    parsed = parse_lint_output(CLEAN_OUTPUT)
    assert parsed["findings"] == []
    assert parsed["advisories"] == []
    # Synthesized summary when the line is missing.
    assert parsed["summary"] == {"errors": 0, "warnings": 0, "info": 0, "obsolete": 0}
    assert parsed["statistics"] is not None
    assert any("dialogue blocks" in s for s in parsed["statistics"])


def test_dirty_output_extracts_findings_with_severity():
    parsed = parse_lint_output(DIRTY_OUTPUT)
    findings = parsed["findings"]
    # Three primary findings: nonexistent jump, missing asset, defined twice.
    assert len(findings) >= 3
    by_msg = {f["message"]: f for f in findings}
    nonexistent = next(f for m, f in by_msg.items() if "nonexistent" in m)
    assert nonexistent["file"] == "game/script.rpy"
    assert nonexistent["line"] == 5
    assert nonexistent["severity"] == "error"
    not_loadable = next(f for m, f in by_msg.items() if "not loadable" in m)
    assert not_loadable["file"] == "game/screens.rpy"
    assert not_loadable["severity"] == "error"


def test_advisories_separated_from_findings():
    parsed = parse_lint_output(DIRTY_OUTPUT)
    assert any("config.check_conflicting_properties" in a for a in parsed["advisories"])
    # Advisory must NOT appear inside any finding's message.
    assert not any(
        "check_conflicting_properties" in f["message"] for f in parsed["findings"]
    )


def test_summary_line_captured_when_present():
    parsed = parse_lint_output(DIRTY_OUTPUT)
    assert parsed["summary"] == {"errors": 3, "warnings": 0, "info": 0, "obsolete": 0}
    assert parsed["summary_line"] is not None
    assert "3 errors" in parsed["summary_line"]


def test_defined_twice_collects_references():
    parsed = parse_lint_output(DIRTY_OUTPUT)
    twice = next(
        f for f in parsed["findings"] if "defined twice" in f["message"]
    )
    refs = twice.get("references") or []
    assert {"file": "game/script.rpy", "line": 14} in refs


def test_severity_inference_for_warnings():
    """A made-up "should " sentence rounds to info — confirm the
    inference treats it as advisory rather than error."""
    sample = "game/script.rpy:1 You should consider naming this label.\n"
    parsed = parse_lint_output(sample)
    assert parsed["findings"][0]["severity"] == "info"


def test_returns_summary_when_lint_omits_it():
    """Modern Ren'Py sometimes prints no summary line. We synthesize one
    so agents have a consistent shape to dispatch on."""
    sample = "game/script.rpy:5 Image bg foo uses file 'foo.png', which is not loadable.\n"
    parsed = parse_lint_output(sample)
    assert parsed["summary"]["errors"] == 1
    assert parsed["summary"]["warnings"] == 0


# ---------- aggregate_findings ---------------------------------------------------


def test_normalize_finding_message_collapses_quoted_substrings():
    a = normalize_finding_message("Could not evaluate 'jennifer' in the who part of a say statement.")
    b = normalize_finding_message("Could not evaluate 'erica' in the who part of a say statement.")
    assert a == b
    assert "'{value}'" in a
    # Double-quoted substrings collapse too.
    c = normalize_finding_message('Image bg foo uses file "images/foo.png", which is not loadable.')
    assert "'{value}'" in c
    assert "images/foo.png" not in c


def _finding(file, line, message, severity="warning", rule="renpy_lint"):
    return {"rule": rule, "severity": severity, "file": file, "line": line, "message": message}


def test_aggregate_findings_groups_by_normalized_pattern():
    findings = [
        _finding("game/a.rpy", 1, "Could not evaluate 'jennifer' in the who part of a say statement."),
        _finding("game/a.rpy", 2, "Could not evaluate 'erica' in the who part of a say statement."),
        _finding("game/b.rpy", 3, "Could not evaluate 'stephanie' in the who part of a say statement."),
    ]
    out = aggregate_findings(findings)
    assert out["pattern_count"] == 1
    assert out["truncated"] is False
    group = out["patterns"][0]
    assert group["count"] == 3
    assert group["severity"] == "warning"
    assert len(group["examples"]) == 3
    files = {e["file"] for e in group["examples"]}
    assert files == {"game/a.rpy", "game/b.rpy"}


def test_aggregate_findings_caps_examples_but_keeps_true_count():
    findings = [
        _finding("game/a.rpy", i, f"Could not evaluate '{i}' in the who part of a say statement.")
        for i in range(50)
    ]
    out = aggregate_findings(findings, examples_per_pattern=5)
    group = out["patterns"][0]
    assert group["count"] == 50
    assert len(group["examples"]) == 5


def test_aggregate_findings_keeps_distinct_patterns_separate():
    findings = [
        _finding("game/a.rpy", 1, "Could not evaluate 'jennifer' in the who part of a say statement."),
        _finding("game/a.rpy", 2, "Image bg foo uses file 'foo.png', which is not loadable.", severity="error"),
    ]
    out = aggregate_findings(findings)
    assert out["pattern_count"] == 2
    assert {g["count"] for g in out["patterns"]} == {1, 1}


def test_aggregate_findings_sorts_by_severity_then_count_descending():
    findings = (
        [_finding("f", 1, "warn pattern A") for _ in range(10)]
        + [_finding("f", 1, "warn pattern B") for _ in range(3)]
        + [_finding("f", 1, "error pattern", severity="error") for _ in range(1)]
    )
    out = aggregate_findings(findings)
    severities_and_counts = [(g["severity"], g["count"]) for g in out["patterns"]]
    # Error first regardless of its (lower) count; within warnings, the
    # more common pattern (10) sorts ahead of the less common one (3).
    assert severities_and_counts == [("error", 1), ("warning", 10), ("warning", 3)]


def test_aggregate_findings_truncates_pattern_list_when_over_limit():
    findings = [_finding("f", i, f"unique message number {i} with no shared template") for i in range(10)]
    out = aggregate_findings(findings, pattern_limit=3)
    assert out["pattern_count"] == 10
    assert out["truncated"] is True
    assert len(out["patterns"]) == 3


def test_aggregate_findings_handles_empty_input():
    out = aggregate_findings([])
    assert out["patterns"] == []
    assert out["pattern_count"] == 0
    assert out["truncated"] is False
