#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["regex"]
# ///
"""Conformance tests for script-runs.py against the v2.0 companion fixture.

Runs every case in docs/script-run-extraction-tests.json under the default
policy, plus each declared policy-sensitivity variant under its non-default
knob, and checks the extracted run substrings match exactly (code points, not
glyphs). Two suites:

  * ``cases``                — the v1.4 Latin backward-compatibility suite,
                               run under target_script = Latin (spec 12.1).
  * ``generalization_cases`` — G1-G11, each carrying its own target_script
                               (Greek, Cyrillic, Arabic, Hebrew, Latin; 12.2).

Also verifies the input_codepoints redundancy lists, so a corrupted or
re-escaped fixture — or a cross-script confusable substituted by copy-paste
(Greek Alpha U+0391 vs Latin A U+0041) — is caught rather than silently
mis-tested.

Run:  uv run tests/test_script_runs.py     (installs the 'regex' dependency)
  or: python tests/test_script_runs.py     (if 'regex' is already importable)
from the repo root or from inside tests/.
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIXTURE = os.path.join(ROOT, "docs", "script-run-extraction-tests.json")


def load_script_runs():
    spec = importlib.util.spec_from_file_location(
        "script_runs", os.path.join(ROOT, "script-runs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script_runs = load_script_runs()


def policy_from(defaults, **overrides):
    merged = dict(defaults)
    merged.update(overrides)
    # affinity_overrides in the fixture would be a {hex: affinity} map; the
    # default is empty and no case overrides it, but translate defensively.
    ov = merged.get("affinity_overrides") or {}
    merged["affinity_overrides"] = {
        script_runs._parse_codepoint(k): v.upper() for k, v in ov.items()}
    return script_runs.Policy(**merged)


def extracted(text, policy):
    return [sub for (sub, _s, _e) in script_runs.extract_script_runs(text, policy)]


def check_codepoints(case):
    """Verify input_codepoints matches the parsed input, when present."""
    listed = case.get("input_codepoints")
    if listed is None:
        return
    got = ["U+%04X" % ord(ch) for ch in case["input"]]
    want = [cp.upper() for cp in listed]
    assert got == want, (
        "case %s input_codepoints mismatch:\n  fixture: %s\n  parsed:  %s"
        % (case["id"], want, got))


def check_offsets(text, policy):
    """Every emitted (start, end) must slice back to the run text (strip mode)."""
    if policy.bidi_controls == "preserve_pairs":
        return  # emitted text intentionally drops shed controls inside the span
    for sub, start, end in script_runs.extract_script_runs(text, policy):
        assert text[start:end] == sub, (
            "offset slice %r != run %r" % (text[start:end], sub))


def run_suite(cases, defaults, label):
    """Drive one fixture array; returns the number of assertions made."""
    checked = 0
    for case in cases:
        text = case["input"]
        check_codepoints(case)

        # A generalization case names its own target script; the Latin suite
        # inherits the fixture default (spec 12.1, 12.2). An isolate case adds a
        # per-case "policy" object, merged over the defaults for that case only
        # (spec 12.3) — the knobs it exercises are off by default.
        base = dict(defaults)
        if "target_script" in case:
            base["target_script"] = case["target_script"]
        base.update(case.get("policy", {}))

        # Default policy.
        pol = policy_from(base)
        got = extracted(text, pol)
        assert got == case["expected"], (
            "%s case %s (target_script=%s, default) expected %r, got %r"
            % (label, case["id"], pol.target_script, case["expected"], got))
        check_offsets(text, pol)
        checked += 1

        # Declared policy-sensitivity variants.
        for knob, settings in (case.get("policy_sensitivity") or {}).items():
            for setting, expected in settings.items():
                value = {"on": True, "off": False}.get(setting, setting)
                pol = policy_from(base, **{knob: value})
                got = extracted(text, pol)
                assert got == expected, (
                    "%s case %s (target_script=%s, %s=%s) expected %r, got %r"
                    % (label, case["id"], pol.target_script, knob, setting,
                       expected, got))
                check_offsets(text, pol)
                checked += 1
    return checked


def check_script_validation():
    """target_script must be a real Script value for the active UCD (spec 2.2)."""
    bad_values = ("Bogus", "Latin}", "",
                  # the four ISO 15924 special codes, by name and by alias
                  "Common", "Zyyy", "Inherited", "Zinh", "Unknown", "Zzzz",
                  "Katakana_Or_Hiragana", "Hrkt")
    for bad in bad_values:
        try:
            script_runs.Policy(target_script=bad)
        except script_runs.ScriptError:
            continue
        raise AssertionError("target_script %r should have been rejected" % bad)
    # Names and 4-letter aliases the UCD knows are accepted.
    for good in ("Latin", "greek", "Cyrl", "Hebrew"):
        script_runs.Policy(target_script=good)


def check_script_listing():
    """Every script --help lists must be usable, and spelled for the right script.

    The listing is read out of the regex module's private property tables, so
    this also fails loudly if a future release moves them (the listing would
    silently shrink to the fallback examples).
    """
    names = script_runs.available_scripts()
    assert len(names) > 100, (
        "only %d scripts listed — has the regex module's property table moved? "
        "(%r)" % (len(names), names[:5]))
    assert names == tuple(sorted(names)), "script listing is not sorted"
    assert len(set(names)) == len(names), "script listing has duplicates"

    values = script_runs.regex._regex_core.PROPERTIES["SCRIPT"][1]
    for name in names:
        # Accepted as typed, and naming a script the active UCD really has.
        script_runs.Policy(target_script=name)
        assert script_runs._normalize_script(name) in values, (
            "listed script %r is not a UCD Script value" % name)

    # A cosmetic display name must resolve to the very script it is keyed under:
    # 'Han' for HANI, not merely to something that happens to compile.
    for key, shown in script_runs._SCRIPT_DISPLAY.items():
        assert key in values, (
            "display table has a stale entry %r; the active UCD has no such "
            "script" % key)
        assert values.get(script_runs._normalize_script(shown)) == values[key], (
            "display name %r does not resolve to script %r" % (shown, key))

    # None of the four special codes may be offered as a target.
    for special in ("Common", "Inherited", "Unknown", "Katakana_Or_Hiragana"):
        assert special not in names, "%s must not be listed as a target" % special

    # Spot-check that a listed non-Latin script really extracts, spelled either
    # way (loose matching), and that a script with no characters in the text
    # yields nothing rather than erroring.
    for spelling in ("Old_Hungarian", "old hungarian", "Hung"):
        pol = script_runs.Policy(target_script=spelling)
        assert extracted("한국어 𐲀𐲂𐲇 텍스트", pol) == ["𐲀𐲂𐲇"], spelling
        assert extracted("한국어 Windows 11", pol) == []


def main():
    with open(FIXTURE, encoding="utf-8") as fh:
        fixture = json.load(fh)

    defaults = fixture["default_policy"]
    cases = fixture["cases"]
    gcases = fixture.get("generalization_cases", [])
    icases = fixture.get("isolate_cases", [])
    qcases = fixture.get("straight_quote_cases", [])

    checked = run_suite(cases, defaults, "latin")
    checked += run_suite(gcases, defaults, "generalization")
    checked += run_suite(icases, defaults, "isolate")
    checked += run_suite(qcases, defaults, "straight-quote")

    # Structural edge cases beyond the fixture (spec section 13), plus the
    # target-script parameterization itself.
    latin = script_runs.Policy()
    assert extracted("", latin) == []
    assert extracted("한국어 텍스트", latin) == []
    assert extracted("Hello, World!", latin) == ["Hello, World"]
    assert extracted("plain latin only", latin) == ["plain latin only"]

    greek = script_runs.Policy(target_script="Greek")
    assert extracted("", greek) == []
    assert extracted("Windows 11", greek) == []          # Latin is OTHER here
    # Internal glue merges by the sandwich rule; the trailing digit group closes
    # at the following space and self-commits, exactly as for a Latin target.
    assert extracted("한국어 Αθήνα-Πάτρα 2026 텍스트", greek) == ["Αθήνα-Πάτρα 2026"]

    # The same text partitions differently per target script (spec 12.2 note).
    mixed = "한국어 Windows 11, Αθήνα 2026 텍스트"
    assert extracted(mixed, latin) == ["Windows 11"]
    assert extracted(mixed, greek) == ["Αθήνα 2026"]

    # min_latin_letters / numerals_bind_to_target are documented aliases.
    assert script_runs.Policy(min_latin_letters=3).min_target_letters == 3
    assert script_runs.Policy(numerals_bind_to_target=True).numerals_bind_to_latin

    check_script_validation()
    check_script_listing()

    print("OK: %d fixture assertions (%d Latin + %d generalization + %d isolate "
          "+ %d straight-quote cases) + edge cases passed"
          % (checked, len(cases), len(gcases), len(icases), len(qcases)))


if __name__ == "__main__":
    main()
