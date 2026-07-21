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
Text", specification v2.2 (see docs/script-run-extraction-spec.md). The hard part
is deciding which script-neutral characters (Unicode Script=Common or Inherited)
fold into a target-script run and which act as a bridge to the surrounding text
or dangle without a host. The spec adapts the neutral-resolution skeleton of the
Unicode Bidirectional Algorithm (UAX #9, rules N1-N2); this file follows the
normative pseudocode of the spec's section 11.2 phase for phase:

    1. Classify each grapheme cluster (TARGET / OTHER / NEUTRAL / CONTROL /
       HARD_BREAK) from its "classification code point" (spec section 4.1, 5).
    2. Coalesce adjacent equal-class clusters into runs (section 6).
    3. Resolve bidi-control clusters (section 8; default: strip + re-coalesce.
       With --isolate-binds, matched isolate boundaries are kept as walls
       that bind their interior neutrals inward -- section 8.5).
    4. Resolve each NEUTRAL run against its strong neighbours: merge when
       sandwiched between target-script text (section 7.1), discard when isolated
       (7.2), or split directionally at mixed boundaries using per-cluster
       binding affinity (7.3-7.5). With --straight-quotes, a boundary " or '
       first takes opener/closer affinity from its spacing (section 7.3a).
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
active versions are printed by ``--unicode-version``, and ``--help`` lists every
Script value that data offers as a target. The spec's minimum
reference is Unicode 16.0; character-level differences from a documented later
UCD version are not conformance failures (spec section 2.2).

Usage:
    script-runs [FILE] [--script SCRIPT] [policy options]
    script-runs --help                  # ends with every script you may target
    script-runs --script Greek --json report.txt
    echo '한국어 Windows 11 (23H2)' | script-runs
    echo '한국어 Αθήνα 2026 텍스트' | script-runs --script Greek
"""

import argparse
import json
import shutil
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

# The two straight (ASCII) quotation marks. Both are General_Category Po, so the
# §7.3 derivation gives them the default SEP affinity; the optional contextual
# rule of §7.3a may promote a boundary occurrence to RIGHT (opener) or LEFT
# (closer). Curly quotes are Pi/Pf and already resolve to RIGHT/LEFT by category.
STRAIGHT_QUOTES = frozenset({0x0022, 0x0027})   # " and '


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


# Fallback suggestions, used only in error messages when the active UCD's script
# table cannot be read (see available_scripts).
_SCRIPT_EXAMPLES = ("Latin", "Greek", "Cyrillic", "Arabic", "Hebrew", "Armenian",
                    "Georgian", "Devanagari", "Thai", "Hangul", "Han")

# The four ISO 15924 special codes. None of them identifies a script: Zyyy and
# Zinh are the neutrals this algorithm resolves rather than extracts, Zzzz is
# "unknown", and Hrkt exists only for Script_Extensions (no character has
# Script=Hrkt, so it would match nothing). A target must be a real script.
_NON_SCRIPTS = frozenset({"common", "zyyy", "inherited", "qaai", "zinh",
                          "unknown", "zzzz", "katakana_or_hiragana", "hrkt"})

_RE_SCRIPT_NAME = regex.compile(r"\A[A-Za-z][A-Za-z_ ]*\Z")


def _normalize_script(name):
    """UCD loose matching: case, spaces and underscores are insignificant."""
    return name.replace("_", "").replace(" ", "").replace("-", "").upper()


# Preferred spellings for display. The set of scripts always comes from the
# active UCD (available_scripts); this table only makes the names pretty, since
# the regex module's reverse table stores them folded to upper case with the
# separators dropped (ANATOLIANHIEROGLYPHS) and sometimes keeps the 4-letter
# alias as the canonical form (HANI for Han). Every spelling here is accepted by
# the matcher as-is — loose matching means a stale or missing entry can only
# cost prettiness, never correctness — and tests/test_script_runs.py asserts each
# one still resolves to the script it names. Anything absent falls back to
# title case, which is also accepted.
_SCRIPT_DISPLAY = {
    "ANATOLIANHIEROGLYPHS": "Anatolian_Hieroglyphs",
    "BASSAVAH": "Bassa_Vah",
    "BERIAERFE": "Beria_Erfe",
    "CANADIANABORIGINAL": "Canadian_Aboriginal",
    "CAUCASIANALBANIAN": "Caucasian_Albanian",
    "CYPROMINOAN": "Cypro_Minoan",
    "DIVESAKURU": "Dives_Akuru",
    "EGYPTIANHIEROGLYPHS": "Egyptian_Hieroglyphs",
    "GUNJALAGONDI": "Gunjala_Gondi",
    "GURUNGKHEMA": "Gurung_Khema",
    "HANI": "Han",
    "HANIFIROHINGYA": "Hanifi_Rohingya",
    "IMPERIALARAMAIC": "Imperial_Aramaic",
    "INSCRIPTIONALPAHLAVI": "Inscriptional_Pahlavi",
    "INSCRIPTIONALPARTHIAN": "Inscriptional_Parthian",
    "KAYAHLI": "Kayah_Li",
    "KHITANSMALLSCRIPT": "Khitan_Small_Script",
    "KIRATRAI": "Kirat_Rai",
    "LAOO": "Lao",
    "LINEARA": "Linear_A",
    "LINEARB": "Linear_B",
    "MASARAMGONDI": "Masaram_Gondi",
    "MEETEIMAYEK": "Meetei_Mayek",
    "MENDEKIKAKUI": "Mende_Kikakui",
    "MEROITICCURSIVE": "Meroitic_Cursive",
    "MEROITICHIEROGLYPHS": "Meroitic_Hieroglyphs",
    "MROO": "Mro",
    "NAGMUNDARI": "Nag_Mundari",
    "NEWTAILUE": "New_Tai_Lue",
    "NKOO": "Nko",
    "NYIAKENGPUACHUEHMONG": "Nyiakeng_Puachue_Hmong",
    "OLCHIKI": "Ol_Chiki",
    "OLDHUNGARIAN": "Old_Hungarian",
    "OLDITALIC": "Old_Italic",
    "OLDNORTHARABIAN": "Old_North_Arabian",
    "OLDPERMIC": "Old_Permic",
    "OLDPERSIAN": "Old_Persian",
    "OLDSOGDIAN": "Old_Sogdian",
    "OLDSOUTHARABIAN": "Old_South_Arabian",
    "OLDTURKIC": "Old_Turkic",
    "OLDUYGHUR": "Old_Uyghur",
    "OLONAL": "Ol_Onal",
    "PAHAWHHMONG": "Pahawh_Hmong",
    "PAUCINHAU": "Pau_Cin_Hau",
    "PHAGSPA": "Phags_Pa",
    "PSALTERPAHLAVI": "Psalter_Pahlavi",
    "SIGNWRITING": "SignWriting",
    "SORASOMPENG": "Sora_Sompeng",
    "SYLOTINAGRI": "Syloti_Nagri",
    "TAILE": "Tai_Le",
    "TAITHAM": "Tai_Tham",
    "TAIVIET": "Tai_Viet",
    "TAIYO": "Tai_Yo",
    "TOLONGSIKI": "Tolong_Siki",
    "TULUTIGALARI": "Tulu_Tigalari",
    "VAII": "Vai",
    "WARANGCITI": "Warang_Citi",
    "YIII": "Yi",
    "ZANABAZARSQUARE": "Zanabazar_Square",
}


@lru_cache(maxsize=None)
def available_scripts():
    """Every Script value the active UCD offers as a target, sorted for display.

    Read out of the regex module's own property tables, so the list tracks
    whichever UCD that module ships (spec section 2.2) instead of a list baked in
    here. Those tables are private API; if a future release moves them, the
    listing degrades to the example names rather than failing.
    """
    try:
        prop_id, values = regex._regex_core.PROPERTIES["SCRIPT"]
        canonical = regex._regex_core.PROPERTY_NAMES[prop_id][1]
    except Exception:      # pragma: no cover - depends on the regex release
        return _SCRIPT_EXAMPLES
    skip = {values[n] for n in (_normalize_script(s) for s in _NON_SCRIPTS)
            if n in values}
    names = [_SCRIPT_DISPLAY.get(name, name.title())
             for ident, name in canonical.items() if ident not in skip]
    return tuple(sorted(names))


@lru_cache(maxsize=None)
def script_matcher(script):
    """Compile \\p{Script=...} for ``script``, raising ScriptError if invalid.

    The name is validated before interpolation so a value like ``Latin}`` cannot
    smuggle syntax into the pattern.
    """
    name = script.strip()
    listing = "run --help for the %d scripts the active UCD offers" % len(
        available_scripts())
    if not _RE_SCRIPT_NAME.match(name):
        raise ScriptError(
            "%r is not a Unicode Script name (letters, spaces and underscores "
            "only); %s" % (script, listing))
    if _normalize_script(name).lower() in {_normalize_script(s).lower()
                                           for s in _NON_SCRIPTS}:
        raise ScriptError(
            "%r names no script; the target must be a real script — %s"
            % (script, listing))
    try:
        return regex.compile(r"\p{Script=%s}" % name)
    except regex.error:
        raise ScriptError(
            "unknown Unicode Script %r for the active UCD (Unicode %s); %s "
            "(a 4-letter script alias such as Cyrl works too)"
            % (script, unicodedata.unidata_version, listing))


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
        "isolate_binds", "straight_quotes_contextual",
    )

    def __init__(self, target_script=DEFAULT_SCRIPT, strip_terminal_punct=True,
                 numerals_bind_to_latin=False, trailing_digits_bind=True,
                 max_bridge=None, bidi_controls="strip", min_target_letters=1,
                 affinity_overrides=None, cjk_punct_strong=True,
                 isolate_binds=False, straight_quotes_contextual=False,
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
        self.isolate_binds = isolate_binds       # section 8.5
        self.straight_quotes_contextual = straight_quotes_contextual  # section 7.3a


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
    __slots__ = ("s", "cls", "aff", "ccp", "start", "length", "bind")

    def __init__(self, s, cls, aff, ccp, start, bind=None):
        self.s = s              # the grapheme cluster text
        self.cls = cls          # TARGET / OTHER / NEUTRAL / CONTROL / HARD_BREAK
        self.aff = aff          # binding affinity (NEUTRAL only), else None
        self.ccp = ccp          # classification code point (or None)
        self.start = start      # code-point offset of the cluster in the source
        self.length = len(s)    # length in code points
        self.bind = bind        # isolate wall: RIGHT/LEFT inward direction (8.5)


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
def split_leading(members, policy, bind=False):
    """Leading glue: L in {OTHER, EDGE}, R = TARGET. Scan right-to-left from the
    target-script edge; return the number of trailing clusters absorbed.

    ``bind`` is set when the left neighbour is the opening boundary of a matched
    isolate (section 8.5). The isolate guarantees these neutrals lie inside the
    span and so cannot belong to the outer text: run exhaustion then COMMITS the
    provisionals instead of releasing them. Anchors and STOPs still halt the scan
    first, so the bind only ever supplies the missing anchor, never overrides one.
    """
    committed = len(members)  # index; clusters [committed:] are absorbed
    exhausted = True
    for i in range(len(members) - 1, -1, -1):
        a = members[i].aff
        if a == RIGHT:
            committed = i                       # anchor commits itself + provisionals
        elif a == DIGIT and policy.numerals_bind_to_latin:
            committed = i                       # knob: digit binds like an anchor
        elif a == SEP or a == DIGIT:
            pass                                # provisional; needs an anchor beyond
        else:                                    # LEFT or STOP
            exhausted = False
            break
    if exhausted and bind:
        committed = 0                           # isolate boundary binds inward
    return len(members) - committed


def split_trailing(members, R, policy, bind=False):
    """Trailing glue: L = TARGET, R in {OTHER, EDGE}. Scan left-to-right from the
    target-script edge; return the number of leading clusters absorbed.

    ``bind`` is the section 8.5 mirror of ``split_leading``'s: a matched isolate's
    closing boundary commits on run exhaustion. Because the boundary also makes
    ``R`` EDGE rather than OTHER, the abutment exception cannot fire against text
    the isolate has already excluded from the span.
    """
    committed = -1          # index; clusters [:committed + 1] are absorbed
    exhausted = True
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
            exhausted = False
            break
    close_group(True)
    if exhausted and bind:
        # Section 8.5: the isolate's own boundary is the anchor. This deliberately
        # outranks trailing_digits_bind = off — an explicit per-instance signal
        # beats a corpus-wide heuristic about trailing numerals.
        committed = len(members) - 1
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


def _is_space_like(c):
    """True for the neighbours a quote may open after / close before (§7.3a).

    That is: an EDGE (string end, ``None``), a wall (HARD_BREAK, including an
    isolate wall under §8.5), or whitespace. Non-space separators (`-`, `/`) are
    deliberately excluded — a quote wedged against one is left ambiguous (SEP).
    """
    if c is None or c.cls == HARD_BREAK:
        return True
    if c.ccp is None:
        return False
    return unicodedata.category(chr(c.ccp)) == "Zs" or c.ccp == 0x09


def resolve_straight_quotes(analysis):
    """Contextual affinity for U+0022 / U+0027 (spec §7.3a); in place.

    Runs on the CONTROL-RESOLVED stream (after §8), so a quote's neighbours are
    real text, never a stripped control. A straight quote becomes an opener
    (``RIGHT``) when it follows space/EDGE/an opening anchor and is *not* itself
    followed by space; a closer (``LEFT``) when it follows non-space and precedes
    space/EDGE/a closing anchor. Anything else keeps the default ``SEP``.

    Decisions are computed against the pre-pass affinities of neighbours and then
    applied together, so the result does not depend on iteration order (§11.1).
    The ambiguous apostrophe cases (`don't`, `O'Brien`, `5'10"`) never reach this
    rule: they sit inside a TARGET…TARGET sandwich, which §7.1 resolves without
    ever consulting affinity.
    """
    decisions = []
    for i, c in enumerate(analysis):
        if c.cls != NEUTRAL or c.ccp not in STRAIGHT_QUOTES:
            continue
        prev = analysis[i - 1] if i > 0 else None
        nxt = analysis[i + 1] if i + 1 < len(analysis) else None
        space_after = _is_space_like(nxt)
        open_before = _is_space_like(prev) or (
            prev is not None and prev.cls == NEUTRAL and prev.aff == RIGHT)
        close_before = (not _is_space_like(prev)) and (
            space_after or (nxt is not None and nxt.cls == NEUTRAL
                            and nxt.aff == LEFT))
        if open_before and not space_after:
            decisions.append((i, RIGHT))       # opener facing following text
        elif close_before:
            decisions.append((i, LEFT))         # closer facing preceding text
        # else: unbalanced / wedged -> leave at SEP (conservative)
    for i, aff in decisions:
        analysis[i].aff = aff


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

    # Section 8.5: with isolate_binds on, the boundaries of a MATCHED isolate
    # survive control resolution as HARD_BREAK-class walls carrying an inward
    # bind direction. Everything else -- unmatched controls, standalone marks and
    # every embedding (LRE/RLE/LRO/RLO...PDF, which do not isolate their content
    # the way LRI/RLI/FSI...PDI do) -- is shed exactly as before.
    iso = match_control_pairs(all_clusters) if policy.isolate_binds else {}
    analysis = []
    for c in all_clusters:
        if c.cls != CONTROL:
            analysis.append(c)
        elif c.start in iso and (c.ccp in _ISOLATE_INIT or c.ccp == 0x2069):
            analysis.append(Cluster(c.s, HARD_BREAK, None, c.ccp, c.start,
                                    RIGHT if c.ccp in _ISOLATE_INIT else LEFT))

    # Section 7.3a: contextual straight-quote affinity, on the control-resolved
    # stream so a quote's neighbours are real text (and, under §8.5, isolate
    # walls) rather than stripped controls.
    if policy.straight_quotes_contextual:
        resolve_straight_quotes(analysis)

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

    def wall_bind(run_pos, direction):
        """True if the EDGE at ``run_pos`` is an isolate wall binding inward.

        ``direction`` is RIGHT for a left neighbour (its closing cluster faces
        us) and LEFT for a right neighbour (its opening cluster does). Real hard
        breaks carry bind = None and so never bind (section 7.6 is unchanged).
        """
        if run_pos < 0 or run_pos >= len(runs):
            return False                        # string edge, never an isolate
        r = runs[run_pos]
        if r[0] != HARD_BREAK:
            return False
        c = analysis[r[2] if direction == RIGHT else r[1]]
        return c.bind == direction

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
            m = split_leading(members, policy, bind=wall_bind(k - 1, RIGHT))
            gr = right_gid(k)
            for i in range(hi - m + 1, hi + 1):
                owner[i] = gr
        else:                                              # section 7.5 trailing
            kcount = split_trailing(members, R, policy,
                                    bind=wall_bind(k + 1, LEFT))
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


def _columns(names, width, indent="  ", gap=2):
    """Lay ``names`` out in as many equal columns as ``width`` allows."""
    cell = max(len(n) for n in names) + gap
    cols = max(1, (width - len(indent) + gap) // cell)
    rows = -(-len(names) // cols)            # ceiling division
    lines = []
    for r in range(rows):
        row = [names[c * rows + r] for c in range(cols) if c * rows + r < len(names)]
        lines.append((indent + "".join(n.ljust(cell) for n in row)).rstrip())
    return lines


def _script_epilog():
    """The --help listing of every script the active UCD accepts as a target."""
    names = available_scripts()
    try:
        width = min(shutil.get_terminal_size(fallback=(80, 24)).columns, 100)
    except Exception:                        # pragma: no cover - odd terminals
        width = 80
    head = ("available scripts (%d, from the Unicode data the 'regex' module "
            "ships):" % len(names))
    tail = ("Case, spaces and underscores are insignificant, and each script's "
            "4-letter\nISO 15924 alias works too, so Old_Hungarian, "
            "old hungarian and Hung are one\nand the same. Common, Inherited, "
            "Unknown and Katakana_Or_Hiragana are not\nscripts and are rejected."
            " Extracting several scripts means one run each.")
    return "\n".join([head] + _columns(list(names), width) + ["", tail])


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="script-runs",
        description="Extract embedded runs of one Unicode script from "
                    "mixed-script text (spec v2.2).",
        epilog=_script_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("file", nargs="?", metavar="FILE",
                   help="text file to read (UTF-8); omit to read from stdin")
    p.add_argument("-s", "--script", type=_parse_script, default=DEFAULT_SCRIPT,
                   metavar="SCRIPT",
                   help="Unicode Script to extract, e.g. Greek, Cyrillic, "
                        "Arabic, Hebrew (default: %s); the full list of %d is "
                        "at the end of this help"
                        % (DEFAULT_SCRIPT, len(available_scripts())))
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
    g.add_argument("--isolate-binds", dest="isolate_binds", action="store_true",
                   help="treat a matched isolate (LRI/RLI/FSI...PDI) as a wall "
                        "whose interior neutrals bind inward, so '<rtl> ⁦\"12 "
                        "Main St.\"⁩ <rtl>' keeps the quotes and number")
    g.add_argument("--straight-quotes", dest="straight_quotes_contextual",
                   action="store_true",
                   help='give a boundary straight quote (" or \') opener/closer '
                        "affinity from its spacing, so a quoted phrase keeps its "
                        "quotes at a run edge (spec 7.3a)")
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
        isolate_binds=args.isolate_binds,
        straight_quotes_contextual=args.straight_quotes_contextual,
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
