#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["regex"]
# ///
"""Conformance tests for latin-runs.py against the v1.4 companion fixture.

Runs every case in docs/latin-run-extraction-tests.json under the default
policy, plus each declared policy-sensitivity variant under its non-default
knob, and checks the extracted run substrings match exactly (code points, not
glyphs). Also verifies the input_codepoints redundancy lists, so a corrupted or
re-escaped fixture is caught rather than silently mis-tested.

Run:  uv run tests/test_latin_runs.py     (installs the 'regex' dependency)
  or: python tests/test_latin_runs.py     (if 'regex' is already importable)
from the repo root or from inside tests/.
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIXTURE = os.path.join(ROOT, "docs", "latin-run-extraction-tests.json")


def load_latin_runs():
    spec = importlib.util.spec_from_file_location(
        "latin_runs", os.path.join(ROOT, "latin-runs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


latin_runs = load_latin_runs()


def policy_from(defaults, **overrides):
    merged = dict(defaults)
    merged.update(overrides)
    # affinity_overrides in the fixture would be a {hex: affinity} map; the
    # default is empty and no case overrides it, but translate defensively.
    ov = merged.get("affinity_overrides") or {}
    merged["affinity_overrides"] = {
        latin_runs._parse_codepoint(k): v.upper() for k, v in ov.items()}
    return latin_runs.Policy(**merged)


def extracted(text, policy):
    return [sub for (sub, _s, _e) in latin_runs.extract_latin_runs(text, policy)]


def check_codepoints(case):
    """Verify input_codepoints matches the parsed input, when present."""
    listed = case.get("input_codepoints")
    if listed is None:
        return
    got = ["U+%04X" % ord(ch) for ch in case["input"]]
    want = [cp.upper().replace("U+", "U+") for cp in listed]
    assert got == want, (
        "case %s input_codepoints mismatch:\n  fixture: %s\n  parsed:  %s"
        % (case["id"], want, got))


def check_offsets(text, policy):
    """Every emitted (start, end) must slice back to the run text (strip mode)."""
    if policy.bidi_controls == "preserve_pairs":
        return  # emitted text intentionally drops shed controls inside the span
    for sub, start, end in latin_runs.extract_latin_runs(text, policy):
        assert text[start:end] == sub, (
            "offset slice %r != run %r" % (text[start:end], sub))


def main():
    with open(FIXTURE, encoding="utf-8") as fh:
        fixture = json.load(fh)

    defaults = fixture["default_policy"]
    cases = fixture["cases"]
    checked = 0

    for case in cases:
        text = case["input"]
        check_codepoints(case)

        # Default policy.
        pol = policy_from(defaults)
        got = extracted(text, pol)
        assert got == case["expected"], (
            "case %s (default) expected %r, got %r"
            % (case["id"], case["expected"], got))
        check_offsets(text, pol)
        checked += 1

        # Declared policy-sensitivity variants.
        for knob, settings in (case.get("policy_sensitivity") or {}).items():
            for setting, expected in settings.items():
                value = {"on": True, "off": False}.get(setting, setting)
                pol = policy_from(defaults, **{knob: value})
                got = extracted(text, pol)
                assert got == expected, (
                    "case %s (%s=%s) expected %r, got %r"
                    % (case["id"], knob, setting, expected, got))
                check_offsets(text, pol)
                checked += 1

    # A couple of structural edge cases beyond the fixture (spec section 13).
    assert extracted("", latin_runs.Policy()) == []
    assert extracted("한국어 텍스트", latin_runs.Policy()) == []
    assert extracted("Hello, World!", latin_runs.Policy()) == ["Hello, World"]
    assert extracted("plain latin only", latin_runs.Policy()) == ["plain latin only"]

    print("OK: %d fixture assertions (%d cases) + edge cases passed"
          % (checked, len(cases)))


if __name__ == "__main__":
    main()
