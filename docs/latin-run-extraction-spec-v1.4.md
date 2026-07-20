# Latin Run Extraction from Mixed-Script Text

**Specification, Version 1.4 (Draft)**

---

## 1. Introduction

### 1.1 Problem Statement

Given a Unicode string whose primary content is written in one or more non-Latin scripts (e.g., Korean, Arabic, Hebrew), possibly including right-to-left (RTL) scripts, extract every maximal contiguous run of embedded Latin-script text — English phrases, product names, URLs, version strings, copyright notices, and similar material.

The central difficulty is the treatment of **language-neutral characters**: digits, whitespace, punctuation, and symbols whose Unicode script property is `Common` or `Inherited`. These characters carry no script identity of their own, yet they are frequently an integral part of a Latin entity. A correct extractor must fold neutral characters into a Latin run when they logically belong to it, in all three positions:

- **Internal** — neutrals joining Latin components: `Windows 11 (23H2)`, `https://example.com/path?q=1`
- **Trailing** — neutrals appended to a Latin run: `macOS™`, `100 GB+`
- **Leading** — neutral sequences preceding a Latin run: `© 2026 Watch Tower Bible and Tract Society of Pennsylvania`

Simultaneously, the extractor must **exclude** neutral characters when they act as a bridge between Latin and non-Latin text, or when they dangle without a Latin host. A comma separating a Korean clause from an English phrase belongs to neither run's output; a bare year adjacent to Latin text with no anchoring symbol must not be captured.

Naive approaches fail:

- A regex over `[A-Za-z]` plus permissive neutral classes cannot express *conditional* absorption ("absorb this separator only if an anchoring symbol lies beyond it") without pathological lookahead, and either over-captures (dragging in sentence punctuation from the surrounding text) or under-captures (splitting `Windows 11 (23H2)` into fragments).
- Splitting on script boundaries alone discards all neutral glue, producing `Windows`, `11`, `23H2` as separate fragments and losing `©`, `™`, parentheses, and URLs' structure entirely.

### 1.2 Design Insight

The problem is structurally identical to the **neutral-type resolution phase of the Unicode Bidirectional Algorithm** (UAX #9, rules N1–N2): both must decide which strong context a run of weak/neutral characters belongs to. This specification adapts that skeleton:

1. Classify each character by script strength.
2. Coalesce into maximal runs of uniform class.
3. Resolve each neutral run by examining its strong neighbors — merging when flanked by Latin on both sides, discarding when flanked by non-Latin, and **splitting directionally** at mixed boundaries using per-cluster *binding affinity*.

All processing occurs in **logical order** (storage order), never visual order. This is what makes the algorithm indifferent to RTL display: Arabic and Hebrew are strong scripts like any other, and display-time reordering never enters the computation.

### 1.3 Scope and Non-Goals

**In scope:** segmentation and extraction of Latin runs, including attached neutral glue, from any Unicode string; correct behavior in the presence of RTL scripts, bidi control characters, and multiple digit families.

**Out of scope:** display/rendering of extracted runs (§8.4 notes one consequence); language identification within Latin runs (English vs. French, etc.); spell-aware or dictionary-aware segmentation; normalization policy of the input (the algorithm operates on the input as given; see §4.3).

---

## 2. Conformance and Conventions

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are to be interpreted as described in RFC 2119.

Conformance has two tiers:

- **Core conformance:** an implementation MUST produce the outputs specified in §12 test cases marked *Core*.
- **Full conformance:** an implementation additionally satisfies the cases marked *Full*, which exercise bidi controls, non-European digit families, and policy defaults.

Policy knobs (§9) allow deviation where explicitly noted; a conforming implementation MUST document any non-default knob settings.

**Unicode data version.** This specification depends normatively on UAX #29 segmentation and on the `Script`, `Script_Extensions`, `General_Category`, `Bidi_Paired_Bracket_Type`, `Extended_Pictographic`, `Pattern_White_Space`, and `Regional_Indicator` properties and the bidi-control character definitions, all of which are versioned with the Unicode Character Database. A conforming implementation MUST document the UCD version it uses, and conformance test results MUST record that version. This specification is written against Unicode 16.0 as the minimum reference version; character-level differences arising from later UCD versions are not conformance failures if documented.

---

## 3. Terminology

- **Code point:** a Unicode scalar value.
- **Grapheme cluster:** an extended grapheme cluster per UAX #29. The atomic unit of this algorithm; no run boundary may fall inside one.
- **Logical order:** the order of code points in storage (the order in which text is typed/read), as opposed to visual display order.
- **Strong character:** a character whose script property identifies a specific script (Latin, Hangul, Arabic, …).
- **Neutral character:** a character of script `Common` or `Inherited` (subject to reassignment rules in §5).
- **Run:** a maximal contiguous sequence of grapheme clusters sharing one class.
- **Binding affinity:** a per-cluster property (§7.3) indicating whether a neutral cluster attaches to the preceding text, the following text, or neither.
- **Anchor:** a neutral cluster whose affinity points toward the Latin run in a directional scan — `RIGHT` affinity in leading scans (§7.4), `LEFT` affinity in trailing scans (§7.5) — and whose absorption commits all provisionally held clusters between it and the run. Trailing digit groups additionally act as anchors under §7.5 rule 2.

---

## 4. Data Model and Preprocessing

### 4.1 Input

A Unicode string in logical order. The implementation MUST iterate by grapheme cluster (UAX #29 extended grapheme clusters). Classification (§5) is applied to the **classification code point** of each cluster; the cluster is then treated atomically.

The classification code point is defined operationally as follows:

1. Take the **first code point** of the cluster.
2. If its `Script` property is `Inherited`, take instead the first code point in the cluster whose script is **not** `Inherited`.
3. If every code point in the cluster is `Inherited` (degenerate input: a cluster of bare combining marks), the cluster classifies per §5.2(b).

This rule is deliberately mechanical rather than linguistic: prepended format characters with a strong script (e.g., U+0600 ARABIC NUMBER SIGN) correctly pull the cluster to `BASE`; emoji sequences classify by their first code point (typically `Common`, hence `NEUTRAL`, subject to the `STOP` affinity in §7.3); regional-indicator pairs classify as `NEUTRAL`. Implementations MUST use this rule (or one producing identical results for all clusters) so that independent implementations classify identically.

### 4.2 Output

An ordered list of extracted runs, each carrying:

- the substring (or, RECOMMENDED, `(start, end)` offsets into the original string — offsets preserve provenance and avoid copying),
- the offsets expressed in a documented unit (code points, UTF-8 bytes, or UTF-16 units — the implementation MUST state which).

### 4.3 Normalization

The algorithm does not require a particular normalization form. Implementations SHOULD document whether they normalize input (e.g., to NFC) before processing. Note that under NFD, combining marks appear as separate code points with script `Inherited`; rule §5.2(b) ensures they classify with their base character regardless.

---

## 5. Phase 1 — Character Classification

Each grapheme cluster is assigned exactly one class:

| Class | Meaning |
|---|---|
| `LATIN` | Strong Latin-script character |
| `BASE` | Strong character of any other script (Hangul, Han, Arabic, Hebrew, …) |
| `NEUTRAL` | Script-neutral character, candidate for glue |
| `CONTROL` | Bidi formatting character (§5.4) |
| `HARD_BREAK` | Paragraph/line boundary (§5.5) |

### 5.1 Base rule

Let `sc` be the Unicode `Script` property of the cluster's classification code point (§4.1).

- `sc = Latin` → `LATIN`
- `sc = Common` or `sc = Inherited` → `NEUTRAL`, subject to the overrides below
- otherwise → `BASE`

### 5.2 Overrides (applied in order; first match wins)

**(a) Hard breaks.** U+000A LINE FEED, U+000D CARRIAGE RETURN, U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR → `HARD_BREAK`. Implementations MAY additionally treat U+000B, U+000C, and U+0085 as hard breaks.

**(b) Inherited attachment.** By §4.1, the classification code point can have script `Inherited` only when **every** code point in the cluster is `Inherited` (degenerate input: a cluster of bare combining marks). Such a cluster takes the class of the nearest preceding cluster whose classification code point is not `Inherited`; at string start, `NEUTRAL`. (In well-formed input, marks are inside their base's grapheme cluster and this rule is moot.)

**(c) Bidi controls.** The characters in §5.4 → `CONTROL`, even though their script is `Common`.

**(d) Script-bound digits.** Digits whose script or `Script_Extensions` bind them to a specific script are **strong**, not neutral:

| Range | Name | Class |
|---|---|---|
| U+0660–U+0669 | ARABIC-INDIC DIGITS | `BASE` |
| U+06F0–U+06F9 | EXTENDED ARABIC-INDIC DIGITS | `BASE` |
| U+0030–U+0039 | DIGITS ZERO–NINE (European) | `NEUTRAL` |

The general rule: any digit block other than ASCII `0-9` whose `Script_Extensions` names a single non-Latin script MUST classify as `BASE`. An Arabic-Indic year in a copyright line belongs to the surrounding Arabic text and MUST NOT be absorbable Latin glue.

**(e) Script-affiliated punctuation and full-width forms.** Classification of characters whose usage is bound to the base script is governed by the `cjk_punct_strong` knob (§9):

- When `cjk_punct_strong = on` (the default), the following code points MUST classify as `BASE`, not `NEUTRAL`: U+3001 (、), U+3002 (。), U+FF0C (，), U+FF0E (．), corner brackets U+300C–U+300F, and the full-width forms block U+FF01–U+FF60 excluding the full-width Latin letters U+FF21–U+FF3A and U+FF41–U+FF5A (which are `Script = Latin` and classify `LATIN` by the base rule regardless).
- **The block coverage is deliberately broader than punctuation.** U+FF01–U+FF60 also contains the full-width ASCII digits U+FF10–U+FF19, currency signs, and mathematical symbols, and these are **intentionally included** in the strengthening: a full-width character is a typographic signal of CJK context, and a full-width digit or symbol in mixed-script text belongs to the host text for the same reason an Arabic-Indic digit does (§5.2d). In particular, `한국어 Windows １１` yields `Windows` — full-width digits are never trailing-capturable the way ASCII digits are. (Note also that `DIGIT` affinity in §7.3 is ASCII-only, so even with the knob off, full-width digits classify `NEUTRAL` with default `SEP` affinity and are still not captured as trailing digit groups; case 28 pins this. The knob *is* observable for full-width characters in sandwich position: case 29.)
- When `cjk_punct_strong = off`, these code points MUST instead classify according to the remaining rules of this section and §5.1 — normally `NEUTRAL`, and thereafter subject to the ordinary affinity derivation of §7.3 (most fall to the default `SEP`).
- Implementations MAY additionally generalize via `Script_Extensions` (a `Common` character whose extensions are exclusively non-Latin scripts → `BASE`). Such generalization applies only when the knob is on, and any generalization beyond the explicit list above MUST be documented; it is otherwise implementation-defined.

Rationale: full-width punctuation is the single most common false bridge between CJK text and an embedded Latin phrase; classifying it as strong eliminates that family of errors at zero cost. Conformance case 24 is the discriminating test for punctuation (`Alpha。Beta` splits into two runs when the knob is on, and sandwich-merges into one when it is off); case 29 is the discriminating test for full-width digits.

### 5.3 Latin detection

`LATIN` covers all Latin-script letters: ASCII `A–Z a–z`, Latin-1 letters, Latin Extended blocks, IPA extensions, etc. — i.e., `Script = Latin`, not merely ASCII.

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
[BASE][NEUTRAL][LATIN][NEUTRAL][LATIN][NEUTRAL][BASE][HARD_BREAK][NEUTRAL][LATIN]...
```

`CONTROL` clusters form their own runs at this stage (they are resolved in §8, before neutral resolution).

---

## 7. Phase 3 — Neutral Resolution

Each `NEUTRAL` run is examined with respect to its effective left neighbor `L` and right neighbor `R`, each drawn from `{LATIN, BASE, EDGE}` — where `EDGE` denotes string start/end or an adjacent `HARD_BREAK`.

### 7.1 Sandwich rule (merge)

**`L = LATIN` and `R = LATIN` → the entire neutral run merges**, joining the two Latin runs into one. This resolves internal glue: spaces, digits, parentheses, slashes, dots, hyphens inside `Windows 11 (23H2)` or a URL all fold in unconditionally.

*Bridge guard (policy knob `max_bridge`, §9):* when `max_bridge` is finite, a neutral run whose length **in grapheme clusters** exceeds `max_bridge` MUST NOT merge; its decision is `DISCARD`, leaving the two flanking Latin runs separate (the directional scans of §7.4/§7.5 do not apply, since both neighbors are `LATIN`). The default is ∞ (no limit); the hard-break wall (§5.5) provides the structural guard. An implementation MAY additionally refuse to bridge on other documented criteria (e.g., a sentence-terminal character followed by wide whitespace), but any such extension deviates from the reference behavior and MUST be documented as a non-default configuration.

### 7.2 Isolation rule (discard)

**`L ∈ {BASE, EDGE}` and `R ∈ {BASE, EDGE}` → the neutral run is discarded.** It has no Latin host on either side.

### 7.3 Binding affinity

At mixed boundaries, individual neutral **grapheme clusters** carry a **binding affinity**. `affinity(cluster)` MUST be a **total function** over `NEUTRAL` clusters: every neutral cluster deterministically maps to exactly one of five values.

| Affinity | Behavior in scans | Members |
|---|---|---|
| `RIGHT` | anchor toward following text | `©` U+00A9, `#`, `@`, `№`, `¿`, `¡`; opening brackets/quotes (`Ps`, `Pi`); currency signs (`Sc`) |
| `LEFT` | anchor toward preceding text | `™` U+2122, `℠`, `%`, `‰`, `°`, `+`, `®`*; closing brackets/quotes (`Pe`, `Pf`); sentence/phrase terminals `. , ; : ! ?` |
| `SEP` | traversable; absorbed only transitively | whitespace (`Zs`, tab), and **all neutral characters not matched by any other rule** (includes `— – · • | / \ = _ & ~ ^ * ' " -` and other `Po`/`Sm`/`Sk`/`Pd`/`Pc` characters) |
| `DIGIT` | directional (§7.4, §7.5) | ASCII digits U+0030–U+0039 **only** — full-width digits U+FF10–U+FF19 never receive `DIGIT` affinity (§5.2e; they are `BASE` under the default knob, or default-`SEP` neutrals otherwise) |
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

### 7.4 Leading-glue rule (`L ∈ {BASE, EDGE}`, `R = LATIN`)

Scan the neutral run **right-to-left**, starting at the edge adjacent to the Latin run. Maintain a *committed* boundary (initially: the Latin edge) and a *provisional* set (initially empty).

For each cluster encountered:

1. **`RIGHT` affinity** → absorb it **and commit** the entire provisional set. Continue scanning (further `RIGHT` anchors, separators, and digits may extend the capture, e.g., `#1 © 2026 Example`).
2. **`SEP` or `DIGIT`** → add to the provisional set; continue scanning. (With `numerals_bind_to_latin = on`, `DIGIT` instead commits like a `RIGHT` anchor; see §9 and §11.2.)
3. **`LEFT` affinity** → stop. It belongs to the preceding text (e.g., the `.` ending the preceding sentence). Provisional clusters not yet committed are released.
4. **`STOP`** → stop; release uncommitted provisionals.
5. Run exhausted → stop; release uncommitted provisionals.

The Latin run's start extends to the last committed position. Everything released stays outside the run and — because `L` is not Latin — is discarded.

**Worked example.** Input `…펜실베이니아. © 2026 Watch Tower…` — scanning back from `Watch`: space → provisional; `6 2 0 2` → provisional; space → provisional; `©` → `RIGHT` anchor, **commit all**; continue; `.` → `LEFT`, stop. Extracted run begins at `©`. The Korean sentence's period is untouched.

**Counter-example (no anchor).** Input `…텍스트 2026 Watch Tower…` — space and `2026` go provisional; next character is Hangul (run exhausted); nothing committed. Extracted run begins at `Watch`; the bare `2026` is correctly excluded. (Knob `numerals_bind_to_latin`, §9, can change this default.)

### 7.5 Trailing-glue rule (`L = LATIN`, `R ∈ {BASE, EDGE}`)

Scan **left-to-right** from the Latin edge:

1. **`LEFT` affinity** → absorb and commit all provisionals. If a digit group is open when the anchor is reached, the anchor **consumes** it: the group and everything between it and the anchor are committed by the anchor itself (which commits through its own position), and the group's open state MUST NOT survive past the anchor. The group does not separately self-commit under rule 2 — a stale group state that later triggered rule 2 would move the committed boundary *backward* from the anchor to the last digit, dropping the anchor. (See the normative pseudocode in §11.2; case 17 with `strip_terminal_punct = off` is the regression test.)
2. **`DIGIT`** → provisional while the digit group is open; on reaching the end of a **maximal digit group** (the next cluster is not `DIGIT`, or the run is exhausted), the group **commits itself and all provisionals between it and the committed boundary** — *unless* the **abutment exception** applies:
   > **Abutment exception.** A digit group whose final cluster is the final cluster of the neutral run, when `R = BASE`, does **not** self-commit; it remains provisional. Such a group is contiguous with the following strong non-Latin text and is presumed to belong to it (e.g., Korean counter constructions: in `Apple 5개`, the `5` binds to `개`; in Arabic, a digit group abutting a following Arabic word behaves likewise). A digit group abutting `EDGE` (string end or hard break) **does** self-commit.
   >
   > *The exception is intentionally limited to direct contiguity.* A separated form such as `Apple 5 개` **is** captured (`Apple 5`), because the digit group closes at the space and self-commits before the scan reaches the `BASE` neighbor. This is not an oversight: after coalescing, `Apple 5 개` and case 14's `⁨Windows 11⁩ نص` (post-strip) present the **identical** class structure `LATIN [SEP DIGIT⁺ SEP] BASE` — no rule operating on this algorithm's information can distinguish them, so widening the exception to spaced digits would necessarily break case 14. The narrow rule aligns with standard Korean orthography, in which counters attach directly to their numeral (`5개`); spaced counter forms are nonstandard and inherently ambiguous with the version-number pattern, and this specification resolves that ambiguity in favor of capture. Corpora where spaced host-language numerals dominate should set `trailing_digits_bind = off`. Cases 19 and 22 pin both sides of this line.
3. **`SEP`** → provisional.
4. **`RIGHT` affinity** → stop (an opener facing non-Latin text belongs to what follows, or dangles; either way it is not trailing glue). Release uncommitted provisionals.
5. **`STOP`** → stop; release uncommitted provisionals.
6. Run exhausted → stop; release uncommitted provisionals.

**Examples.**
- `macOS™의` → `™` is `LEFT`; absorbed; boundary breaks before the Hangul.
- `한국어 Windows 11` (string end) → trailing run `␣11`: space provisional, digit group `11` closes at `EDGE` → self-commits with the space → `Windows 11`.
- `…⁨Windows 11⁩ نص` (after control stripping, trailing run `␣11␣`) → digit group closes at the following space, not at run end → self-commits → `Windows 11`.
- `주소는 …a?b=1 입니다` → trailing run `=1␣`: `=` is `SEP` (rule 5 of §7.3) → provisional; digit group `1` closes at the space → commits itself **and** the `=` → `…a?b=1`.
- `사과 Apple 5개 주문` → trailing run `␣5`: digit group's final character is run-final and `R = BASE` → abutment exception → provisional, released → `Apple`.
- `use Windows 11, 그리고` → space provisional; `11` opens a digit group; `,` is `LEFT` → commits everything through itself, consuming the open group (rule 1); final space released → `use Windows 11,`; Phase-4 `strip_terminal_punct` then removes the comma.

**Both-sides case.** When a neutral run has `BASE`/`EDGE` on both sides it is discarded whole (§7.2); §7.4/§7.5 never apply. When `L = LATIN` and `R = LATIN` the sandwich rule (§7.1) preempts both scans.

### 7.5a Rationale: directional digit asymmetry (informative)

A single symmetric `DIGIT` behavior cannot satisfy the intent of this specification: leading bare numerals (`2026 Windows`) must be excluded absent an anchor, while trailing numerals (`Windows 11`) must be captured. The asymmetry is linguistic, not arbitrary: in Latin-script naming conventions, numerals **following** a Latin head are overwhelmingly attributive — version numbers, model numbers, standards, quantities (`Windows 11`, `iPhone 15`, `USB 3`, `HTTP 2`) — whereas numerals **preceding** a Latin word in mixed-script running text are typically independent host-sentence material (years, counts) unless a symbol such as `©` explicitly binds them forward. Hence: trailing digit groups self-commit (with the abutment exception guarding constructions like Korean counters); leading digit groups require a `RIGHT` anchor. The `trailing_digits_bind` knob (§9) disables self-commit for corpora where trailing numerals are predominantly host-language material.

### 7.6 Hard-break walls

`EDGE` arising from a `HARD_BREAK` behaves exactly like `EDGE` at string boundaries: directional scans operate normally on the neutral run, but no rule may reach across the break. Thus a line beginning `© 2026 Example Corp` still captures its leading glue (scan finds the `©` anchor before hitting the wall), while a Latin run at the end of one line never merges with one at the start of the next.

---

## 8. Bidi Control Handling

Bidi controls (§5.4) are resolved **before** Phase 3, so that neutral-resolution scans never see them.

### 8.1 Default: strip

When `bidi_controls = strip`, implementations MUST delete `CONTROL` clusters from consideration entirely: remove the control runs and re-coalesce adjacent runs of equal class. Matching, indexing, translation-memory, and other non-display applications SHOULD select this policy. An extracted `Windows 11` with a stray FSI at its front and no matching PDI is a malformed fragment; stripping prevents this class of defect.

### 8.2 Alternative: pair-aware structural handling (policy knob `bidi_controls = preserve_pairs`)

Where extracted runs must round-trip for display:

- An isolate pair `FSI/LRI/RLI … PDI` (or embedding pair `LRE/RLE/LRO/RLO … PDF`) whose **entire contents** lie within a single resolved Latin run MAY be preserved inside the run, treated as a unit.
- A pair that would **straddle** a run boundary MUST NOT be split; the implementation MUST either exclude the pair entirely or extend the run to cover it — it MUST NOT emit a run containing an unmatched initiator or terminator.
- **Unpaired** initiators/terminators at a boundary MUST be shed, never absorbed.

### 8.3 Practical note

In corpora produced by converters that insert directional isolates at RTL/LTR script boundaries, controls will sit **precisely at the segmentation boundaries this algorithm computes**. This is the common case, not a corner case; the strip-then-re-coalesce order of §8.1 handles it cleanly (e.g., `عربي⁦ FSI Windows 11 PDI ⁩عربي` reduces to `BASE NEUTRAL LATIN NEUTRAL BASE` after stripping, and resolves normally).

### 8.4 Display consequence (informative)

An extracted Latin run loses the ambient paragraph direction of its source. Standalone rendering defaults to LTR — correct for Latin content in virtually all cases. If a consumer re-displays runs inside RTL context and fidelity matters, wrap output in FSI…PDI or record source paragraph direction as run metadata. For non-display uses, ignore this section.

---

## 9. Policy Knobs

| Knob | Default | Effect |
|---|---|---|
| `strip_terminal_punct` | **on** | Phase-4 removal of trailing `. , ; : ! ?` captured by §7.5 |
| `numerals_bind_to_latin` | **off** | If on, **leading** digit groups bind toward adjacent Latin without requiring a `RIGHT` anchor (captures bare `2026 Windows`) |
| `trailing_digits_bind` | **on** | Trailing digit groups self-commit per §7.5 rule 2. If off, trailing digits are purely provisional (symmetric with leading behavior); test cases 2, 14, 18, 20, and 22 then change. (Case 17 is unaffected: its `LEFT` comma anchor commits the digits regardless — see §7.5 rule 1.) |
| `max_bridge` | ∞ | Maximum neutral-run length, in grapheme clusters, for the sandwich rule; a longer run between two Latin runs is discarded (§7.1). Encoded as `null` in the companion fixture's `default_policy` (`null` = ∞, no limit) |
| `bidi_controls` | `strip` | `strip` (§8.1) or `preserve_pairs` (§8.2) |
| `min_latin_letters` | 1 | Acceptance threshold (§10.2) |
| `affinity_overrides` | ∅ | Per-corpus additions/moves in the §7.3 table (e.g., `®` → `RIGHT`) |
| `cjk_punct_strong` | **on** | **CJK/full-width character strengthening** (§5.2e): **on** ⇒ the listed CJK punctuation and the full-width forms block U+FF01–U+FF60 (including full-width digits and symbols, excluding full-width Latin letters) MUST classify `BASE`; **off** ⇒ they classify per the remaining rules (normally `NEUTRAL`, default affinity `SEP`) |

### 9.1 Rationale for `strip_terminal_punct = on`

Sentence terminals following a Latin run usually punctuate the **surrounding** sentence, not the Latin entity (`…use Windows 11, 그리고…` — the comma is the host sentence's). Terminals *inside* an entity (`e.g.`, `Node.js`) are protected by the sandwich rule and are never trailing. Corpora rich in entities that legitimately end in periods should switch the knob off and handle terminals in the affinity table instead.

---

## 10. Phase 4 — Trim, Validate, Emit

### 10.1 Trim

For each resolved Latin run: remove pure-whitespace clusters from both edges (non-space glue such as `©`, `™`, brackets is retained).

If `strip_terminal_punct` is on, additionally strip terminal punctuation **iteratively from the run end**, as follows. The *terminal set* is exactly `. , ; : ! ?` (U+002E, U+002C, U+003B, U+003A, U+0021, U+003F). While the run's final grapheme cluster is a member of the terminal set: remove it, then remove any pure-whitespace clusters newly exposed at the run end, and repeat. **No other cluster is ever removed by this phase**; in particular, closing brackets, quotes, and symbols are never removed.

This mechanical rule replaces the informal "protected by a matching structure" language of earlier drafts and is the normative definition of protection: a terminal is protected precisely when it is not run-final at any step of the iteration — i.e., when at least one retained non-terminal cluster (typically a closing bracket) follows it. No bracket-matching computation is required, because the affinity scans (§7.5) determine which closers are retained in the first place. Consequences:

- `(Example.)` → the period is followed by the retained `)`; the final cluster is never a terminal; nothing is stripped → `(Example.)`.
- `Example.)` (closer with no matching opener inside the run) → the final cluster is `)`, not a terminal; iteration halts immediately and the period is retained → `Example.)`. This resolution of the malformed-nesting case — retention, not stripping — is deliberate and normative.
- `use Windows 11,` → the final cluster is `,` → stripped → `use Windows 11`.

Conformance cases 17, 26, and 27 pin this behavior.

### 10.2 Validate

A run MUST contain at least `min_latin_letters` characters of general category `L*` with script Latin. Runs failing the threshold are dropped — this removes any pathological glue-without-host survivor.

### 10.3 Emit

Output runs in logical order with offsets per §4.2.

---

## 11. Algorithm Summary (normative pseudocode)

### 11.1 Processing order and mutation semantics (normative)

Phase 3 is a **pure decision pass followed by a materialization pass**. All neutral-run decisions MUST be computed against the immutable Phase-2 run list (after §8 control resolution); no decision may observe the effect of another decision. Decisions are then applied in a single subsequent pass, after which adjacent `LATIN` runs are coalesced once.

This is well-defined and order-independent by construction: a neutral run's decision depends only on the **classes** of its neighboring runs, neighboring runs of a `NEUTRAL` run are always strong runs or edges (Phase 2 guarantees alternation), and no Phase-3 operation changes the class of a strong run. Implementations MAY use any evaluation order, or in-place left-to-right mutation, **provided** the results are identical to decide-then-materialize; the two-pass formulation is the reference semantics.

### 11.2 Pseudocode

```text
function extract_latin_runs(text, policy):
    clusters   = grapheme_clusters(text)                          # UAX #29
    classified = [ (c, classify(c, policy)) for c in clusters ]   # §5, §4.1
    runs       = coalesce(classified)                             # §6
    runs       = resolve_bidi_controls(runs, policy)              # §8 (strip → re-coalesce)

    # ---- Decision pass (no mutation) ----
    decisions = []
    for each run r of class NEUTRAL in runs:
        L = effective_left_neighbor(r)    # LATIN | BASE | EDGE  (HARD_BREAK ⇒ EDGE)
        R = effective_right_neighbor(r)
        if L == LATIN and R == LATIN:                                         # §7.1
            if policy.max_bridge is null or cluster_length(r) <= policy.max_bridge:  decisions.add(MERGE(r))
            else:                                       decisions.add(DISCARD(r))  # bridge guard
        elif L != LATIN and R != LATIN:    decisions.add(DISCARD(r))          # §7.2
        elif R == LATIN:                   decisions.add(split_leading(r, policy))     # §7.4
        else:                              decisions.add(split_trailing(r, R, policy)) # §7.5

    # ---- Materialization pass ----
    apply(decisions, runs)                # extend/merge/discard boundaries
    coalesce_adjacent_latin(runs)

    result = []
    for each run r of class LATIN in runs:
        trim(r, policy)                                     # §10.1
        if validate(r, policy): result.append(offsets(r))   # §10.2
    return result

function split_leading(r, policy):         # scan right→left from Latin edge, by cluster
    committed = latin_edge; provisional = []
    for cl in reverse_clusters(r):
        a = affinity(cl)                                    # total, §7.3
        if a == RIGHT:            committed = pos(cl); provisional = []  # anchor commits
        elif a == DIGIT and policy.numerals_bind_to_latin:
                                  committed = pos(cl); provisional = []  # digit group + intervening
                                                                         # SEPs commit without anchor
        elif a in {SEP, DIGIT}:   provisional.push(cl)      # default: leading digits need an anchor
        else:                     break                     # LEFT or STOP ⇒ stop
    return EXTEND_START_TO(committed)     # released provisionals stay outside

function split_trailing(r, R, policy):     # scan left→right from Latin edge, by cluster
    committed = latin_edge; provisional = []; group_open = false
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
            # unless it abuts following BASE text (abutment exception)
            if policy.trailing_digits_bind:
                unless (at_run_end and R == BASE):
                    committed = end_of_group; provisional = []
            group_open = false
```

Note that with `numerals_bind_to_latin = on`, each digit cluster in the leading scan commits like a `RIGHT` anchor, which by the standard commit mechanics also captures any provisional separators between the digit group and the run boundary — so `텍스트 2026 Windows` yields `2026 Windows` (group plus intervening space), while a lone `SEP` beyond the group is still released unless a further anchor appears. With `trailing_digits_bind = off`, trailing digit groups are purely provisional, restoring symmetric leading/trailing digit behavior; conformance cases 2, 14, 18, 20, and 22 assume the default (**on**) and change when it is off. Case 17 passes under either setting, because its trailing comma is a `LEFT` anchor that commits the digit group independently of the knob — but note that this invariant holds only because the `LEFT` branch above clears `group_open`. Without that clearing, the subsequent `SEP` (the space after the comma) would call `close_group()` and move `committed` backward from the comma to the final digit, which is observable when `strip_terminal_punct = off` (expected `use Windows 11,`, buggy output `use Windows 11`). Case 17's `strip_terminal_punct` sensitivity variant is the regression test for this.

Complexity: O(n) in clusters; each cluster is classified once and visited at most twice (coalescing plus at most one boundary scan).

---

## 12. Conformance Test Cases

Notation: input → expected extracted runs (as substrings). Base script shown as Korean (`한`) or Arabic (`ع`); results identical for any `BASE` script. Tier: **C** = Core, **F** = Full.

**Test inputs and expected outputs are normative as literal UTF-8 plain text.** Rendered, exported, or converted copies of this document are not authoritative for the conformance suite: document processors commonly transform content (auto-linking URLs into `<a>` markup, smart-quote substitution, dash conversion), and any such transformation of a test string invalidates that copy of the suite. Implementers MUST take test data from the plain-text source of this specification or from the machine-readable companion file `latin-run-extraction-tests.json`, which encodes all 29 cases with default-policy expectations, selected per-knob sensitivity variants, and explicit code-point listings for cases containing invisible characters. Where the table below and the companion file disagree, the companion file governs. Implementers SHOULD verify code points, not glyphs, when a case fails.

**JSON re-escaping hazard.** The authoritativeness of the companion file applies to its **original bytes**, parsed once by a conforming JSON parser. Copies of the fixture that have passed through an additional encoding layer — embedded in another JSON document, pasted into a code block, logged, or diffed by tools that escape backslashes — will display `\n` as `\\n` and similar, and inspecting such a copy will produce false conclusions about the fixture's content (this occurred in review: the single-escape `\n` of case 8, bytes `5C 6E`, parsing to U+000A, was misread as a double escape from a re-encoded copy). Auditors MUST verify escaping questions against the raw bytes of the original file or against the parsed string's code points, and SHOULD use the `input_codepoints` redundancy lists — which exist precisely to make such corruption and misreading mechanically detectable.

**Policy-sensitivity variants are normative but NOT exhaustive.** The default-policy expectations are complete for all 29 cases; the `policy_sensitivity` entries are selected normative expectations for particular non-default settings. The absence of an entry for a given knob does **not** imply the case is unaffected by that knob (broad knobs such as `min_latin_letters`, `max_bridge`, and `affinity_overrides` inherently affect many cases). Test harnesses MUST NOT infer invariance from an absent entry; the companion file carries `"policy_sensitivity_is_exhaustive": false` to the same effect.

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
| 11 | F | `عام ٢٠٢٦ Windows` | `Windows` | Arabic-Indic digits are BASE, not provisional glue |
| 12 | F | `عام 2026 Windows` | `Windows` | European digits provisional, no anchor, released |
| 13 | F | `النص © 2026 Example Corp نهاية` | `© 2026 Example Corp` | leading + trailing boundaries vs RTL base |
| 14 | F | `نص ⁨Windows 11⁩ نص` (FSI…PDI) | `Windows 11` | control strip → re-coalesce → trailing digit-group commit (group closed by following space, §7.5 rule 2) |
| 15 | F | `نص ‏Windows نص` (stray RLM) | `Windows` | unpaired control shed, never absorbed |
| 16 | F | `한국어。English 텍스트` | `English` | U+3002 boundary, basic form (output is knob-insensitive here; case 24 is the discriminating test for §5.2e) |
| 17 | C | `use Windows 11, 그리고` | `use Windows 11`‡ | trailing digits consumed by `,` LEFT anchor (§7.5 rule 1); `strip_terminal_punct` removes host-sentence comma; **regression test** (via ‡ variant) for anchor clearing digit-group state (§11.2) |
| 18 | C | `한국어 Windows 11` | `Windows 11` | trailing digit group abutting `EDGE` self-commits |
| 19 | C | `사과 Apple 5개 주문` | `Apple` | abutment exception: digit group contiguous with following BASE released |
| 20 | C | `값은 file-123 입니다` | `file-123`¶ | totality: `-` falls to default `SEP`, committed transitively by digit group |
| 21 | F | `축하 Party 🎉 완료` | `Party` | emoji at a run boundary is never absorbed (basic form; does not by itself discriminate `STOP` from `SEP` — case 25 does) |
| 22 | C | `사과 Apple 5 개 주문` | `Apple 5`§¶ | spaced digit group closes before run end → self-commits; contrast with case 19 |
| 23 | F | `한국어 Windows 1️⃣ 텍스트` | `Windows` | cluster-wide `STOP` test (§7.3.3): keycap sequence U+0031 U+FE0F U+20E3 is `STOP`, not `DIGIT` |
| 24 | F | `한국어 Alpha。Beta 텍스트` | `Alpha`, `Beta`ǁ | U+3002 classified `BASE` (§5.2e): prevents LATIN–NEUTRAL–LATIN sandwich merge; discriminating test for `cjk_punct_strong` |
| 25 | F | `축하 Party 🎉™ 완료` | `Party` | `Extended_Pictographic` → `STOP` blocks the scan from reaching a later `LEFT` anchor (`™`); a `SEP` misclassification would yield `Party 🎉™` |
| 26 | C | `한국어 (Example.) 텍스트` | `(Example.)` | §10.1 iterative stripping: run-final `)` is not a terminal, so the period is protected |
| 27 | C | `한국어 Example.) 텍스트` | `Example.)` | §10.1 malformed-nesting resolution: unmatched retained closer still protects the period (retention is normative) |
| 28 | F | `한국어 Windows １１` | `Windows` | full-width digits U+FF10–U+FF19: `BASE` under §5.2(e) strengthening, and never `DIGIT` affinity (§7.3) — so, unlike ASCII digits (case 18), never captured as a trailing digit group; output identical under `cjk_punct_strong = off` |
| 29 | F | `한국어 Alpha１Beta 텍스트` | `Alpha`, `Beta`ǁ | discriminating test that full-width **digits** (not only punctuation) are in the §5.2(e) strengthening set; a `NEUTRAL` classification sandwich-merges to `Alpha１Beta` |

\* Case 7: `(존 3:16)` yields no run — its only strong content is Hangul; parens/digits have no Latin host.
† Case 9: the neutral run `␣100␣` lies between BASE and LATIN, so the leading scan from `GB` holds the space and digits as provisionals; the run exhausts at Hangul with no `RIGHT` anchor, and they are released. Under defaults the run is `GB+`; capturing `100 GB+` requires `numerals_bind_to_latin = on`. This case is the canonical illustration of the bare-numeral policy knob, and implementations MUST document which setting their suite tests.
‡ With `strip_terminal_punct = off`: `use Windows 11,`. This variant is the regression test for §11.2's requirement that a `LEFT` anchor clear open digit-group state: an implementation with the stale-state bug emits `use Windows 11` (comma dropped) under this setting.
§ Case 22 vs. case 19: the abutment exception applies only to a digit group directly contiguous with following `BASE` text. `Apple 5개` (case 19) releases the numeral; `Apple 5 개` (case 22) captures it, and necessarily so — its class structure is identical to case 14's. See the rationale note in §7.5 rule 2.
¶ Cases 20 and 22 are sensitive to `trailing_digits_bind`: with the knob off, case 20 yields `file` (the `-123` suffix stays provisional and is released) and case 22 yields `Apple`. These variants are recorded in the companion fixture.
ǁ With `cjk_punct_strong = off` the strengthened code point classifies `NEUTRAL` (default affinity `SEP`), the sandwich rule merges, and the expected output becomes a single run: case 24 → `Alpha。Beta`; case 29 → `Alpha１Beta`.

---

## 13. Implementation Notes (informative)

- **Character data:** any Unicode-complete library suffices — ICU (`uscript_getScript`, `u_charType`, `ublock_getCode`), Python `unicodedata` + `regex` module script properties, Rust `unicode-script`/`unicode-segmentation`, JS `Intl.Segmenter` + `\p{Script=…}` regex properties. Grapheme segmentation MUST follow UAX #29; do not iterate raw code units.
- **Affinity table:** implement exactly the five-step derivation of §7.3 (overrides → explicit list → cluster-wide pictographic `STOP` test → general-category rules → default `SEP`). The function is total by construction; there is no "unclassified" state, and independent implementations MUST agree on every cluster given the same UCD version and overrides.
- **Offsets:** compute in the encoding of the source string; when the source is UTF-8, byte offsets are RECOMMENDED for zero-copy slicing.
- **Testing:** the §12 table is intentionally minimal; production suites should add NFC/NFD variants of cases 1–4, empty/all-neutral/all-Latin inputs, and adjacent hard breaks.

---

## 14. Changelog

**1.4** — Fourth review round:

1. **Case 8 escape report investigated and found to be a false positive; countermeasure adopted anyway.** Review reported the fixture's case 8 as containing a literal backslash-plus-`n` (`\\n` in JSON source) rather than U+000A. Byte-level inspection of the authoritative file shows the source is the correct single escape (`… 65 5C 6E C2 A9 …`), which parses to U+000A LINE FEED; the doubled escape existed only in a re-encoded copy inspected during review. The input is **unchanged**. Two hardening measures added: (a) case 8 now carries the `input_codepoints` redundancy list (including U+000A) so any escaping corruption or misreading is mechanically detectable; (b) §12 gains an explicit **JSON re-escaping hazard** warning requiring auditors to verify escaping questions against original bytes or parsed code points.
2. **`max_bridge: null` documented** (§9, fixture notes): in the companion fixture's `default_policy`, `null` normatively represents the specification default of ∞ (no bridge-length limit). Harnesses MUST NOT interpret it as zero, absent, or invalid.
3. **§5.2(e) scope made explicit** (retitled *Script-affiliated punctuation and full-width forms*): the U+FF01–U+FF60 strengthening deliberately covers the whole block — full-width digits U+FF10–U+FF19, currency signs, and symbols included — not only punctuation, by the same host-affiliation logic as §5.2(d). The §9 knob description is renamed accordingly (*CJK/full-width character strengthening*). §7.3 now states that `DIGIT` affinity is ASCII-only, so full-width digits are never trailing-capturable under either knob setting. New **case 28** (`Windows １１` → `Windows`, knob-insensitive) pins the default; new **case 29** (`Alpha１Beta`) is the knob-discriminating test for full-width digits specifically. Note that a trailing full-width-digit test alone cannot discriminate the knob (both settings yield `Windows`), which is why both cases exist.
4. Editorial: §7.3 opening now says `affinity(cluster)` over neutral grapheme clusters, matching the cluster-based derivation that follows.
5. §12 preamble updated: 29 cases; companion fixture updated in lockstep (`spec_version: "1.4"`).

**1.3** — Third review round:

1. **Normative pseudocode bug fixed** (§11.2, §7.5 rule 1): a `LEFT` anchor in `split_trailing` now clears `group_open`. Previously, a digit group open when the anchor was reached left stale state, and a subsequent `SEP`'s `close_group()` moved the committed boundary *backward* from the anchor to the last digit. Masked under the default policy (Phase-4 stripping removed the comma anyway), but observable in case 17 with `strip_terminal_punct = off`, whose expected output is `use Windows 11,`. Case 17's sensitivity variant is designated the regression test.
2. **Knob-sensitivity claims corrected** (§9, §11.2, §12; reverses the overstated claim in changelog 1.2 item 8): `trailing_digits_bind = off` also changes cases 20 (`file-123` → `file`) and 22 (`Apple 5` → `Apple`), not only 2, 14, and 18. The companion fixture gains the missing variants, and both the spec and fixture now state explicitly that policy-sensitivity entries are **selected, not exhaustive** (`"policy_sensitivity_is_exhaustive": false`); absence of an entry does not imply invariance.
3. **Case 16 demoted to a basic boundary case**; new discriminating **case 24** (`한국어 Alpha。Beta 텍스트` → `Alpha`, `Beta`) added. Case 16's output is identical whether U+3002 classifies `BASE` or neutral-`SEP`, so it could not detect the misclassification it claimed to exercise; the LATIN。LATIN form can (a `NEUTRAL` misclassification sandwich-merges to `Alpha。Beta`). Case 24 also carries the `cjk_punct_strong = off` variant.
4. **Case 21 demoted likewise**; new discriminating **case 25** (`축하 Party 🎉™ 완료` → `Party`) added. In case 21 both `STOP` and `SEP` classifications of 🎉 yield `Party`; placing a `LEFT` anchor (`™`) beyond the emoji makes incorrect traversal observable (`Party 🎉™`).
5. **`cjk_punct_strong` made deterministic** (§5.2e, §9): *on* ⇒ the listed code points MUST classify `BASE`; *off* ⇒ they classify per the remaining rules (normally `NEUTRAL`); `Script_Extensions` generalization is on-only, documented, and otherwise implementation-defined.
6. **`max_bridge` implemented in the normative pseudocode** (§7.1, §11.2): a neutral run between two Latin runs longer than `max_bridge` grapheme clusters is `DISCARD`ed, not merged. Previously the knob appeared in the policy table but had no effect in the reference algorithm (the same defect class fixed for the digit knobs in 1.2).
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

1. **Trailing digit groups now self-commit** (§7.5 rule 2), with an *abutment exception* for digit groups contiguous with following `BASE` text (Korean counters, etc.). Fixes cases 2, 14, and 17, which were unreachable under the 1.0 rules; adds cases 18–19. Rationale for the leading/trailing asymmetry recorded in new §7.5a; new knob `trailing_digits_bind`.
2. **`affinity(ch)` is now total** (§7.3): five-step normative derivation ending in a default of `SEP`; new `STOP` affinity for pictographics and unstripped controls. Defines behavior of `= _ & ~ - ^` etc.; adds cases 20–21.
3. **Phase-3 processing order made normative** (§11.1): decide-then-materialize reference semantics, with the order-independence argument stated.
4. **"Classification code point" defined operationally** (§4.1), replacing the undefined "base code point."
5. **Unicode data version pinning** required (§2).
6. Case 14's "Exercises" annotation corrected (it exercises trailing resolution, not the sandwich rule).

**1.0** — Initial draft.

---

*End of specification.*
