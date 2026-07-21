#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["regex"]
# ///
"""
script-runs.py — Extract embedded runs of one Unicode script from mixed-script text.

Given a string whose primary content is written in one or more scripts, this
finds every maximal contiguous run of text belonging to a single configured
*target script* — English phrases, product names, URLs, version strings and
copyright notices when the target is Latin; embedded Greek, Cyrillic, Arabic or
Hebrew phrases when those are the target — together with the language-neutral
"glue" (digits, spaces, punctuation, symbols) that logically belongs to each run.

This is a conforming implementation of "Script Run Extraction from Mixed-Script
Text", specification v2.0 (see docs/script-run-extraction-spec.md). The hard part
is deciding which script-neutral characters (Unicode Script=Common or Inherited)
fold into a target-script run and which act as a bridge to the surrounding text
or dangle without a host. The spec adapts the neutral-resolution skeleton of the
Unicode Bidirectional Algorithm (UAX #9, rules N1-N2); this file follows the
normative pseudocode of the spec's section 11.2 phase for phase:

    1. Classify each grapheme cluster (TARGET / OTHER / NEUTRAL / CONTROL /
       HARD_BREAK) from its "classification code point" (spec section 4.1, 5).
    2. Coalesce adjacent equal-class clusters into runs (section 6).
    3. Resolve bidi-control clusters (section 8; default: strip + re-coalesce).
    4. Resolve each NEUTRAL run against its strong neighbours: merge when
       sandwiched between target-script text (section 7.1), discard when isolated
       (7.2), or split directionally at mixed boundaries using per-cluster
       binding affinity (7.3-7.5).
    5. Trim edge whitespace and terminal punctuation, validate, and emit each
       target-script run with its (start, end) offsets (section 10).

All processing is in logical (storage) order, which is what makes the algorithm
indifferent to RTL display: Arabic and Hebrew are strong scripts like any other —
whether as target or as host — and display-time reordering never enters the
computation.

CONFORMANCE NOTES (spec section 2)
----------------------------------
- Configured with target_script = Latin this is behaviourally identical to a
  v1.4 "Latin Run Extraction" implementation (spec section 2.1); the v1.4
  conformance suite is the fixture's ``cases`` array.
- Documented deviation: spec section 9 makes ``target_script`` a required knob
  with no default. This tool defaults ``--script`` to ``Latin`` for command-line
  ergonomics (the v1.4-equivalent configuration). The library ``Policy`` default
  is likewise ``Latin``.
- Documented alias: ``min_latin_letters`` is accepted as an alias for
  ``min_target_letters``, and ``numerals_bind_to_target`` as an alias for the
  spec's retained name ``numerals_bind_to_latin`` (both permitted by section 9).

UNICODE DATA
------------
Script, Script_Extensions, Extended_Pictographic, Regional_Indicator and UAX #29
grapheme segmentation come from the third-party ``regex`` module (its own bundled
UCD); general categories come from the standard-library ``unicodedata``. The
active versions are printed by ``--unicode-version``. The spec's minimum
reference is Unicode 16.0; character-level differences from a documented later
UCD version are not conformance failures (spec section 2.2).

Usage:
    script-runs [FILE] [--script SCRIPT] [policy options]
    script-runs --script Greek --json report.txt
    echo '한국어 Windows 11 (23H2)' | script-runs
    echo '한국어 Αθήνα 2026 텍스트' | script-runs --script Greek
"""

import argparse
import json
import sys
import unicodedata
from functools import lru_cache

try:
    import regex
except ImportError:  # pragma: no cover - surfaced to the user with guidance
    sys.stderr.write(
        "script-runs: the 'regex' module is required (Script/grapheme data).\n"
        "Run via the provided wrapper (uv installs it), or: pip install regex\n"
    )
    sys.exit(1)

__version__ = "1.0"

# ---------------------------------------------------------------------------
# Cluster classes (spec section 5) and binding affinities (section 7.3)
# ---------------------------------------------------------------------------
TARGET, OTHER, NEUTRAL, CONTROL, HARD_BREAK = (
    "TARGET", "OTHER", "NEUTRAL", "CONTROL", "HARD_BREAK")
RIGHT, LEFT, SEP, DIGIT, STOP = "RIGHT", "LEFT", "SEP", "DIGIT", "STOP"
# EDGE is not a class; it is the effective neighbour at a string boundary or an
# adjacent HARD_BREAK (spec section 7).
EDGE = "EDGE"

DEFAULT_SCRIPT = "Latin"

# Bidi formatting characters (spec section 5.4). Handled by section 8, never as
# ordinary neutral glue.
BIDI_CONTROLS = frozenset({
    0x200E, 0x200F, 0x061C,                     # LRM, RLM, ALM (standalone)
    0x202A, 0x202B, 0x202D, 0x202E, 0x202C,     # LRE, RLE, LRO, RLO, PDF
    0x2066, 0x2067, 0x2068, 0x2069,             # LRI, RLI, FSI, PDI
})
_ISOLATE_INIT = frozenset({0x2066, 0x2067, 0x2068})   # ... PDI (0x2069)
_EMBED_INIT = frozenset({0x202A, 0x202B, 0x202D, 0x202E})  # ... PDF (0x202C)

# Paragraph/line separators are walls (spec section 5.2a, 5.5). The last three
# are the permitted MAY additions (VT, FF, NEL); treating them as walls is safe
# and documented.
HARD_BREAKS = frozenset({0x000A, 0x000D, 0x2028, 0x2029, 0x000B, 0x000C, 0x0085})

# Terminal set for Phase-4 stripping and LEFT affinity (spec section 7.3, 10.1).
TERMINALS = frozenset(".,;:!?")

# Explicit affinity code points (spec section 7.3 step 2), tested against the
# classification code point. General-category rules (step 4) cover the rest of
# openers/closers/currency/whitespace/digits. Affinity is a property of the
# neutral cluster alone and is independent of the configured target script.
EXPLICIT_RIGHT = frozenset({
    0x00A9,  # ©
    0x0023,  # #
    0x0040,  # @
    0x2116,  # №
    0x00BF,  # ¿
    0x00A1,  # ¡
})
EXPLICIT_LEFT = frozenset({
    0x2122,  # ™
    0x2120,  # ℠
    0x0025,  # %
    0x2030,  # ‰
    0x00B0,  # °
    0x002B,  # +
    0x00AE,  # ®  (default LEFT; overridable per section 7.3 / --affinity-override)
}) | frozenset(ord(c) for c in TERMINALS)


def _cjk_strong_set():
    """Code points strengthened to OTHER when cjk_punct_strong is on (section 5.2e).

    The listed CJK punctuation plus the full-width forms block U+FF01-U+FF60 as a
    whole (full-width digits, currency and symbols included, by the same
    host-affiliation logic as Arabic-Indic digits), excluding the full-width
    Latin letters, which are Script=Latin and classify by the base rule of
    section 5.1 regardless (TARGET when the target script is Latin, else OTHER).

    Note that this strengthening can only ever produce OTHER, never TARGET
    (section 5.2d, 5.2e): it is applied to Common/Inherited clusters only.
    """
    s = {0x3001, 0x3002, 0xFF0C, 0xFF0E}
    s.update(range(0x300C, 0x3010))  # corner brackets 「」『』
    for cp in range(0xFF01, 0xFF61):
        if 0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A:
            continue  # full-width Latin letters
        s.add(cp)
    return frozenset(s)


CJK_STRONG = _cjk_strong_set()


# ---------------------------------------------------------------------------
# The target script (spec section 2.2, 5.1)
# ---------------------------------------------------------------------------
class ScriptError(ValueError):
    """The configured target_script is not a usable Unicode Script value."""


# Suggested in error messages only; any Script name or 4-letter alias the active
# UCD knows is accepted.
_SCRIPT_EXAMPLES = ("Latin", "Greek", "Cyrillic", "Arabic", "Hebrew", "Armenian",
                    "Georgian", "Devanagari", "Thai", "Hangul", "Han")

# Script values that name "no script"; a target must identify a real script,
# since Common/Inherited characters are the neutrals this algorithm resolves.
_NON_SCRIPTS = frozenset({"common", "zyyy", "inherited", "qaai", "zinh",
                          "unknown", "zzzz"})

_RE_SCRIPT_NAME = regex.compile(r"\A[A-Za-z][A-Za-z_ ]*\Z")


@lru_cache(maxsize=None)
def script_matcher(script):
    """Compile \\p{Script=...} for ``script``, raising ScriptError if invalid.

    The name is validated before interpolation so a value like ``Latin}`` cannot
    smuggle syntax into the pattern.
    """
    name = script.strip()
    if not _RE_SCRIPT_NAME.match(name):
        raise ScriptError(
            "%r is not a Unicode Script name (letters, spaces and underscores "
            "only; e.g. %s)" % (script, ", ".join(_SCRIPT_EXAMPLES)))
    if name.replace(" ", "_").lower() in _NON_SCRIPTS:
        raise ScriptError(
            "%r names no script; target_script must be a real script such as %s"
            % (script, ", ".join(_SCRIPT_EXAMPLES)))
    try:
        return regex.compile(r"\p{Script=%s}" % name)
    except regex.error:
        raise ScriptError(
            "unknown Unicode Script %r for the active UCD (Unicode %s); try one "
            "of %s, or any other Script name or 4-letter alias"
            % (script, unicodedata.unidata_version, ", ".join(_SCRIPT_EXAMPLES)))


# ---------------------------------------------------------------------------
# Policy (spec section 9)
# ---------------------------------------------------------------------------
class Policy:
    """The tunable knobs of spec section 9; defaults match the companion fixture.

    ``target_script`` is required by the spec and has no default there; this
    implementation defaults it to Latin (the v1.4-equivalent configuration) and
    documents the deviation. ``min_latin_letters`` and ``numerals_bind_to_target``
    are accepted as aliases (section 9).
    """

    __slots__ = (
        "target_script", "strip_terminal_punct", "numerals_bind_to_latin",
        "trailing_digits_bind", "max_bridge", "bidi_controls",
        "min_target_letters", "affinity_overrides", "cjk_punct_strong",
    )

    def __init__(self, target_script=DEFAULT_SCRIPT, strip_terminal_punct=True,
                 numerals_bind_to_latin=False, trailing_digits_bind=True,
                 max_bridge=None, bidi_controls="strip", min_target_letters=1,
                 affinity_overrides=None, cjk_punct_strong=True,
                 min_latin_letters=None, numerals_bind_to_target=None):
        script_matcher(target_script)           # validate eagerly (section 2.2)
        self.target_script = target_script
        self.strip_terminal_punct = strip_terminal_punct
        self.numerals_bind_to_latin = (numerals_bind_to_latin
                                       if numerals_bind_to_target is None
                                       else numerals_bind_to_target)
        self.trailing_digits_bind = trailing_digits_bind
        self.max_bridge = max_bridge            # None == infinity (no limit)
        self.bidi_controls = bidi_controls      # "strip" | "preserve_pairs"
        self.min_target_letters = (min_target_letters if min_latin_letters is None
                                   else min_latin_letters)
        self.affinity_overrides = affinity_overrides or {}   # {codepoint: affinity}
        self.cjk_punct_strong = cjk_punct_strong


# ---------------------------------------------------------------------------
# Unicode property helpers
# ---------------------------------------------------------------------------
_RE_COMMON = regex.compile(r"\p{Script=Common}")
_RE_INHERITED = regex.compile(r"\p{Script=Inherited}")
_RE_PICTO = regex.compile(r"\p{Extended_Pictographic}")
_RE_RI = regex.compile(r"\p{Regional_Indicator}")


@lru_cache(maxsize=None)
def script_kind(ch, target_script):
    """One of 'Target', 'Common', 'Inherited', 'Other' for a single character.

    The base rule of spec section 5.1: a single equality test of the character's
    Script property against the configured target, with Common/Inherited held
    aside as the neutrals.
    """
    if script_matcher(target_script).match(ch):
        return "Target"
    if _RE_COMMON.match(ch):
        return "Common"
    if _RE_INHERITED.match(ch):
        return "Inherited"
    return "Other"


@lru_cache(maxsize=None)
def is_pictographic(ch):
    return bool(_RE_PICTO.match(ch))


@lru_cache(maxsize=None)
def is_regional_indicator(ch):
    return bool(_RE_RI.match(ch))


def is_target_letter(ch, target_script):
    """A category-L* character whose Script is the target (section 10.2)."""
    return (unicodedata.category(ch).startswith("L")
            and script_kind(ch, target_script) == "Target")


def classification_code_point(cluster, target_script):
    """Return (codepoint, kind) for a cluster's classification char (section 4.1).

    The first code point, unless its script is Inherited, in which case the first
    non-Inherited code point; if every code point is Inherited, the cluster is
    degenerate (bare combining marks) and 'Inherited' is returned.
    """
    first = cluster[0]
    kind = script_kind(first, target_script)
    if kind != "Inherited":
        return ord(first), kind
    for ch in cluster:
        kind = script_kind(ch, target_script)
        if kind != "Inherited":
            return ord(ch), kind
    return ord(first), "Inherited"


def cluster_is_stop(cluster):
    """Cluster-wide STOP test (spec section 7.3 step 3).

    STOP if any code point is Extended_Pictographic or Regional_Indicator, or the
    cluster contains U+FE0F (VS16) or U+20E3 (COMBINING ENCLOSING KEYCAP). This
    examines every code point so that a keycap sequence such as 1️⃣
    (U+0031 U+FE0F U+20E3) is STOP, not DIGIT.
    """
    for ch in cluster:
        o = ord(ch)
        if o == 0xFE0F or o == 0x20E3:
            return True
        if is_pictographic(ch) or is_regional_indicator(ch):
            return True
    return False


def affinity_of(cluster, ccp, policy):
    """Binding affinity of a NEUTRAL cluster (spec section 7.3, first match wins).

    Independent of the configured target script — only the neutral cluster itself
    and the policy overrides decide.
    """
    # 1. policy overrides, keyed by classification code point
    ov = policy.affinity_overrides.get(ccp)
    if ov is not None:
        return ov
    # 2. explicit code-point lists
    if ccp in EXPLICIT_RIGHT:
        return RIGHT
    if ccp in EXPLICIT_LEFT:
        return LEFT
    # 3. cluster-wide pictographic / RI / VS16 / keycap STOP test
    if cluster_is_stop(cluster):
        return STOP
    # 4. general category of the classification code point
    cat = unicodedata.category(chr(ccp))
    if cat in ("Ps", "Pi"):
        return RIGHT          # opening brackets / quotes
    if cat in ("Pe", "Pf"):
        return LEFT           # closing brackets / quotes
    if cat == "Sc":
        return RIGHT          # currency signs
    if cat == "Zs" or ccp == 0x09:
        return SEP            # whitespace (space chars and tab)
    if 0x30 <= ccp <= 0x39:
        return DIGIT          # ASCII digits only (section 7.3, 5.2e)
    # 5. everything else is traversable glue
    return SEP


# ---------------------------------------------------------------------------
# A classified grapheme cluster
# ---------------------------------------------------------------------------
class Cluster:
    __slots__ = ("s", "cls", "aff", "ccp", "start", "length")

    def __init__(self, s, cls, aff, ccp, start):
        self.s = s              # the grapheme cluster text
        self.cls = cls          # TARGET / OTHER / NEUTRAL / CONTROL / HARD_BREAK
        self.aff = aff          # binding affinity (NEUTRAL only), else None
        self.ccp = ccp          # classification code point (or None)
        self.start = start      # code-point offset of the cluster in the source
        self.length = len(s)    # length in code points


def classify_text(text, policy):
    """Return the list of classified Cluster objects for the whole text."""
    target = policy.target_script
    clusters = []
    offset = 0
    prev_class = None  # class of the nearest preceding non-degenerate cluster
    for s in regex.findall(r"\X", text):
        cp0 = ord(s[0])
        if cp0 in HARD_BREAKS:
            cls, aff, ccp = HARD_BREAK, None, cp0
            prev_class = None
        elif cp0 in BIDI_CONTROLS:
            cls, aff, ccp = CONTROL, None, cp0
            # controls do not update prev_class for the degenerate-mark rule
        else:
            ccp, kind = classification_code_point(s, target)
            if kind == "Target":
                cls = TARGET
            elif kind == "Other":
                cls = OTHER
            elif kind == "Inherited":
                # Degenerate all-Inherited cluster (section 5.2b): take the class
                # of the nearest preceding non-degenerate cluster, else NEUTRAL.
                cls = prev_class if prev_class in (TARGET, OTHER, NEUTRAL) else NEUTRAL
            else:  # Common
                if policy.cjk_punct_strong and ccp in CJK_STRONG:
                    cls = OTHER           # section 5.2e strengthening
                else:
                    cls = NEUTRAL
            aff = affinity_of(s, ccp, policy) if cls == NEUTRAL else None
            prev_class = cls
        clusters.append(Cluster(s, cls, aff, ccp, offset))
        offset += len(s)
    return clusters


# ---------------------------------------------------------------------------
# Directional neutral-resolution scans (spec section 7.4, 7.5, pseudocode 11.2)
# ---------------------------------------------------------------------------
def split_leading(members, policy):
    """Leading glue: L in {OTHER, EDGE}, R = TARGET. Scan right-to-left from the
    target-script edge; return the number of trailing clusters absorbed."""
    committed = len(members)  # index; clusters [committed:] are absorbed
    for i in range(len(members) - 1, -1, -1):
        a = members[i].aff
        if a == RIGHT:
            committed = i                       # anchor commits itself + provisionals
        elif a == DIGIT and policy.numerals_bind_to_latin:
            committed = i                       # knob: digit binds like an anchor
        elif a == SEP or a == DIGIT:
            pass                                # provisional; needs an anchor beyond
        else:                                    # LEFT or STOP
            break
    return len(members) - committed


def split_trailing(members, R, policy):
    """Trailing glue: L = TARGET, R in {OTHER, EDGE}. Scan left-to-right from the
    target-script edge; return the number of leading clusters absorbed."""
    committed = -1          # index; clusters [:committed + 1] are absorbed
    group_open = False
    group_end = -1          # index of the last DIGIT in the current group

    def close_group(at_run_end):
        nonlocal committed, group_open, group_end
        if group_open:
            # Section 7.5 rule 2: a closed digit group self-commits unless it
            # abuts following OTHER text at the run end (abutment exception).
            if policy.trailing_digits_bind and not (at_run_end and R == OTHER):
                committed = group_end
            group_open = False

    for i, m in enumerate(members):
        a = m.aff
        if a == LEFT:
            # Anchor commits through itself, CONSUMING any open digit group
            # (section 7.5 rule 1). Clearing group_open is load-bearing: a stale
            # group would let a later close_group() move the boundary backward
            # from the anchor to the last digit (spec section 11.2 note).
            committed = i
            group_open = False
        elif a == DIGIT:
            group_open = True
            group_end = i
        elif a == SEP:
            close_group(False)
        else:                                    # RIGHT or STOP
            close_group(False)
            break
    close_group(True)
    return committed + 1


# ---------------------------------------------------------------------------
# Union-find over target-run group ids (merges join sandwiched runs)
# ---------------------------------------------------------------------------
class _UF:
    def __init__(self):
        self.parent = {}

    def add(self, x):
        self.parent.setdefault(x, x)

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ---------------------------------------------------------------------------
# Bidi-control pair matching (spec section 8.2, preserve_pairs)
# ---------------------------------------------------------------------------
def match_control_pairs(clusters):
    """Return {start_offset: partner_start_offset} for matched isolate/embedding
    pairs. Standalone marks (LRM/RLM/ALM) and unmatched initiators/terminators
    are absent, so they are never retained (section 8.2)."""
    partner = {}
    iso_stack, emb_stack = [], []
    for c in clusters:
        if c.cls != CONTROL:
            continue
        cp = c.ccp
        if cp in _ISOLATE_INIT:
            iso_stack.append(c.start)
        elif cp == 0x2069:                       # PDI
            if iso_stack:
                o = iso_stack.pop()
                partner[o] = c.start
                partner[c.start] = o
        elif cp in _EMBED_INIT:
            emb_stack.append(c.start)
        elif cp == 0x202C:                       # PDF
            if emb_stack:
                o = emb_stack.pop()
                partner[o] = c.start
                partner[c.start] = o
    return partner


# ---------------------------------------------------------------------------
# Phase 4: trim, validate (spec section 10)
# ---------------------------------------------------------------------------
def _is_ws(s):
    return s != "" and all(unicodedata.category(c) == "Zs" or c == "\t" for c in s)


def trim(members, policy):
    """Remove edge whitespace and, if enabled, iteratively strip run-final
    terminal punctuation, re-trimming whitespace each step (section 10.1)."""
    lo, hi = 0, len(members)
    while lo < hi and _is_ws(members[lo].s):
        lo += 1
    while hi > lo and _is_ws(members[hi - 1].s):
        hi -= 1
    members = members[lo:hi]
    if policy.strip_terminal_punct:
        changed = True
        while changed and members:
            changed = False
            if members[-1].s in TERMINALS:
                members = members[:-1]
                changed = True
                while members and _is_ws(members[-1].s):
                    members = members[:-1]
    return members


def validate(members, policy):
    target = policy.target_script
    n = sum(1 for m in members for ch in m.s if is_target_letter(ch, target))
    return n >= policy.min_target_letters


# ---------------------------------------------------------------------------
# The extractor (spec section 11.2)
# ---------------------------------------------------------------------------
def extract_script_runs(text, policy=None):
    """Extract target-script runs from ``text``.

    Returns a list of ``(substring, start, end)`` tuples, in logical order, where
    ``start``/``end`` are code-point offsets into the original string. With the
    default policy the target script is Latin, reproducing spec v1.4 behaviour.
    """
    if policy is None:
        policy = Policy()

    all_clusters = classify_text(text, policy)

    # Section 8: resolve bidi controls before neutral resolution. Both modes run
    # the analysis on the control-free stream (controls never enter Phase 3); the
    # preserve_pairs mode differs only at emission, re-inserting fully contained
    # matched pairs.
    partner = (match_control_pairs(all_clusters)
               if policy.bidi_controls == "preserve_pairs" else {})
    analysis = [c for c in all_clusters if c.cls != CONTROL]

    # Section 6: coalesce into runs of uniform class. Each run keeps its member
    # clusters and their global indices into ``analysis``.
    runs = []  # list of [cls, start_index, end_index] (inclusive)
    for i, c in enumerate(analysis):
        if runs and runs[-1][0] == c.cls:
            runs[-1][2] = i
        else:
            runs.append([c.cls, i, i])

    def eff(run_pos):
        """Effective neighbour class TARGET | OTHER | EDGE."""
        if run_pos < 0 or run_pos >= len(runs):
            return EDGE
        cls = runs[run_pos][0]
        if cls == HARD_BREAK:
            return EDGE
        return cls

    # owner[i] is the group id of analysis[i], or None if the cluster is dropped.
    owner = [None] * len(analysis)
    uf = _UF()
    for r in runs:
        if r[0] == TARGET:
            gid = r[1]
            uf.add(gid)
            for i in range(r[1], r[2] + 1):
                owner[i] = gid

    def left_gid(k):
        prev = runs[k - 1]
        return owner[prev[2]]

    def right_gid(k):
        nxt = runs[k + 1]
        return owner[nxt[1]]

    # Section 7 / 11.2 decision + materialization. Decisions depend only on the
    # immutable Phase-2 run classes, so a single left-to-right pass is equivalent
    # to the reference decide-then-materialize semantics (section 11.1).
    for k, r in enumerate(runs):
        if r[0] != NEUTRAL:
            continue
        lo, hi = r[1], r[2]
        members = analysis[lo:hi + 1]
        L, R = eff(k - 1), eff(k + 1)
        if L == TARGET and R == TARGET:                    # section 7.1 sandwich
            if policy.max_bridge is None or len(members) <= policy.max_bridge:
                gl, gr = left_gid(k), right_gid(k)
                for i in range(lo, hi + 1):
                    owner[i] = gl
                uf.union(gl, gr)
            # else: bridge guard -> DISCARD (leave flanking runs separate)
        elif L != TARGET and R != TARGET:                  # section 7.2 isolation
            pass
        elif R == TARGET:                                  # section 7.4 leading
            m = split_leading(members, policy)
            gr = right_gid(k)
            for i in range(hi - m + 1, hi + 1):
                owner[i] = gr
        else:                                              # section 7.5 trailing
            kcount = split_trailing(members, R, policy)
            gl = left_gid(k)
            for i in range(lo, lo + kcount):
                owner[i] = gl

    # Assemble contiguous same-group segments into target-script runs.
    results = []
    i, n = 0, len(analysis)
    while i < n:
        if owner[i] is None:
            i += 1
            continue
        root = uf.find(owner[i])
        j = i
        while j + 1 < n and owner[j + 1] is not None and uf.find(owner[j + 1]) == root:
            j += 1
        members = trim(analysis[i:j + 1], policy)
        if members and validate(members, policy):
            start = members[0].start
            end = members[-1].start + members[-1].length
            substring = _emit_substring(members, start, end, all_clusters,
                                        partner, policy)
            results.append((substring, start, end))
        i = j + 1
    return results


def _emit_substring(members, start, end, all_clusters, partner, policy):
    """The emitted text of a run (section 4.2, 8.2)."""
    if policy.bidi_controls != "preserve_pairs":
        return "".join(m.s for m in members)
    # preserve_pairs: rebuild from the original clusters in [start, end),
    # retaining a bidi control only if it belongs to a matched pair whose partner
    # is also within this run's span (fully contained -> keep; straddling or
    # unmatched -> shed). This guarantees no unmatched control is ever emitted.
    parts = []
    for c in all_clusters:
        if c.start < start or c.start >= end:
            continue
        if c.cls == CONTROL:
            p = partner.get(c.start)
            if p is not None and start <= p < end:
                parts.append(c.s)
        else:
            parts.append(c.s)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------
def _parse_codepoint(token):
    """Parse 'U+00AE', '0xAE', or 'AE' into an int."""
    t = token.strip()
    tl = t.lower()
    if tl.startswith("u+"):
        t = t[2:]
    elif tl.startswith("0x"):
        t = t[2:]
    try:
        return int(t, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "codepoint must be hex (e.g. U+00AE, 0xAE, AE): %r" % token)


def _parse_affinity_override(token):
    """Parse 'U+00AE=RIGHT' into (codepoint, affinity)."""
    if "=" not in token:
        raise argparse.ArgumentTypeError(
            "expected CODEPOINT=AFFINITY, e.g. U+00AE=RIGHT: %r" % token)
    cp_tok, aff = token.split("=", 1)
    aff = aff.strip().upper()
    valid = {RIGHT, LEFT, SEP, DIGIT, STOP}
    if aff not in valid:
        raise argparse.ArgumentTypeError(
            "affinity must be one of %s: %r" % (", ".join(sorted(valid)), token))
    return _parse_codepoint(cp_tok), aff


def _parse_script(token):
    """Validate a --script value, reporting unknown names as a usage error."""
    try:
        script_matcher(token)
    except ScriptError as exc:
        raise argparse.ArgumentTypeError(str(exc))
    return token.strip()


def _parse_max_bridge(token):
    t = token.strip().lower()
    if t in ("inf", "none", "infinity", ""):
        return None
    try:
        v = int(t)
    except ValueError:
        raise argparse.ArgumentTypeError("max-bridge must be an integer or 'inf'")
    if v < 0:
        raise argparse.ArgumentTypeError("max-bridge must be non-negative")
    return v


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="script-runs",
        description="Extract embedded runs of one Unicode script from "
                    "mixed-script text (spec v2.0).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("file", nargs="?", metavar="FILE",
                   help="text file to read (UTF-8); omit to read from stdin")
    p.add_argument("-s", "--script", type=_parse_script, default=DEFAULT_SCRIPT,
                   metavar="SCRIPT",
                   help="Unicode Script to extract, e.g. Greek, Cyrillic, Arabic, "
                        "Hebrew (default: %s)" % DEFAULT_SCRIPT)
    p.add_argument("--encoding", default="utf-8",
                   help="input encoding (default: utf-8)")
    p.add_argument("--json", action="store_true",
                   help="emit JSON Lines (one object per run) instead of a table")
    p.add_argument("--unicode-version", action="store_true",
                   help="print the active Unicode data versions and exit")

    g = p.add_argument_group("policy knobs (spec section 9)")
    g.add_argument("--no-strip-terminal-punct", dest="strip_terminal_punct",
                   action="store_false",
                   help="keep trailing . , ; : ! ? captured as trailing glue")
    g.add_argument("--numerals-bind", "--numerals-bind-to-latin",
                   dest="numerals_bind_to_latin", action="store_true",
                   help="leading digit groups bind to the target script without a "
                        "RIGHT anchor (captures a bare '2026 Windows')")
    g.add_argument("--no-trailing-digits-bind", dest="trailing_digits_bind",
                   action="store_false",
                   help="trailing digit groups stay provisional (symmetric with "
                        "leading behaviour)")
    g.add_argument("--max-bridge", type=_parse_max_bridge, default=None,
                   metavar="N",
                   help="max neutral-run length (clusters) for the sandwich merge; "
                        "'inf' (default) means no limit")
    g.add_argument("--bidi-controls", choices=("strip", "preserve_pairs"),
                   default="strip",
                   help="how to handle bidi formatting characters (default: strip)")
    g.add_argument("--min-target-letters", "--min-latin-letters",
                   dest="min_target_letters", type=int, default=1, metavar="N",
                   help="minimum target-script letters for a run to be emitted "
                        "(default: 1)")
    g.add_argument("--affinity-override", action="append", default=[],
                   type=_parse_affinity_override, metavar="CP=AFFINITY",
                   help="override a cluster's binding affinity, e.g. "
                        "U+00AE=RIGHT (repeatable)")
    g.add_argument("--no-cjk-punct-strong", dest="cjk_punct_strong",
                   action="store_false",
                   help="do not strengthen CJK punctuation / full-width forms to "
                        "strong (they become neutral glue)")
    return p


def policy_from_args(args):
    return Policy(
        target_script=args.script,
        strip_terminal_punct=args.strip_terminal_punct,
        numerals_bind_to_latin=args.numerals_bind_to_latin,
        trailing_digits_bind=args.trailing_digits_bind,
        max_bridge=args.max_bridge,
        bidi_controls=args.bidi_controls,
        min_target_letters=args.min_target_letters,
        affinity_overrides=dict(args.affinity_override),
        cjk_punct_strong=args.cjk_punct_strong,
    )


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    # Force UTF-8 output so runs and offsets print correctly on legacy consoles.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if args.unicode_version:
        rv = getattr(regex, "__version__", "?")
        print("regex module: %s (its bundled UCD drives Script/grapheme data)" % rv)
        print("unicodedata:  Unicode %s (general categories)"
              % unicodedata.unidata_version)
        return 0

    # Read the input.
    try:
        if args.file is None:
            data = sys.stdin.buffer.read()
        else:
            with open(args.file, "rb") as fh:
                data = fh.read()
        text = data.decode(args.encoding)
    except OSError as exc:
        sys.stderr.write("script-runs: cannot read input: %s\n" % exc)
        return 1
    except (UnicodeDecodeError, LookupError) as exc:
        sys.stderr.write("script-runs: cannot decode input as %s: %s\n"
                         % (args.encoding, exc))
        return 1

    runs = extract_script_runs(text, policy_from_args(args))

    if args.json:
        for substring, start, end in runs:
            sys.stdout.write(json.dumps(
                {"text": substring, "start": start, "end": end,
                 "script": args.script},
                ensure_ascii=False) + "\n")
    else:
        for substring, start, end in runs:
            print("%6d %6d  %s" % (start, end, substring))
    return 0


if __name__ == "__main__":
    sys.exit(main())
