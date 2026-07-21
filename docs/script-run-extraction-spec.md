# Script Run Extraction from Mixed-Script Text

**Specification, Version 2.0 (Draft) — Parameterized Target Script**

*Supersedes: Latin Run Extraction from Mixed-Script Text, Version 1.4. An implementation of this specification configured with `target_script = Latin` MUST be behaviorally identical to a conforming v1.4 implementation (§2.1).*

---

## 1. Introduction

### 1.1 Problem Statement

Given a Unicode string whose primary content is written in one or more scripts, possibly including right-to-left (RTL) scripts, extract every maximal contiguous run of embedded text belonging to a single configured **target script** — for example, English phrases, product names, URLs, version strings, and copyright notices when the target script is Latin; or embedded Greek, Cyrillic, Arabic, or Hebrew phrases when those are the target.

This specification generalizes *Latin Run Extraction v1.4* into a script-parameterized extractor. Rather than extracting Latin-script runs specifically, the implementation extracts runs belonging to a single configured script, `target_script`:

```
target_script = Latin
target_script = Greek
target_script = Cyrillic
target_script = Arabic
target_script = Hebrew
```

All neutral-resolution behavior, bidi handling, affinity rules, digit handling, hard-break handling, and trimming semantics carry over from v1.4 unchanged; only the identity of the extracted script is a parameter. Conceptually, v1.4 already behaved as a generic "extract target-script runs from mixed-script text" algorithm with Latin hardcoded as the target.

The central difficulty is the treatment of **language-neutral characters**: digits, whitespace, punctuation, and symbols whose Unicode script property is `Common` or `Inherited`. These characters carry no script identity of their own, yet they are frequently an integral part of a target-script entity. A correct extractor must fold neutral characters into a target-script run when they logically belong to it, in all three positions (examples shown for `target_script = Latin`):

- **Internal** — neutrals joining target-script components: `Windows 11 (23H2)`, `https://example.com/path?q=1`
- **Trailing** — neutrals appended to a target-script run: `macOS™`, `100 GB+`
- **Leading** — neutral sequences preceding a target-script run: `© 2026 Watch Tower Bible and Tract Society of Pennsylvania`

The same structures arise for every target script; with `target_script = Greek`, the input `한국어 Αθήνα 2026 텍스트` yields the single run `Αθήνα 2026` by exactly the rules that yield `Windows 11` for Latin.

Simultaneously, the extractor must **exclude** neutral characters when they act as a bridge between target-script and other-script text, or when they dangle without a target-script host. A comma separating a Korean clause from an English phrase belongs to neither run's output; a bare year adjacent to target-script text with no anchoring symbol must not be captured.

Naive approaches fail:

- A regex over a script's letter set plus permissive neutral classes cannot express *conditional* absorption ("absorb this separator only if an anchoring symbol lies beyond it") without pathological lookahead, and either over-captures (dragging in sentence punctuation from the surrounding text) or under-captures (splitting `Windows 11 (23H2)` into fragments).
- Splitting on script boundaries alone discards all neutral glue, producing `Windows`, `11`, `23H2` as separate fragments and losing `©`, `™`, parentheses, and URLs' structure entirely.

### 1.2 Design Insight

The problem is structurally identical to the **neutral-type resolution phase of the Unicode Bidirectional Algorithm** (UAX #9, rules N1–N2): both must decide which strong context a run of weak/neutral characters belongs to. This specification adapts that skeleton:

1. Classify each character by script strength, relative to the configured target script.
2. Coalesce into maximal runs of uniform class.
3. Resolve each neutral run by examining its strong neighbors — merging when flanked by target-script text on both sides, discarding when flanked by other-script text, and **splitting directionally** at mixed boundaries using per-cluster *binding affinity*.

All processing occurs in **logical order** (storage order), never visual order. This is what makes the algorithm indifferent to RTL display: Arabic and Hebrew are strong scripts like any other — whether as target or as other script — and display-time reordering never enters the computation. Note in particular that the leading-anchor rules are independent of script identity: `עברית © 2026 العربية סוף` with `target_script = Arabic` yields `© 2026 العربية` by the same mechanics that yield case 4's Latin copyright line.

### 1.3 Scope and Non-Goals

**In scope:** segmentation and extraction of target-script runs, including attached neutral glue, from any Unicode string, for any single configured Unicode Script value; correct behavior in the presence of RTL scripts, bidi control characters, and multiple digit families.

**Out of scope:** display/rendering of extracted runs (§8.4 notes one consequence); simultaneous extraction of multiple target scripts in a single pass (run the extractor once per script); language identification within target-script runs (English vs. French within Latin, etc.); spell-aware or dictionary-aware segmentation; normalization policy of the input (the algorithm operates on the input as given; see §4.3).

---

## 2. Conformance and Conventions

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are to be interpreted as described in RFC 2119.

Conformance has two tiers:

- **Core conformance:** an implementation MUST produce the outputs specified in §12 test cases marked *Core*.
- **Full conformance:** an implementation additionally satisfies the cases marked *Full*, which exercise bidi controls, non-European digit families, and policy defaults.

Conformance is evaluated per configuration: the §12 suite is normative for `target_script = Latin` (§12.1), and the generalization cases of §12.2 are normative for the target scripts they name. Policy knobs (§9) allow deviation where explicitly noted; a conforming implementation MUST document any non-default knob settings, including the configured `target_script`.

### 2.1 Backward compatibility with Version 1.4 (normative)

The following requirement is normative:

> An implementation of this specification configured with `target_script = Latin` MUST produce identical classifications, neutral-resolution decisions, trimming behavior, validation results, offsets, and extracted substrings as a conforming implementation of *Latin Run Extraction v1.4*, for all conforming inputs and test cases.

Consequently, `v2(target_script = Latin)` is behaviorally equivalent to `v1.4`. The extraction behavior for `target_script = Latin` is bit-for-bit identical to v1.4; this preserves the entire existing v1.4 conformance suite and its companion fixture (§12.1) while allowing Greek, Cyrillic, Arabic, Hebrew, and other target scripts.

Every normative rule in this document is the v1.4 rule with the class names systematically renamed (`LATIN` → `TARGET`, `BASE` → `OTHER`, "Latin run" → "target-script run", `min_latin_letters` → `min_target_letters`) and the script identity drawn from configuration. No neutral-resolution, affinity, digit, control, hard-break, or trimming logic differs.

### 2.2 Unicode data version

This specification depends normatively on UAX #29 segmentation and on the `Script`, `Script_Extensions`, `General_Category`, `Bidi_Paired_Bracket_Type`, `Extended_Pictographic`, `Pattern_White_Space`, and `Regional_Indicator` properties and the bidi-control character definitions, all of which are versioned with the Unicode Character Database. A conforming implementation MUST document the UCD version it uses, and conformance test results MUST record that version. This specification is written against Unicode 16.0 as the minimum reference version; character-level differences arising from later UCD versions are not conformance failures if documented.

`target_script` values are Unicode `Script` property values (e.g., `Latin`, `Greek`, `Cyrillic`, `Arabic`, `Hebrew`, `Armenian`, `Georgian`). An implementation MUST reject a configured value that is not a valid `Script` value for its documented UCD version.

---

## 3. Terminology

- **Code point:** a Unicode scalar value.
- **Grapheme cluster:** an extended grapheme cluster per UAX #29. The atomic unit of this algorithm; no run boundary may fall inside one.
- **Logical order:** the order of code points in storage (the order in which text is typed/read), as opposed to visual display order.
- **Target script:** the Unicode `Script` value selected by configuration (`target_script`, §9) and designated as the script to be extracted. Examples: `Latin`, `Greek`, `Cyrillic`, `Arabic`, `Hebrew`, `Armenian`, `Georgian`.
- **Other script:** any strong script that is not the configured target script.
- **Strong character:** a character whose script property identifies a specific script (Latin, Hangul, Arabic, …) — classified `TARGET` or `OTHER` depending on whether that script is the configured target.
- **Neutral character:** a character of script `Common` or `Inherited` (subject to reassignment rules in §5).
- **Run:** a maximal contiguous sequence of grapheme clusters sharing one class.
- **Binding affinity:** a per-cluster property (§7.3) indicating whether a neutral cluster attaches to the preceding text, the following text, or neither.
- **Anchor:** a neutral cluster whose affinity points toward the target-script run in a directional scan — `RIGHT` affinity in leading scans (§7.4), `LEFT` affinity in trailing scans (§7.5) — and whose absorption commits all provisionally held clusters between it and the run. Trailing digit groups additionally act as anchors under §7.5 rule 2.

---

## 4. Data Model and Preprocessing

### 4.1 Input

A Unicode string in logical order. The implementation MUST iterate by grapheme cluster (UAX #29 extended grapheme clusters). Classification (§5) is applied to the **classification code point** of each cluster; the cluster is then treated atomically.

The classification code point is defined operationally as follows:

1. Take the **first code point** of the cluster.
2. If its `Script` property is `Inherited`, take instead the first code point in the cluster whose script is **not** `Inherited`.
3. If every code point in the cluster is `Inherited` (degenerate input: a cluster of bare combining marks), the cluster classifies per §5.2(b).

This rule is deliberately mechanical rather than linguistic: prepended format characters with a strong script (e.g., U+0600 ARABIC NUMBER SIGN) correctly pull the cluster to that script's class (`TARGET` when it is the configured target, otherwise `OTHER`); emoji sequences classify by their first code point (typically `Common`, hence `NEUTRAL`, subject to the `STOP` affinity in §7.3); regional-indicator pairs classify as `NEUTRAL`. Implementations MUST use this rule (or one producing identical results for all clusters) so that independent implementations classify identically.

### 4.2 Output

An ordered list of extracted target-script runs, each carrying:

- the substring (or, RECOMMENDED, `(start, end)` offsets into the original string — offsets preserve provenance and avoid copying),
- the offsets expressed in a documented unit (code points, UTF-8 bytes, or UTF-16 units — the implementation MUST state which).

### 4.3 Normalization

The algorithm does not require a particular normalization form. Implementations SHOULD document whether they normalize input (e.g., to NFC) before processing. Note that under NFD, combining marks appear as separate code points with script `Inherited`; rule §5.2(b) ensures they classify with their base character regardless.

---

## 5. Phase 1 — Character Classification

Each grapheme cluster is assigned exactly one class:

| Class | Meaning |
|---|---|
| `TARGET` | Strong character of the configured target script |
| `OTHER` | Strong character of any other script (relative to the target: e.g., Hangul, Han, Arabic, Hebrew, Latin, …) |
| `NEUTRAL` | Script-neutral character, candidate for glue |
| `CONTROL` | Bidi formatting character (§5.4) |
| `HARD_BREAK` | Paragraph/line boundary (§5.5) |

*(These are the v1.4 classes with `LATIN` renamed to `TARGET` and `BASE` renamed to `OTHER`. Under `target_script = Latin` the two classifications are identical by construction.)*

### 5.1 Base rule

Let `sc` be the Unicode `Script` property of the cluster's classification code point (§4.1).

- `sc = target_script` → `TARGET`
- `sc = Common` or `sc = Inherited` → `NEUTRAL`, subject to the overrides below
- otherwise → `OTHER`

**Examples.** With `target_script = Greek`:

```
Αθήνα   -> TARGET
Москва  -> OTHER
한국어   -> OTHER
Windows -> OTHER
```

With `target_script = Arabic`:

```
النص      -> TARGET
Windows   -> OTHER
עברית     -> OTHER
```

### 5.2 Overrides (applied in order; first match wins)

**(a) Hard breaks.** U+000A LINE FEED, U+000D CARRIAGE RETURN, U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR → `HARD_BREAK`. Implementations MAY additionally treat U+000B, U+000C, and U+0085 as hard breaks.

**(b) Inherited attachment.** By §4.1, the classification code point can have script `Inherited` only when **every** code point in the cluster is `Inherited` (degenerate input: a cluster of bare combining marks). Such a cluster takes the class of the nearest preceding cluster whose classification code point is not `Inherited`; at string start, `NEUTRAL`. (In well-formed input, marks are inside their base's grapheme cluster and this rule is moot.)

**(c) Bidi controls.** The characters in §5.4 → `CONTROL`, even though their script is `Common`.

**(d) Script-bound digits.** Digits whose script or `Script_Extensions` bind them to a specific script are **strong**, not neutral. Digits affiliated with a non-target script remain strong and classify as `OTHER`:

| Range | Name | Class |
|---|---|---|
| U+0660–U+0669 | ARABIC-INDIC DIGITS | strong: `TARGET` iff their script is the configured target, else `OTHER` |
| U+06F0–U+06F9 | EXTENDED ARABIC-INDIC DIGITS | strong: `TARGET` iff their script is the configured target, else `OTHER` |
| U+0030–U+0039 | DIGITS ZERO–NINE (European/ASCII) | `NEUTRAL` |

The general rule: any digit block other than ASCII `0-9` whose `Script` or `Script_Extensions` names a single specific script MUST classify as strong — `TARGET` when that script equals `target_script` (by the base rule of §5.1), and `OTHER` otherwise. An Arabic-Indic year in a copyright line belongs to the surrounding Arabic text: with `target_script = Latin` or `target_script = Greek`, Arabic-Indic digits classify `OTHER` and MUST NOT be absorbable glue.

**No digit ever automatically becomes `TARGET` merely because its `Script_Extensions` includes the target script.** The strengthening rule of this subsection promotes script-bound digits out of `NEUTRAL`; promotion to `TARGET` occurs only through the base rule of §5.1 on the `Script` property itself. The existing ASCII-digit neutral policy is unchanged: ASCII digits are always `NEUTRAL` with `DIGIT` affinity (§7.3), regardless of target script.

**(e) Script-affiliated punctuation and full-width forms.** Classification of characters whose usage is bound to a host script is governed by the `cjk_punct_strong` knob (§9):

- When `cjk_punct_strong = on` (the default), the following code points MUST classify as `OTHER`, not `NEUTRAL`: U+3001 (、), U+3002 (。), U+FF0C (，), U+FF0E (．), corner brackets U+300C–U+300F, and the full-width forms block U+FF01–U+FF60 excluding the full-width Latin letters U+FF21–U+FF3A and U+FF41–U+FF5A (which are `Script = Latin` and classify by the base rule regardless — `TARGET` when `target_script = Latin`, else `OTHER`).
- **The block coverage is deliberately broader than punctuation.** U+FF01–U+FF60 also contains the full-width ASCII digits U+FF10–U+FF19, currency signs, and mathematical symbols, and these are **intentionally included** in the strengthening: a full-width character is a typographic signal of CJK context, and a full-width digit or symbol in mixed-script text belongs to the host text for the same reason a script-bound digit does (§5.2d). In particular, with `target_script = Latin`, `한국어 Windows １１` yields `Windows` — full-width digits are never trailing-capturable the way ASCII digits are. (Note also that `DIGIT` affinity in §7.3 is ASCII-only, so even with the knob off, full-width digits classify `NEUTRAL` with default `SEP` affinity and are still not captured as trailing digit groups; case 28 pins this. The knob *is* observable for full-width characters in sandwich position: case 29.)
- When `cjk_punct_strong = off`, these code points MUST instead classify according to the remaining rules of this section and §5.1 — normally `NEUTRAL`, and thereafter subject to the ordinary affinity derivation of §7.3 (most fall to the default `SEP`).
- Implementations MAY additionally generalize via `Script_Extensions` (a `Common` character whose extensions exclusively name non-target scripts → `OTHER`). Such generalization applies only when the knob is on, and any generalization beyond the explicit list above MUST be documented; it is otherwise implementation-defined. Consistent with §5.2(d), this generalization MUST NOT promote any character to `TARGET`.

Rationale: full-width punctuation is the single most common false bridge between CJK text and an embedded non-CJK phrase; classifying it as strong eliminates that family of errors at zero cost for non-CJK targets. Conformance case 24 is the discriminating test for punctuation (`Alpha。Beta` splits into two runs when the knob is on, and sandwich-merges into one when it is off); case 29 is the discriminating test for full-width digits.

*Informative note — CJK target scripts.* This knob's default was designed for the common configuration in which the target script is not a CJK script and CJK material is host text. When `target_script` is itself `Han`, `Hiragana`, `Katakana`, or `Hangul`, the on-default classifies the host script's own punctuation as `OTHER` relative to it, splitting runs at 、 and 。. Corpora with a CJK target script SHOULD set `cjk_punct_strong = off` and/or use `affinity_overrides` (§9) as appropriate, and MUST document the setting as usual.

### 5.3 Target-script detection

`TARGET` covers **all** letters of the configured script — i.e., every character with `Script = target_script`, not merely a familiar core subset. For `target_script = Latin` this means ASCII `A–Z a–z`, Latin-1 letters, Latin Extended blocks, IPA extensions, etc.; for `target_script = Greek` it includes polytonic and archaic letters; for `target_script = Arabic` it includes the presentation forms and supplements; and so on for every script.

### 5.4 Bidi control set

```
U+200E LEFT-TO-RIGHT MARK          U+200F RIGHT-TO-LEFT MARK
U+061C ARABIC LETTER MARK
U+202A LRE   U+202B RLE   U+202D LRO   U+202E RLO   U+202C PDF
U+2066 LRI   U+2067 RLI   U+2068 FSI   U+2069 PDI
```

These are invisible formatting characters. They are handled by dedicated rules in §8 and MUST NOT be treated as ordinary neutral glue.

### 5.5 Hard breaks

`HARD_BREAK` runs are **walls**: no neutral resolution, absorption, or merging may cross one (§7.6). This is the primary guard against merging logically distinct blocks (e.g., a copyright footer line into the paragraph above it).

---

## 6. Phase 2 — Run Coalescing

Collapse the classified cluster stream into a list of runs, each `(class, start, end)`, by merging adjacent clusters of identical class. The result is an alternating structure such as:

```
[OTHER][NEUTRAL][TARGET][NEUTRAL][TARGET][NEUTRAL][OTHER][HARD_BREAK][NEUTRAL][TARGET]...
```

`CONTROL` clusters form their own runs at this stage (they are resolved in §8, before neutral resolution).

---

## 7. Phase 3 — Neutral Resolution

Each `NEUTRAL` run is examined with respect to its effective left neighbor `L` and right neighbor `R`, each drawn from `{TARGET, OTHER, EDGE}` — where `EDGE` denotes string start/end or an adjacent `HARD_BREAK`.

*(The algorithm of this section is unchanged from v1.4 except that references are renamed: `LATIN` → `TARGET`, `BASE` → `OTHER`.)*

### 7.1 Sandwich rule (merge)

**`L = TARGET` and `R = TARGET` → the entire neutral run merges**, joining the two target-script runs into one. This resolves internal glue: spaces, digits, parentheses, slashes, dots, hyphens inside `Windows 11 (23H2)` or a URL all fold in unconditionally. The rule is script-independent: with `target_script = Greek`, `Αθήνα 2026 (v2)` becomes one Greek run by the same mechanics.

*Bridge guard (policy knob `max_bridge`, §9):* when `max_bridge` is finite, a neutral run whose length **in grapheme clusters** exceeds `max_bridge` MUST NOT merge; its decision is `DISCARD`, leaving the two flanking target-script runs separate (the directional scans of §7.4/§7.5 do not apply, since both neighbors are `TARGET`). The default is ∞ (no limit); the hard-break wall (§5.5) provides the structural guard. An implementation MAY additionally refuse to bridge on other documented criteria (e.g., a sentence-terminal character followed by wide whitespace), but any such extension deviates from the reference behavior and MUST be documented as a non-default configuration.

### 7.2 Isolation rule (discard)

**`L ∈ {OTHER, EDGE}` and `R ∈ {OTHER, EDGE}` → the neutral run is discarded.** It has no target-script host on either side.

### 7.3 Binding affinity

At mixed boundaries, individual neutral **grapheme clusters** carry a **binding affinity**. `affinity(cluster)` MUST be a **total function** over `NEUTRAL` clusters: every neutral cluster deterministically maps to exactly one of five values. Affinity is a property of the neutral cluster alone and is **independent of the configured target script**.

| Affinity | Behavior in scans | Members |
|---|---|---|
| `RIGHT` | anchor toward following text | `©` U+00A9, `#`, `@`, `№`, `¿`, `¡`; opening brackets/quotes (`Ps`, `Pi`); currency signs (`Sc`) |
| `LEFT` | anchor toward preceding text | `™` U+2122, `℠`, `%`, `‰`, `°`, `+`, `®`*; closing brackets/quotes (`Pe`, `Pf`); sentence/phrase terminals `. , ; : ! ?` |
| `SEP` | traversable; absorbed only transitively | whitespace (`Zs`, tab), and **all neutral characters not matched by any other rule** (includes `— – · • | / \ = _ & ~ ^ * ' " -` and other `Po`/`Sm`/`Sk`/`Pd`/`Pc` characters) |
| `DIGIT` | directional (§7.4, §7.5) | ASCII digits U+0030–U+0039 **only** — full-width digits U+FF10–U+FF19 never receive `DIGIT` affinity (§5.2e; they are `OTHER` under the default knob, or default-`SEP` neutrals otherwise) |
| `STOP` | halts the scan; never absorbed | `Extended_Pictographic` characters (emoji), regional indicators, bidi controls if not stripped (§8) |

`*` `®` legitimately appears both as prefix and suffix; default is `LEFT` (suffix usage dominates), overridable per corpus (§9).

**Normative derivation order** (first match wins). Affinity is defined over **grapheme clusters**, not raw code points — after §4.1, the scans of §7.4/§7.5 iterate clusters. A cluster's affinity is derived from its **classification code point** (§4.1), with the single exception of the `STOP` test in step 3, which examines the entire cluster:

1. `affinity_overrides` from policy (§9), keyed by classification code point.
2. The explicit code-point lists above (`©`, `™`, terminals, etc.), tested against the classification code point.
3. **Cluster-wide `STOP` test:** the cluster is `STOP` if **any** of its code points has `Extended_Pictographic = true` or `Regional_Indicator = true`, or if the cluster contains U+FE0F VARIATION SELECTOR-16 or U+20E3 COMBINING ENCLOSING KEYCAP. This test MUST examine every code point in the cluster: an emoji keycap sequence such as `1️⃣` (U+0031 U+FE0F U+20E3) has classification code point `1`, and without the cluster-wide test it would incorrectly receive `DIGIT` affinity and self-commit as a trailing version number.
4. General-category rules on the classification code point: `Ps`/`Pi` → `RIGHT`; `Pe`/`Pf` → `LEFT`; `Sc` → `RIGHT`; `Zs` or `Pattern_White_Space` (excluding hard breaks) → `SEP`; ASCII `Nd` → `DIGIT`.
5. **All remaining neutral clusters → `SEP`.**

Rule 5 makes the function total: every neutral cluster deterministically receives an affinity. The choice of `SEP` (traversable) rather than `STOP` (blocking) as the terminal default is deliberate: `SEP` clusters are never absorbed on their own strength — only transitively, when a genuine anchor lies beyond them — so the permissive default cannot by itself cause over-capture, while it is what allows connector-rich entities (`b=1`, `file-123`, `a_b`, `file~backup`) to survive at run boundaries. Implementations preferring a conservative profile MAY move specific characters to `STOP` via overrides, and MUST document any such move.

`SEP` clusters never bind on their own strength. `DIGIT` behaves **asymmetrically by scan direction** — provisional in leading scans, self-committing (with one exception) in trailing scans — as specified in §7.4 and §7.5; the rationale is given in §7.5a.

The affinity table is the primary house-style customization point (§9).

### 7.4 Leading-glue rule (`L ∈ {OTHER, EDGE}`, `R = TARGET`)

Scan the neutral run **right-to-left**, starting at the edge adjacent to the target-script run. Maintain a *committed* boundary (initially: the target-script edge) and a *provisional* set (initially empty).

For each cluster encountered:

1. **`RIGHT` affinity** → absorb it **and commit** the entire provisional set. Continue scanning (further `RIGHT` anchors, separators, and digits may extend the capture, e.g., `#1 © 2026 Example`).
2. **`SEP` or `DIGIT`** → add to the provisional set; continue scanning. (With `numerals_bind_to_latin = on`, `DIGIT` instead commits like a `RIGHT` anchor; see §9 and §11.2.)
3. **`LEFT` affinity** → stop. It belongs to the preceding text (e.g., the `.` ending the preceding sentence). Provisional clusters not yet committed are released.
4. **`STOP`** → stop; release uncommitted provisionals.
5. Run exhausted → stop; release uncommitted provisionals.

The target-script run's start extends to the last committed position. Everything released stays outside the run and — because `L` is not `TARGET` — is discarded.

**Worked example** (`target_script = Latin`). Input `…펜실베이니아. © 2026 Watch Tower…` — scanning back from `Watch`: space → provisional; `6 2 0 2` → provisional; space → provisional; `©` → `RIGHT` anchor, **commit all**; continue; `.` → `LEFT`, stop. Extracted run begins at `©`. The Korean sentence's period is untouched. The identical mechanics with `target_script = Arabic` on `עברית © 2026 العربية סוף` commit the `©` and year to the Arabic run: output `© 2026 العربية`. (Note that the trailing host word must be non-Arabic for this output: if it is Arabic, as in `עברית © 2026 العربية نهاية`, both Arabic words are `TARGET` and the space between them sandwich-merges per §7.1, yielding `© 2026 العربية نهاية` — case G5.)

**Counter-example (no anchor).** Input `…텍스트 2026 Watch Tower…` — space and `2026` go provisional; next character is Hangul (run exhausted); nothing committed. Extracted run begins at `Watch`; the bare `2026` is correctly excluded. (Knob `numerals_bind_to_latin`, §9, can change this default.)

### 7.5 Trailing-glue rule (`L = TARGET`, `R ∈ {OTHER, EDGE}`)

Scan **left-to-right** from the target-script edge:

1. **`LEFT` affinity** → absorb and commit all provisionals. If a digit group is open when the anchor is reached, the anchor **consumes** it: the group and everything between it and the anchor are committed by the anchor itself (which commits through its own position), and the group's open state MUST NOT survive past the anchor. The group does not separately self-commit under rule 2 — a stale group state that later triggered rule 2 would move the committed boundary *backward* from the anchor to the last digit, dropping the anchor. (See the normative pseudocode in §11.2; case 17 with `strip_terminal_punct = off` is the regression test.)
2. **`DIGIT`** → provisional while the digit group is open; on reaching the end of a **maximal digit group** (the next cluster is not `DIGIT`, or the run is exhausted), the group **commits itself and all provisionals between it and the committed boundary** — *unless* the **abutment exception** applies:
   > **Abutment exception.** A digit group whose final cluster is the final cluster of the neutral run, when `R = OTHER`, does **not** self-commit; it remains provisional. Such a group is contiguous with the following strong other-script text and is presumed to belong to it (e.g., Korean counter constructions: in `Apple 5개`, the `5` binds to `개`; in Arabic, a digit group abutting a following Arabic word behaves likewise). A digit group abutting `EDGE` (string end or hard break) **does** self-commit.
   >
   > *The exception is intentionally limited to direct contiguity.* A separated form such as `Apple 5 개` **is** captured (`Apple 5`), because the digit group closes at the space and self-commits before the scan reaches the `OTHER` neighbor. This is not an oversight: after coalescing, `Apple 5 개` and case 14's `⁨Windows 11⁩ نص` (post-strip) present the **identical** class structure `TARGET [SEP DIGIT⁺ SEP] OTHER` — no rule operating on this algorithm's information can distinguish them, so widening the exception to spaced digits would necessarily break case 14. The narrow rule aligns with standard Korean orthography, in which counters attach directly to their numeral (`5개`); spaced counter forms are nonstandard and inherently ambiguous with the version-number pattern, and this specification resolves that ambiguity in favor of capture. Corpora where spaced host-language numerals dominate should set `trailing_digits_bind = off`. Cases 19 and 22 pin both sides of this line.
3. **`SEP`** → provisional.
4. **`RIGHT` affinity** → stop (an opener facing other-script text belongs to what follows, or dangles; either way it is not trailing glue). Release uncommitted provisionals.
5. **`STOP`** → stop; release uncommitted provisionals.
6. Run exhausted → stop; release uncommitted provisionals.

**Examples** (`target_script = Latin`).
- `macOS™의` → `™` is `LEFT`; absorbed; boundary breaks before the Hangul.
- `한국어 Windows 11` (string end) → trailing run `␣11`: space provisional, digit group `11` closes at `EDGE` → self-commits with the space → `Windows 11`.
- `…⁨Windows 11⁩ نص` (after control stripping, trailing run `␣11␣`) → digit group closes at the following space, not at run end → self-commits → `Windows 11`.
- `주소는 …a?b=1 입니다` → trailing run `=1␣`: `=` is `SEP` (rule 5 of §7.3) → provisional; digit group `1` closes at the space → commits itself **and** the `=` → `…a?b=1`.
- `사과 Apple 5개 주문` → trailing run `␣5`: digit group's final character is run-final and `R = OTHER` → abutment exception → provisional, released → `Apple`.
- `use Windows 11, 그리고` → space provisional; `11` opens a digit group; `,` is `LEFT` → commits everything through itself, consuming the open group (rule 1); final space released → `use Windows 11,`; Phase-4 `strip_terminal_punct` then removes the comma.

With another target script the same rules apply verbatim; e.g., with `target_script = Greek`, `한국어 Αθήνα 2026` (string end) captures `Αθήνα 2026` by the digit-group self-commit exactly as case 18 does for Latin.

**Both-sides case.** When a neutral run has `OTHER`/`EDGE` on both sides it is discarded whole (§7.2); §7.4/§7.5 never apply. When `L = TARGET` and `R = TARGET` the sandwich rule (§7.1) preempts both scans.

### 7.5a Rationale: directional digit asymmetry (informative)

A single symmetric `DIGIT` behavior cannot satisfy the intent of this specification: leading bare numerals (`2026 Windows`) must be excluded absent an anchor, while trailing numerals (`Windows 11`) must be captured. The asymmetry is linguistic, not arbitrary: in Latin-script naming conventions, numerals **following** a head word are overwhelmingly attributive — version numbers, model numbers, standards, quantities (`Windows 11`, `iPhone 15`, `USB 3`, `HTTP 2`) — whereas numerals **preceding** a word in mixed-script running text are typically independent host-sentence material (years, counts) unless a symbol such as `©` explicitly binds them forward. Hence: trailing digit groups self-commit (with the abutment exception guarding constructions like Korean counters); leading digit groups require a `RIGHT` anchor.

This rationale was formulated for Latin entities, but the trailing-attributive pattern generalizes well to product names, standards, and versions embedded in any script. The `trailing_digits_bind` knob (§9) disables self-commit for corpora — of any target script — where trailing numerals are predominantly host-language material. Per §2.1, the default behavior is identical for every target script; only the knob, not the script, changes it.

### 7.6 Hard-break walls

`EDGE` arising from a `HARD_BREAK` behaves exactly like `EDGE` at string boundaries: directional scans operate normally on the neutral run, but no rule may reach across the break. Thus a line beginning `© 2026 Example Corp` still captures its leading glue (scan finds the `©` anchor before hitting the wall), while a target-script run at the end of one line never merges with one at the start of the next.

---

## 8. Bidi Control Handling

Bidi controls (§5.4) are resolved **before** Phase 3, so that neutral-resolution scans never see them.

### 8.1 Default: strip

When `bidi_controls = strip`, implementations MUST delete `CONTROL` clusters from consideration entirely: remove the control runs and re-coalesce adjacent runs of equal class. Matching, indexing, translation-memory, and other non-display applications SHOULD select this policy. An extracted `Windows 11` with a stray FSI at its front and no matching PDI is a malformed fragment; stripping prevents this class of defect.

### 8.2 Alternative: pair-aware structural handling (policy knob `bidi_controls = preserve_pairs`)

Where extracted runs must round-trip for display:

- An isolate pair `FSI/LRI/RLI … PDI` (or embedding pair `LRE/RLE/LRO/RLO … PDF`) whose **entire contents** lie within a single resolved target-script run MAY be preserved inside the run, treated as a unit.
- A pair that would **straddle** a run boundary MUST NOT be split; the implementation MUST either exclude the pair entirely or extend the run to cover it — it MUST NOT emit a run containing an unmatched initiator or terminator.
- **Unpaired** initiators/terminators at a boundary MUST be shed, never absorbed.

### 8.3 Practical note

In corpora produced by converters that insert directional isolates at RTL/LTR script boundaries, controls will sit **precisely at the segmentation boundaries this algorithm computes**. This is the common case, not a corner case; the strip-then-re-coalesce order of §8.1 handles it cleanly (e.g., `عربي⁦ FSI Windows 11 PDI ⁩عربي` with `target_script = Latin` reduces to `OTHER NEUTRAL TARGET NEUTRAL OTHER` after stripping, and resolves normally).

### 8.4 Display consequence (informative)

An extracted run loses the ambient paragraph direction of its source. Standalone rendering typically applies first-strong or LTR-default direction detection — usually correct for a run consisting of a single strong script, whether LTR (Latin, Greek, Cyrillic) or RTL (Arabic, Hebrew), but the *ambient* context is still lost: an RTL target-script run extracted from an LTR host paragraph (or vice versa) may render with different neutral placement than in situ. If a consumer re-displays runs inside directional context and fidelity matters, wrap output in FSI…PDI or record source paragraph direction as run metadata. For non-display uses, ignore this section.

---

## 9. Policy Knobs and Configuration

| Knob | Default | Effect |
|---|---|---|
| `target_script` | **required, no default** | The Unicode `Script` value to extract (§5.1). MUST be an explicit configuration value; MUST be a valid `Script` value for the documented UCD version (§2.2). `Latin` reproduces v1.4 behavior exactly (§2.1) |
| `strip_terminal_punct` | **on** | Phase-4 removal of trailing `. , ; : ! ?` captured by §7.5 |
| `numerals_bind_to_latin` | **off** | If on, **leading** digit groups bind toward adjacent target-script text without requiring a `RIGHT` anchor (captures bare `2026 Windows`). *The knob name is retained from v1.4 for configuration compatibility; it applies to the configured target script, whatever it is* |
| `trailing_digits_bind` | **on** | Trailing digit groups self-commit per §7.5 rule 2. If off, trailing digits are purely provisional (symmetric with leading behavior); test cases 2, 14, 18, 20, and 22 then change. (Case 17 is unaffected: its `LEFT` comma anchor commits the digits regardless — see §7.5 rule 1.) |
| `max_bridge` | ∞ | Maximum neutral-run length, in grapheme clusters, for the sandwich rule; a longer run between two target-script runs is discarded (§7.1). Encoded as `null` in the companion fixture's `default_policy` (`null` = ∞, no limit) |
| `bidi_controls` | `strip` | `strip` (§8.1) or `preserve_pairs` (§8.2) |
| `min_target_letters` | 1 | Acceptance threshold (§10.2). *Renamed from v1.4's `min_latin_letters`; implementations MAY accept the old name as an alias when `target_script = Latin`, and MUST document if they do* |
| `affinity_overrides` | ∅ | Per-corpus additions/moves in the §7.3 table (e.g., `®` → `RIGHT`) |
| `cjk_punct_strong` | **on** | **CJK/full-width character strengthening** (§5.2e): **on** ⇒ the listed CJK punctuation and the full-width forms block U+FF01–U+FF60 (including full-width digits and symbols, excluding full-width Latin letters) MUST classify `OTHER`; **off** ⇒ they classify per the remaining rules (normally `NEUTRAL`, default affinity `SEP`). See the informative note in §5.2(e) regarding CJK target scripts |

### 9.1 Rationale for `strip_terminal_punct = on`

Sentence terminals following a target-script run usually punctuate the **surrounding** sentence, not the embedded entity (`…use Windows 11, 그리고…` — the comma is the host sentence's). Terminals *inside* an entity (`e.g.`, `Node.js`) are protected by the sandwich rule and are never trailing. Corpora rich in entities that legitimately end in periods should switch the knob off and handle terminals in the affinity table instead.

### 9.2 Configuration examples

```json
{
  "target_script": "Greek",
  "min_target_letters": 1
}
```

```json
{
  "target_script": "Arabic",
  "strip_terminal_punct": true,
  "trailing_digits_bind": true
}
```

```json
{
  "target_script": "Latin"
}
```

The last configuration, with all other knobs at their defaults, is the v1.4-equivalent configuration (§2.1).

---

## 10. Phase 4 — Trim, Validate, Emit

### 10.1 Trim

For each resolved target-script run: remove pure-whitespace clusters from both edges (non-space glue such as `©`, `™`, brackets is retained).

If `strip_terminal_punct` is on, additionally strip terminal punctuation **iteratively from the run end**, as follows. The *terminal set* is exactly `. , ; : ! ?` (U+002E, U+002C, U+003B, U+003A, U+0021, U+003F). While the run's final grapheme cluster is a member of the terminal set: remove it, then remove any pure-whitespace clusters newly exposed at the run end, and repeat. **No other cluster is ever removed by this phase**; in particular, closing brackets, quotes, and symbols are never removed.

This mechanical rule replaces the informal "protected by a matching structure" language of earlier drafts and is the normative definition of protection: a terminal is protected precisely when it is not run-final at any step of the iteration — i.e., when at least one retained non-terminal cluster (typically a closing bracket) follows it. No bracket-matching computation is required, because the affinity scans (§7.5) determine which closers are retained in the first place. Consequences:

- `(Example.)` → the period is followed by the retained `)`; the final cluster is never a terminal; nothing is stripped → `(Example.)`.
- `Example.)` (closer with no matching opener inside the run) → the final cluster is `)`, not a terminal; iteration halts immediately and the period is retained → `Example.)`. This resolution of the malformed-nesting case — retention, not stripping — is deliberate and normative.
- `use Windows 11,` → the final cluster is `,` → stripped → `use Windows 11`.

Conformance cases 17, 26, and 27 pin this behavior.

### 10.2 Validate

A run MUST contain at least `min_target_letters` characters of general category `L*` whose `Script` property equals `target_script`. Runs failing the threshold are dropped — this removes any pathological glue-without-host survivor.

Examples with `target_script = Greek` and the default threshold of 1: `Αθήνα` is valid; `© 2026` is invalid (no Greek letters); `123 ABC` is invalid (its letters are Latin, not Greek).

### 10.3 Emit

Output runs in logical order with offsets per §4.2. Output remains an ordered list of extracted runs, but the runs are target-script runs rather than specifically Latin runs.

**Examples.**

Input `한국어 Windows 11 텍스트` with `target_script = Latin` → output `Windows 11`.

Input `한국어 Αθήνα 2026 텍스트` with `target_script = Greek` → output `Αθήνα 2026`.

Input `עברית © 2026 العربية סוף` with `target_script = Arabic` → output `© 2026 العربية` — the leading-anchor rules are independent of script identity (§7.4). Contrast `עברית © 2026 العربية نهاية`, whose final word is itself Arabic: the sandwich rule merges across the intervening space and the output is `© 2026 العربية نهاية` (case G5).

---

## 11. Algorithm Summary (normative pseudocode)

### 11.1 Processing order and mutation semantics (normative)

Phase 3 is a **pure decision pass followed by a materialization pass**. All neutral-run decisions MUST be computed against the immutable Phase-2 run list (after §8 control resolution); no decision may observe the effect of another decision. Decisions are then applied in a single subsequent pass, after which adjacent `TARGET` runs are coalesced once.

This is well-defined and order-independent by construction: a neutral run's decision depends only on the **classes** of its neighboring runs, neighboring runs of a `NEUTRAL` run are always strong runs or edges (Phase 2 guarantees alternation), and no Phase-3 operation changes the class of a strong run. Implementations MAY use any evaluation order, or in-place left-to-right mutation, **provided** the results are identical to decide-then-materialize; the two-pass formulation is the reference semantics.

### 11.2 Pseudocode

```text
function extract_target_runs(text, policy):
    # policy.target_script is required (§9); classify() applies §5.1
    # against it: sc == target_script ⇒ TARGET; Common/Inherited ⇒
    # NEUTRAL (subject to §5.2); otherwise ⇒ OTHER.
    clusters   = grapheme_clusters(text)                          # UAX #29
    classified = [ (c, classify(c, policy)) for c in clusters ]   # §5, §4.1
    runs       = coalesce(classified)                             # §6
    runs       = resolve_bidi_controls(runs, policy)              # §8 (strip → re-coalesce)

    # ---- Decision pass (no mutation) ----
    decisions = []
    for each run r of class NEUTRAL in runs:
        L = effective_left_neighbor(r)    # TARGET | OTHER | EDGE  (HARD_BREAK ⇒ EDGE)
        R = effective_right_neighbor(r)
        if L == TARGET and R == TARGET:                                       # §7.1
            if policy.max_bridge is null or cluster_length(r) <= policy.max_bridge:  decisions.add(MERGE(r))
            else:                                       decisions.add(DISCARD(r))  # bridge guard
        elif L != TARGET and R != TARGET:  decisions.add(DISCARD(r))          # §7.2
        elif R == TARGET:                  decisions.add(split_leading(r, policy))     # §7.4
        else:                              decisions.add(split_trailing(r, R, policy)) # §7.5

    # ---- Materialization pass ----
    apply(decisions, runs)                # extend/merge/discard boundaries
    coalesce_adjacent_target(runs)

    result = []
    for each run r of class TARGET in runs:
        trim(r, policy)                                     # §10.1
        if validate(r, policy): result.append(offsets(r))   # §10.2 (min_target_letters)
    return result

function split_leading(r, policy):         # scan right→left from target edge, by cluster
    committed = target_edge; provisional = []
    for cl in reverse_clusters(r):
        a = affinity(cl)                                    # total, §7.3
        if a == RIGHT:            committed = pos(cl); provisional = []  # anchor commits
        elif a == DIGIT and policy.numerals_bind_to_latin:
                                  committed = pos(cl); provisional = []  # digit group + intervening
                                                                         # SEPs commit without anchor
        elif a in {SEP, DIGIT}:   provisional.push(cl)      # default: leading digits need an anchor
        else:                     break                     # LEFT or STOP ⇒ stop
    return EXTEND_START_TO(committed)     # released provisionals stay outside

function split_trailing(r, R, policy):     # scan left→right from target edge, by cluster
    committed = target_edge; provisional = []; group_open = false
    for cl in forward_clusters(r):
        a = affinity(cl)
        if a == LEFT:             committed = pos(cl); provisional = []
                                  group_open = false   # anchor commits through itself,
                                                       # CONSUMING any open digit group
                                                       # (§7.5 rule 1). Stale group state
                                                       # would let a later close_group()
                                                       # move `committed` backward from
                                                       # the anchor to the digit group.
        elif a == DIGIT:          provisional.push(cl); group_open = true
        elif a == SEP:            close_group(); provisional.push(cl)
        else:                     close_group(); break      # RIGHT or STOP ⇒ stop
    close_group(at_run_end = true)
    return EXTEND_END_TO(committed)

    where close_group(at_run_end = false):
        if group_open:
            # §7.5 rule 2: a closed digit group self-commits when the knob is on,
            # unless it abuts following OTHER text (abutment exception)
            if policy.trailing_digits_bind:
                unless (at_run_end and R == OTHER):
                    committed = end_of_group; provisional = []
            group_open = false
```

Note that with `numerals_bind_to_latin = on`, each digit cluster in the leading scan commits like a `RIGHT` anchor, which by the standard commit mechanics also captures any provisional separators between the digit group and the run boundary — so `텍스트 2026 Windows` (with `target_script = Latin`) yields `2026 Windows` (group plus intervening space), while a lone `SEP` beyond the group is still released unless a further anchor appears. With `trailing_digits_bind = off`, trailing digit groups are purely provisional, restoring symmetric leading/trailing digit behavior; conformance cases 2, 14, 18, 20, and 22 assume the default (**on**) and change when it is off. Case 17 passes under either setting, because its trailing comma is a `LEFT` anchor that commits the digit group independently of the knob — but note that this invariant holds only because the `LEFT` branch above clears `group_open`. Without that clearing, the subsequent `SEP` (the space after the comma) would call `close_group()` and move `committed` backward from the comma to the final digit, which is observable when `strip_terminal_punct = off` (expected `use Windows 11,`, buggy output `use Windows 11`). Case 17's `strip_terminal_punct` sensitivity variant is the regression test for this.

Complexity: O(n) in clusters; each cluster is classified once and visited at most twice (coalescing plus at most one boundary scan). The complexity is independent of the configured target script.

---

## 12. Conformance Test Cases

### 12.1 Latin backward-compatibility suite (`target_script = Latin`)

All 29 cases below are the v1.4 conformance suite, unchanged, and are normative under the configuration `target_script = Latin` with all other knobs at defaults. Per §2.1, a conforming v2 implementation MUST pass every case (at its declared tier) exactly as a conforming v1.4 implementation would.

Notation: input → expected extracted runs (as substrings). Host script shown as Korean (`한`) or Arabic (`ع`); results identical for any `OTHER` script. Tier: **C** = Core, **F** = Full.

**Test inputs and expected outputs are normative as literal UTF-8 plain text.** Rendered, exported, or converted copies of this document are not authoritative for the conformance suite: document processors commonly transform content (auto-linking URLs into `<a>` markup, smart-quote substitution, dash conversion), and any such transformation of a test string invalidates that copy of the suite. Implementers MUST take test data from the plain-text source of this specification or from the machine-readable companion file `script-run-extraction-tests.json` (spec_version 2.0), whose `cases` array reproduces the v1.4 fixture `latin-run-extraction-tests.json` unchanged — all 29 cases with default-policy expectations, selected per-knob sensitivity variants, and explicit code-point listings for cases containing invisible characters — and whose `default_policy` sets `target_script: "Latin"` for this suite. The v1.4 fixture remains a valid source for these 29 cases; it predates the `target_script` parameter, and harnesses MUST run it under `target_script = Latin` (a fixture `default_policy` lacking a `target_script` key means `Latin`). Where the table below and the companion file disagree, the companion file governs. Implementers SHOULD verify code points, not glyphs, when a case fails.

**JSON re-escaping hazard.** The authoritativeness of the companion file applies to its **original bytes**, parsed once by a conforming JSON parser. Copies of the fixture that have passed through an additional encoding layer — embedded in another JSON document, pasted into a code block, logged, or diffed by tools that escape backslashes — will display `\n` as `\\n` and similar, and inspecting such a copy will produce false conclusions about the fixture's content (this occurred in review: the single-escape `\n` of case 8, bytes `5C 6E`, parsing to U+000A, was misread as a double escape from a re-encoded copy). Auditors MUST verify escaping questions against the raw bytes of the original file or against the parsed string's code points, and SHOULD use the `input_codepoints` redundancy lists — which exist precisely to make such corruption and misreading mechanically detectable.

**Policy-sensitivity variants are normative but NOT exhaustive.** The default-policy expectations are complete for all 29 cases; the `policy_sensitivity` entries are selected normative expectations for particular non-default settings. The absence of an entry for a given knob does **not** imply the case is unaffected by that knob (broad knobs such as `min_target_letters`, `max_bridge`, and `affinity_overrides` inherently affect many cases). Test harnesses MUST NOT infer invariance from an absent entry; the companion file carries `"policy_sensitivity_is_exhaustive": false` to the same effect.

| # | Tier | Input | Expected runs | Exercises |
|---|---|---|---|---|
| 1 | C | `한국어 Windows 11 (23H2) 텍스트` | `Windows 11 (23H2)` | sandwich: digits, space, parens |
| 2 | C | `주소는 https://example.com/a?b=1 입니다` | `https://example.com/a?b=1` | sandwich (internal URL glue) + trailing digit commit through `SEP` `=` |
| 3 | C | `macOS™의 기능` | `macOS™` | trailing anchor |
| 4 | C | `텍스트. © 2026 Watch Tower Bible and Tract Society of Pennsylvania` | `© 2026 Watch Tower Bible and Tract Society of Pennsylvania` | leading anchor commits provisionals; stops at `LEFT` `.` |
| 5 | C | `텍스트 2026 Watch Tower` | `Watch Tower` | provisionals released without anchor |
| 6 | C | `한국어, English 텍스트` | `English` | bridging comma excluded both sides |
| 7 | C | `(존 3:16) 한국어 (John 3:16) 텍스트` | `(John 3:16)`* | opener as leading anchor, closer as trailing anchor |
| 8 | C | `English one\n© 2026 Corp` (`\n` denotes a single U+000A LINE FEED; see the fixture's `input_codepoints`) | `English one`, `© 2026 Corp` | hard-break wall; leading glue after wall |
| 9 | C | `한국어 100 GB+ 저장` | `GB+`† | trailing `+` anchor; bare leading numeral released |
| 10 | C | `가나다 ( 라마바` | ∅ | dangling neutral, no host — discarded |
| 11 | F | `عام ٢٠٢٦ Windows` | `Windows` | Arabic-Indic digits are `OTHER`, not provisional glue |
| 12 | F | `عام 2026 Windows` | `Windows` | European digits provisional, no anchor, released |
| 13 | F | `النص © 2026 Example Corp نهاية` | `© 2026 Example Corp` | leading + trailing boundaries vs RTL host |
| 14 | F | `نص ⁨Windows 11⁩ نص` (FSI…PDI) | `Windows 11` | control strip → re-coalesce → trailing digit-group commit (group closed by following space, §7.5 rule 2) |
| 15 | F | `نص ‏Windows نص` (stray RLM) | `Windows` | unpaired control shed, never absorbed |
| 16 | F | `한국어。English 텍스트` | `English` | U+3002 boundary, basic form (output is knob-insensitive here; case 24 is the discriminating test for §5.2e) |
| 17 | C | `use Windows 11, 그리고` | `use Windows 11`‡ | trailing digits consumed by `,` `LEFT` anchor (§7.5 rule 1); `strip_terminal_punct` removes host-sentence comma; **regression test** (via ‡ variant) for anchor clearing digit-group state (§11.2) |
| 18 | C | `한국어 Windows 11` | `Windows 11` | trailing digit group abutting `EDGE` self-commits |
| 19 | C | `사과 Apple 5개 주문` | `Apple` | abutment exception: digit group contiguous with following `OTHER` released |
| 20 | C | `값은 file-123 입니다` | `file-123`¶ | totality: `-` falls to default `SEP`, committed transitively by digit group |
| 21 | F | `축하 Party 🎉 완료` | `Party` | emoji at a run boundary is never absorbed (basic form; does not by itself discriminate `STOP` from `SEP` — case 25 does) |
| 22 | C | `사과 Apple 5 개 주문` | `Apple 5`§¶ | spaced digit group closes before run end → self-commits; contrast with case 19 |
| 23 | F | `한국어 Windows 1️⃣ 텍스트` | `Windows` | cluster-wide `STOP` test (§7.3.3): keycap sequence U+0031 U+FE0F U+20E3 is `STOP`, not `DIGIT` |
| 24 | F | `한국어 Alpha。Beta 텍스트` | `Alpha`, `Beta`ǁ | U+3002 classified `OTHER` (§5.2e): prevents TARGET–NEUTRAL–TARGET sandwich merge; discriminating test for `cjk_punct_strong` |
| 25 | F | `축하 Party 🎉™ 완료` | `Party` | `Extended_Pictographic` → `STOP` blocks the scan from reaching a later `LEFT` anchor (`™`); a `SEP` misclassification would yield `Party 🎉™` |
| 26 | C | `한국어 (Example.) 텍스트` | `(Example.)` | §10.1 iterative stripping: run-final `)` is not a terminal, so the period is protected |
| 27 | C | `한국어 Example.) 텍스트` | `Example.)` | §10.1 malformed-nesting resolution: unmatched retained closer still protects the period (retention is normative) |
| 28 | F | `한국어 Windows １１` | `Windows` | full-width digits U+FF10–U+FF19: `OTHER` under §5.2(e) strengthening, and never `DIGIT` affinity (§7.3) — so, unlike ASCII digits (case 18), never captured as a trailing digit group; output identical under `cjk_punct_strong = off` |
| 29 | F | `한국어 Alpha１Beta 텍스트` | `Alpha`, `Beta`ǁ | discriminating test that full-width **digits** (not only punctuation) are in the §5.2(e) strengthening set; a `NEUTRAL` classification sandwich-merges to `Alpha１Beta` |

\* Case 7: `(존 3:16)` yields no run — its only strong content is Hangul; parens/digits have no target-script host under `target_script = Latin`.
† Case 9: the neutral run `␣100␣` lies between `OTHER` and `TARGET`, so the leading scan from `GB` holds the space and digits as provisionals; the run exhausts at Hangul with no `RIGHT` anchor, and they are released. Under defaults the run is `GB+`; capturing `100 GB+` requires `numerals_bind_to_latin = on`. This case is the canonical illustration of the bare-numeral policy knob, and implementations MUST document which setting their suite tests.
‡ With `strip_terminal_punct = off`: `use Windows 11,`. This variant is the regression test for §11.2's requirement that a `LEFT` anchor clear open digit-group state: an implementation with the stale-state bug emits `use Windows 11` (comma dropped) under this setting.
§ Case 22 vs. case 19: the abutment exception applies only to a digit group directly contiguous with following `OTHER` text. `Apple 5개` (case 19) releases the numeral; `Apple 5 개` (case 22) captures it, and necessarily so — its class structure is identical to case 14's. See the rationale note in §7.5 rule 2.
¶ Cases 20 and 22 are sensitive to `trailing_digits_bind`: with the knob off, case 20 yields `file` (the `-123` suffix stays provisional and is released) and case 22 yields `Apple`. These variants are recorded in the companion fixture.
ǁ With `cjk_punct_strong = off` the strengthened code point classifies `NEUTRAL` (default affinity `SEP`), the sandwich rule merges, and the expected output becomes a single run: case 24 → `Alpha。Beta`; case 29 → `Alpha１Beta`.

### 12.2 Target-script generalization cases

The cases below are normative for the target scripts they name (all other knobs at defaults), and pin the parameterization itself: identical structures resolve identically regardless of which script is the target. Tier assignments follow the same convention as §12.1. Test data here is normative as literal UTF-8 plain text under the same rules and hazards stated in §12.1 — with one hazard specific to this table: **cross-script confusables**. Greek `Α` U+0391, Cyrillic `А` U+0410, and Latin `A` U+0041 (and many lowercase pairs such as Cyrillic `а о с е` vs. Latin) are visually identical in most fonts, and a copy-paste substitution silently changes a case's class structure. The machine-readable companion `script-run-extraction-tests.json` encodes these cases in its `generalization_cases` array with an explicit per-case `target_script` and `input_codepoints` redundancy lists for every input containing confusable or RTL material; where this table and the file disagree, the file governs.

| # | Tier | `target_script` | Input | Expected runs | Exercises |
|---|---|---|---|---|---|
| G1 | C | `Greek` | `한국어 Αθήνα 2026 텍스트` | `Αθήνα 2026` | trailing digit-group self-commit with a non-Latin target (mirror of cases 1/18); sensitive to `trailing_digits_bind` (off: `Αθήνα`) |
| G2 | C | `Greek` | `Αθήνα` | `Αθήνα` | base rule: `sc = Greek` → `TARGET`; whole-string run; validation with `min_target_letters = 1` |
| G3 | C | `Greek` | `Москва Athens 텍스트` | ∅ | Cyrillic, Latin, and Hangul letters are all `OTHER` under a Greek target; no target-script host |
| G4 | C | `Arabic` | `עברית © 2026 العربية סוף` | `© 2026 العربية` | leading `©` anchor commits provisionals toward an RTL target; anchor rules are script-independent (mirror of case 13 with host and target roles swapped: Hebrew host, Arabic target) |
| G5 | C | `Arabic` | `עברית © 2026 العربية نهاية` | `© 2026 العربية نهاية` | sandwich rule with an RTL target: the final word is itself Arabic, so the space before it is `TARGET NEUTRAL TARGET` and merges (§7.1). *Corrects the v2.0 design-delta example, which gave `© 2026 العربية` for this input by overlooking that `نهاية` is Arabic* |
| G6 | C | `Latin` | `한국어 Windows 11 텍스트` | `Windows 11` | v1.4 equivalence spot check (§2.1) with explicit `target_script = Latin`; structural duplicate of case 1 without parens |
| G7 | F | `Greek` | `عام ٢٠٢٦ Αθήνα` | `Αθήνα` | §5.2(d) generalization: Arabic-Indic digits are `OTHER` under a Greek target, never provisional glue (mirror of case 11) |
| G8 | F | `Greek` | `© 2026 텍스트` | ∅ | no `TARGET` neighbor exists anywhere, so the neutral run is `OTHER`/`EDGE`-flanked and discarded (§7.2); nothing survives to §10.2 |
| G9 | F | `Greek` | `한국어 123 ABC 텍스트` | ∅ | Latin letters are `OTHER` under a Greek target, so no run is formed and nothing satisfies a Greek `min_target_letters` threshold (§10.2) |
| G10 | C | `Cyrillic` | `한국어 Москва 11 텍스트` | `Москва 11` | Cyrillic target: trailing digit-group self-commit (mirror of G1/case 18); sensitive to `trailing_digits_bind` (off: `Москва`) |
| G11 | C | `Hebrew` | `النص עברית™ نهاية` | `עברית™` | Hebrew target inside an Arabic host: trailing `™` `LEFT` anchor absorbed (mirror of case 3); RTL-target/RTL-host processing remains purely logical-order |

Note on G4/G5: with `target_script = Latin`, either input yields no run under defaults (the strings contain no Latin letters, so nothing passes §10.2) — the configurations partition the same text differently because the strong classes swap with the parameter. This is the intended consequence of parameterization, not a discrepancy. G4 and G5 differ from each other only in the script of the final word, which flips the right neighbor of the last neutral run between `OTHER` (boundary, scan) and `TARGET` (sandwich, merge).

---

## 13. Implementation Notes (informative)

- **Character data:** any Unicode-complete library suffices — ICU (`uscript_getScript`, `u_charType`, `ublock_getCode`), Python `unicodedata` + `regex` module script properties, Rust `unicode-script`/`unicode-segmentation`, JS `Intl.Segmenter` + `\p{Script=…}` regex properties. Grapheme segmentation MUST follow UAX #29; do not iterate raw code units. The target-script comparison of §5.1 is a single equality test against the configured `Script` value; implementations supporting multiple concurrent targets should simply instantiate the extractor once per target script.
- **Affinity table:** implement exactly the five-step derivation of §7.3 (overrides → explicit list → cluster-wide pictographic `STOP` test → general-category rules → default `SEP`). The function is total by construction and target-script-independent; there is no "unclassified" state, and independent implementations MUST agree on every cluster given the same UCD version and overrides.
- **Offsets:** compute in the encoding of the source string; when the source is UTF-8, byte offsets are RECOMMENDED for zero-copy slicing.
- **Testing:** the §12 tables are intentionally minimal; production suites should add NFC/NFD variants of cases 1–4, empty/all-neutral/all-target inputs, adjacent hard breaks, and — for each deployed target script — structural mirrors of the Core cases in that script.
- **Migration from v1.4:** a v1.4 implementation becomes a conforming v2 implementation by (a) threading `target_script` through classification (§5.1) and validation (§10.2), (b) renaming `min_latin_letters` to `min_target_letters` (optionally aliasing the old name), and (c) leaving every other code path untouched. The renames `LATIN` → `TARGET` and `BASE` → `OTHER` are purely editorial; §2.1 requires the `Latin` configuration to be behaviorally indistinguishable from v1.4, so the existing v1.4 test harness and fixture remain the regression suite.

---

## 14. Changelog

**2.0** — Parameterized target script (Option A):

1. **The special role of Latin is replaced by a configurable `target_script`.** Classification (§5.1) tests `Script(classification_code_point) == target_script`; the classes `LATIN` and `BASE` are renamed `TARGET` and `OTHER`; every occurrence of "Latin run" becomes "target-script run". All neutral-resolution behavior, bidi handling, affinity rules, digit handling, hard-break handling, and trimming semantics are unchanged.
2. **Normative backward-compatibility requirement added** (§2.1): `target_script = Latin` MUST reproduce v1.4 classifications, decisions, trimming, validation, offsets, and substrings bit-for-bit. The full 29-case v1.4 suite and its companion fixture are retained unchanged as the Latin conformance suite (§12.1).
3. **§5.2(d) generalized:** digits affiliated with a non-target script remain strong and classify `OTHER`; no digit ever automatically becomes `TARGET` merely because its `Script_Extensions` includes the target script; the ASCII-digit neutral policy is unchanged. The §5.2(e) `Script_Extensions` generalization likewise may only produce `OTHER`, never `TARGET`.
4. **Configuration** (§9): `target_script` added as a required knob with no default; `min_latin_letters` renamed `min_target_letters` (old name MAY be accepted as an alias for `target_script = Latin`); `numerals_bind_to_latin` retains its v1.4 name for configuration compatibility but is defined over the configured target script.
5. **Validation generalized** (§10.2): a run is valid only if it contains at least `min_target_letters` letters whose `Script` property equals `target_script`.
6. **New generalization test cases G1–G11** (§12.2) pin the parameterization for Greek, Cyrillic, Arabic, Hebrew, and the Latin-equivalence spot check, including the §5.2(d) digit generalization, RTL-target sandwich/boundary contrast (G4/G5), and cross-script validation failures. G5 corrects the Arabic worked example that circulated with the v2.0 design delta: input ending in `نهاية` sandwich-merges to `© 2026 العربية نهاية` because the final word is itself Arabic; the delta's stated output `© 2026 العربية` requires a non-Arabic final word (G4). The corrected example is used in §1.2, §7.4, and §10.3.
7. **Machine-readable companion fixture** `script-run-extraction-tests.json` (spec_version 2.0) added and made governing for the suite: its `cases` array reproduces the v1.4 fixture `latin-run-extraction-tests.json` unchanged (§12.1), its `default_policy` adds `target_script: "Latin"` and renames `min_latin_letters` to `min_target_letters`, and a new `generalization_cases` array encodes G1–G11 with per-case `target_script` and `input_codepoints` redundancy lists guarding cross-script confusables (Greek/Cyrillic/Latin lookalike letters) as well as invisible characters.
8. **Informative additions:** guidance for CJK target scripts under `cjk_punct_strong` (§5.2e); generalized display-direction note (§8.4); migration note for v1.4 implementations (§13).

**1.4** — Fourth review round:

1. **Case 8 escape report investigated and found to be a false positive; countermeasure adopted anyway.** Review reported the fixture's case 8 as containing a literal backslash-plus-`n` (`\\n` in JSON source) rather than U+000A. Byte-level inspection of the authoritative file shows the source is the correct single escape (`… 65 5C 6E C2 A9 …`), which parses to U+000A LINE FEED; the doubled escape existed only in a re-encoded copy inspected during review. The input is **unchanged**. Two hardening measures added: (a) case 8 now carries the `input_codepoints` redundancy list (including U+000A) so any escaping corruption or misreading is mechanically detectable; (b) §12 gains an explicit **JSON re-escaping hazard** warning requiring auditors to verify escaping questions against original bytes or parsed code points.
2. **`max_bridge: null` documented** (§9, fixture notes): in the companion fixture's `default_policy`, `null` normatively represents the specification default of ∞ (no bridge-length limit). Harnesses MUST NOT interpret it as zero, absent, or invalid.
3. **§5.2(e) scope made explicit** (retitled *Script-affiliated punctuation and full-width forms*): the U+FF01–U+FF60 strengthening deliberately covers the whole block — full-width digits U+FF10–U+FF19, currency signs, and symbols included — not only punctuation, by the same host-affiliation logic as §5.2(d). The §9 knob description is renamed accordingly (*CJK/full-width character strengthening*). §7.3 now states that `DIGIT` affinity is ASCII-only, so full-width digits are never trailing-capturable under either knob setting. New **case 28** (`Windows １１` → `Windows`, knob-insensitive) pins the default; new **case 29** (`Alpha１Beta`) is the knob-discriminating test for full-width digits specifically. Note that a trailing full-width-digit test alone cannot discriminate the knob (both settings yield `Windows`), which is why both cases exist.
4. Editorial: §7.3 opening now says `affinity(cluster)` over neutral grapheme clusters, matching the cluster-based derivation that follows.
5. §12 preamble updated: 29 cases; companion fixture updated in lockstep (`spec_version: "1.4"`).

**1.3** — Third review round:

1. **Normative pseudocode bug fixed** (§11.2, §7.5 rule 1): a `LEFT` anchor in `split_trailing` now clears `group_open`. Previously, a digit group open when the anchor was reached left stale state, and a subsequent `SEP`'s `close_group()` moved the committed boundary *backward* from the anchor to the last digit. Masked under the default policy (Phase-4 stripping removed the comma anyway), but observable in case 17 with `strip_terminal_punct = off`, whose expected output is `use Windows 11,`. Case 17's sensitivity variant is designated the regression test.
2. **Knob-sensitivity claims corrected** (§9, §11.2, §12; reverses the overstated claim in changelog 1.2 item 8): `trailing_digits_bind = off` also changes cases 20 (`file-123` → `file`) and 22 (`Apple 5` → `Apple`), not only 2, 14, and 18. The companion fixture gains the missing variants, and both the spec and fixture now state explicitly that policy-sensitivity entries are **selected, not exhaustive** (`"policy_sensitivity_is_exhaustive": false`); absence of an entry does not imply invariance.
3. **Case 16 demoted to a basic boundary case**; new discriminating **case 24** (`한국어 Alpha。Beta 텍스트` → `Alpha`, `Beta`) added. Case 16's output is identical whether U+3002 classifies strong or neutral-`SEP`, so it could not detect the misclassification it claimed to exercise; the TARGET。TARGET form can (a `NEUTRAL` misclassification sandwich-merges to `Alpha。Beta`). Case 24 also carries the `cjk_punct_strong = off` variant.
4. **Case 21 demoted likewise**; new discriminating **case 25** (`축하 Party 🎉™ 완료` → `Party`) added. In case 21 both `STOP` and `SEP` classifications of 🎉 yield `Party`; placing a `LEFT` anchor (`™`) beyond the emoji makes incorrect traversal observable (`Party 🎉™`).
5. **`cjk_punct_strong` made deterministic** (§5.2e, §9): *on* ⇒ the listed code points MUST classify strong; *off* ⇒ they classify per the remaining rules (normally `NEUTRAL`); `Script_Extensions` generalization is on-only, documented, and otherwise implementation-defined.
6. **`max_bridge` implemented in the normative pseudocode** (§7.1, §11.2): a neutral run between two target-script runs longer than `max_bridge` grapheme clusters is `DISCARD`ed, not merged. Previously the knob appeared in the policy table but had no effect in the reference algorithm (the same defect class fixed for the digit knobs in 1.2).
7. **`strip_terminal_punct` protection defined operationally** (§10.1): iterative removal of run-final terminals (`. , ; : ! ?`) only, with whitespace re-trim; no other cluster is removed, so any terminal followed by a retained cluster is protected mechanically, with no bracket-matching computation. The malformed case `Example.)` is resolved normatively in favor of retention. New **cases 26–27** pin `(Example.)` and `Example.)`.
8. §12 preamble updated: 27 cases; policy variants declared non-exhaustive; companion fixture updated in lockstep (`spec_version: "1.3"`).

**1.2** — Second review round:

1. **Both policy knobs implemented in the normative pseudocode** (§11.2): `numerals_bind_to_latin` (leading digit clusters commit like anchors, capturing intervening separators) and `trailing_digits_bind` (gates digit-group self-commit in `close_group`). In 1.1 these were documented but had no effect in the reference algorithm.
2. **Affinity defined over grapheme clusters** (§7.3), derived from the classification code point — except the `STOP` test, which is normatively **cluster-wide** (any `Extended_Pictographic` or `Regional_Indicator` code point, or U+FE0F/U+20E3 anywhere in the cluster). Without this, keycap sequences like `1️⃣` would classify as `DIGIT`. New case 23.
3. **Abutment-exception narrowness confirmed as intentional** (§7.5 rule 2): `Apple 5개` releases the numeral; `Apple 5 개` captures it. The spaced form is class-structurally identical to case 14, so no wider rule is possible; documented with the Korean-orthography rationale. New case 22 pins the contrast.
4. Conformance cross-references in §2 corrected (§10 → §12); `Pattern_White_Space` and `Regional_Indicator` added to the normative property list.
5. Remaining "base code point" wording in §5.1/§5.2(b) replaced with "classification code point"; §5.2(b) reworded to describe the all-`Inherited` case directly.
6. §12 preamble now states that **test data is normative as literal UTF-8 plain text**; rendered/exported copies of the document (which may auto-link URLs or substitute punctuation) are not authoritative for the suite. (The reviewed copy's `<a>`-markup corruption of case 2 arose in rendering; the source was and is plain text.)
7. **Machine-readable companion fixture** `latin-run-extraction-tests.json` added and made governing for the suite: all 23 cases with default-policy expectations, per-knob sensitivity variants, and explicit code-point listings for inputs containing invisible characters (bidi controls, VS16/keycap, Arabic-Indic digits).
8. Knob-sensitivity claim corrected: case 17 is **not** sensitive to `trailing_digits_bind` (its `LEFT` comma anchor commits the digit group under either setting); only cases 2, 14, and 18 change.

**1.1** — Resolves two conformance contradictions and four ambiguities identified in review:

1. **Trailing digit groups now self-commit** (§7.5 rule 2), with an *abutment exception* for digit groups contiguous with following strong host text (Korean counters, etc.). Fixes cases 2, 14, and 17, which were unreachable under the 1.0 rules; adds cases 18–19. Rationale for the leading/trailing asymmetry recorded in new §7.5a; new knob `trailing_digits_bind`.
2. **`affinity(ch)` is now total** (§7.3): five-step normative derivation ending in a default of `SEP`; new `STOP` affinity for pictographics and unstripped controls. Defines behavior of `= _ & ~ - ^` etc.; adds cases 20–21.
3. **Phase-3 processing order made normative** (§11.1): decide-then-materialize reference semantics, with the order-independence argument stated.
4. **"Classification code point" defined operationally** (§4.1), replacing the undefined "base code point."
5. **Unicode data version pinning** required (§2).
6. Case 14's "Exercises" annotation corrected (it exercises trailing resolution, not the sandwich rule).

**1.0** — Initial draft.

*(Changelog entries 1.0–1.4 are reproduced from the v1.4 document; where they name the historical classes `LATIN`/`BASE` or the knob `min_latin_letters`, read the v2.0 names `TARGET`/`OTHER`/`min_target_letters` under §2.1's equivalence. Entries 1.3.3, 1.3.5–6, and 1.1.1 above have had only those class names updated; their substance is unchanged.)*

---

*End of specification.*
