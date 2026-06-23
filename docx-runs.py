#!/usr/bin/env python3
"""
docx-runs.py — Determine the language of each run of text in a .docx file.

Pure XML parsing only: a .docx is just a ZIP archive of XML parts, so we use
the standard library (``zipfile`` + ``xml.etree.ElementTree``). No python-docx
or any other docx-specific library is involved.

WHAT NEEDS TO HAPPEN
--------------------
A "run" (<w:r>) is a contiguous span of text with uniform formatting. Its
language lives in the <w:lang> element inside the run properties (<w:rPr>).
But Word rarely stamps every run, so the language is resolved by walking an
inheritance hierarchy. For a given run we fall back, in order:

    1. Direct run properties .................. <w:r>/<w:rPr>/<w:lang>
    2. Character style ......................... <w:rStyle> -> styles.xml
       (following each style's <w:basedOn> chain up to its root)
    3. Paragraph style ......................... <w:pStyle> -> styles.xml,
       or the document's default paragraph style if the paragraph references
       none (also following <w:basedOn>)
    4. Document defaults ....................... styles.xml/<w:docDefaults>
    5. Application / OS default ................ unknown from the file alone

Note what is deliberately NOT in this chain: the paragraph mark's own run
properties (<w:p>/<w:pPr>/<w:rPr>/<w:lang>). That <w:rPr> formats the pilcrow
glyph and the carry-forward formatting for newly typed text; per ECMA-376 it
is not a source of inheritance for the existing runs inside the paragraph. It
often happens to match (Word tends to stamp the mark with the same language as
the runs), which is exactly why using it as a fallback looks right until it
matters -- mixed-language paragraphs and content imported from other editors.

<w:lang> itself carries up to three attributes, one per script family:

    w:val      -> default (Latin / Western) language
    w:eastAsia -> East Asian ideographs (Korean, Chinese, Japanese, ...)
    w:bidi     -> right-to-left bidirectional text (Arabic, Hebrew, ...)

Because a single run can mix scripts, we additionally inspect the actual
characters of the run text to report which of the three slots actually
applies to that text.

Usage:
    python3 docx-runs.py path/to/file.docx
    python3 docx-runs.py path/to/file.docx --json
"""

import argparse
import json
import sys
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

# The WordprocessingML namespace. Every w:* element/attribute lives here.
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def w(tag):
    """Return a Clark-notation qualified name for a w:* tag, e.g. '{...}lang'."""
    return f"{{{W}}}{tag}"


# ---------------------------------------------------------------------------
# Loading the parts we care about out of the ZIP container
# ---------------------------------------------------------------------------

def load_docx(path):
    """Return (document_root, styles_root). styles_root may be None."""
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())

        if "word/document.xml" not in names:
            raise ValueError(
                f"{path!r} does not look like a .docx (no word/document.xml)"
            )
        document = ET.fromstring(zf.read("word/document.xml"))

        styles = None
        if "word/styles.xml" in names:
            styles = ET.fromstring(zf.read("word/styles.xml"))

    return document, styles


# ---------------------------------------------------------------------------
# Reading a <w:lang> element wherever it appears
# ---------------------------------------------------------------------------

def lang_from_rpr(rpr):
    """
    Given a run-properties element (<w:rPr>), return a dict with any of
    'val', 'eastAsia', 'bidi' that are set on its <w:lang> child, or None.
    """
    if rpr is None:
        return None
    lang = rpr.find("w:lang", NS)
    if lang is None:
        return None
    result = {}
    for attr in ("val", "eastAsia", "bidi"):
        value = lang.get(w(attr))
        if value:
            result[attr] = value
    return result or None


# ---------------------------------------------------------------------------
# styles.xml: build a lookup so we can resolve rStyle / pStyle references
# ---------------------------------------------------------------------------

def build_style_index(styles_root):
    """
    Return (styles_by_id, doc_defaults_lang, default_para_style).

    styles_by_id maps styleId -> {
        'lang':    <lang dict or None>,   # from this style's own rPr
        'basedOn': <parent styleId or None>,
    }

    doc_defaults_lang is the <w:lang> dict from <w:docDefaults> (the global
    baseline), or None.

    default_para_style is the styleId of the style marked
    <w:style w:default="1" w:type="paragraph">, which applies to any paragraph
    that references no pStyle. (The default *character* style is intentionally
    not tracked: unlike the default paragraph style it is not auto-applied to
    every run -- it only takes effect when a run references it via rStyle.)
    """
    styles_by_id = {}
    doc_defaults_lang = None
    default_para_style = None

    if styles_root is None:
        return styles_by_id, doc_defaults_lang, default_para_style

    # Document defaults: docDefaults/rPrDefault/rPr/lang
    rpr_default = styles_root.find("w:docDefaults/w:rPrDefault/w:rPr", NS)
    doc_defaults_lang = lang_from_rpr(rpr_default)

    for style in styles_root.findall("w:style", NS):
        style_id = style.get(w("styleId"))
        if style_id is None:
            continue
        rpr = style.find("w:rPr", NS)
        based_on_el = style.find("w:basedOn", NS)
        based_on = based_on_el.get(w("val")) if based_on_el is not None else None
        styles_by_id[style_id] = {
            "lang": lang_from_rpr(rpr),
            "basedOn": based_on,
        }

        if (style.get(w("type")) == "paragraph"
                and style.get(w("default")) in ("1", "true")):
            default_para_style = style_id

    return styles_by_id, doc_defaults_lang, default_para_style


def resolve_style_lang(style_id, styles_by_id, _seen=None):
    """
    Walk a style's basedOn chain and return the first <w:lang> dict found,
    or None. Guards against cyclic basedOn references.
    """
    if style_id is None:
        return None
    if _seen is None:
        _seen = set()
    if style_id in _seen:
        return None
    _seen.add(style_id)

    entry = styles_by_id.get(style_id)
    if entry is None:
        return None
    if entry["lang"]:
        return entry["lang"]
    return resolve_style_lang(entry["basedOn"], styles_by_id, _seen)


# ---------------------------------------------------------------------------
# The resolution hierarchy for a single run
# ---------------------------------------------------------------------------

def resolve_run_lang(run, paragraph, styles_by_id, doc_defaults_lang,
                     default_para_style):
    """
    Resolve the language for a run, returning (lang_dict, source_label).

    Implements the fallback order described in the module docstring. Note that
    the paragraph mark's run properties (pPr/rPr) are deliberately NOT consulted
    -- per ECMA-376 they format the pilcrow, not the runs inside the paragraph.
    """
    # 1. Direct run properties.
    rpr = run.find("w:rPr", NS)
    direct = lang_from_rpr(rpr)
    if direct:
        return direct, "run/rPr (direct)"

    # 2. Character style (rStyle) -> styles.xml, following basedOn.
    if rpr is not None:
        rstyle_el = rpr.find("w:rStyle", NS)
        if rstyle_el is not None:
            rstyle_id = rstyle_el.get(w("val"))
            lang = resolve_style_lang(rstyle_id, styles_by_id)
            if lang:
                return lang, f"character style {rstyle_id!r}"

    # 3. Paragraph style (pStyle) -> styles.xml, or the document's default
    #    paragraph style if the paragraph references none.
    pstyle_id = None
    if paragraph is not None:
        ppr = paragraph.find("w:pPr", NS)
        if ppr is not None:
            pstyle_el = ppr.find("w:pStyle", NS)
            if pstyle_el is not None:
                pstyle_id = pstyle_el.get(w("val"))
    if pstyle_id is None:
        pstyle_id = default_para_style
    if pstyle_id:
        lang = resolve_style_lang(pstyle_id, styles_by_id)
        if lang:
            label = f"paragraph style {pstyle_id!r}"
            if pstyle_id == default_para_style:
                label += " (default)"
            return lang, label

    # 4. Document defaults (docDefaults).
    if doc_defaults_lang:
        return doc_defaults_lang, "docDefaults"

    # 5. Nothing in the file. The consuming app would use the OS locale.
    return None, "application/OS default (not in file)"


# ---------------------------------------------------------------------------
# Inspect the characters to decide which lang slot actually applies
# ---------------------------------------------------------------------------

def classify_script(text):
    """
    Decide which <w:lang> attribute slot the run's characters fall under by
    looking at the dominant script of the (non-space) characters:

        'eastAsia' -> CJK / Hangul / Kana
        'bidi'     -> Arabic / Hebrew (and related RTL blocks)
        'val'      -> everything else (Latin, Cyrillic, Greek, digits, ...)

    Returns the slot name, or None if the run has no classifiable characters.
    """
    counts = {"val": 0, "eastAsia": 0, "bidi": 0}

    for ch in text:
        if ch.isspace():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue  # control chars and the like have no name

        if any(tok in name for tok in
               ("CJK", "HANGUL", "HIRAGANA", "KATAKANA", "BOPOMOFO", "YI ")):
            counts["eastAsia"] += 1
        elif any(tok in name for tok in
                 ("ARABIC", "HEBREW", "SYRIAC", "THAANA")):
            counts["bidi"] += 1
        elif ch.isalpha():
            counts["val"] += 1

    if not any(counts.values()):
        return None
    return max(counts, key=counts.get)


def slot_from_font_hint(run):
    """
    For runs whose characters carry no script of their own (punctuation,
    digits, symbols), Word records the intended context on the run's font
    table via <w:rFonts w:hint="...">. Map that hint to a lang slot:

        hint="eastAsia" -> 'eastAsia'
        hint="cs"       -> 'bidi'      (cs = complex script / RTL)
        hint="default"  -> 'val'

    Returns the slot name, or None if there is no usable hint.
    """
    rpr = run.find("w:rPr", NS)
    if rpr is None:
        return None
    rfonts = rpr.find("w:rFonts", NS)
    if rfonts is None:
        return None
    hint = rfonts.get(w("hint"))
    return {"eastAsia": "eastAsia", "cs": "bidi", "default": "val"}.get(hint)


# ---------------------------------------------------------------------------
# Walk the document and report
# ---------------------------------------------------------------------------

# A code point is language-neutral if it belongs to no script: ordinary
# whitespace, zero-width spaces, or Unicode bidi formatting controls. A naive
# str.isspace()/str.strip() test catches only the first group, so the bidi
# controls -- which carry no language but, lacking a script or font hint, would
# otherwise resolve to w:val and shatter a contiguous language span -- get
# treated as real text. Centralizing the policy here keeps it easy to extend.
_NEUTRAL_CODEPOINTS = frozenset({
    0x0009, 0x000A, 0x000D, 0x0020,   # tab, LF, CR, space
    0x00A0,                           # no-break space
    0x200B,                           # zero-width space
    0x200E, 0x200F,                   # LRM, RLM
    0x2066, 0x2067, 0x2068, 0x2069,   # LRI, RLI, FSI, PDI
})


def is_neutral_codepoint(cp):
    """True if the code point belongs to no script (whitespace / bidi control)."""
    return cp in _NEUTRAL_CODEPOINTS or 0x202A <= cp <= 0x202E  # LRE/RLE/PDF/LRO/RLO


def is_neutral_run(text):
    """True when a run carries no language: every code point is neutral."""
    return all(is_neutral_codepoint(ord(ch)) for ch in text)


def run_text(run):
    """Concatenate the visible text of a run: <w:t>, <w:tab>, <w:br>, ..."""
    pieces = []
    for child in run:
        tag = child.tag
        if tag == w("t"):
            pieces.append(child.text or "")
        elif tag == w("tab"):
            pieces.append("\t")
        elif tag in (w("br"), w("cr")):
            pieces.append("\n")
    return "".join(pieces)


def coalesce(results):
    """
    Merge adjacent runs that resolve to the same effective language within the
    same paragraph. Word splits runs for editing reasons (IME commits, revision
    IDs, etc.) even when formatting is identical, so merging gives one segment
    per contiguous language span. Run text is concatenated; the first run's
    metadata is kept and a 'runs' count records how many were merged.
    """
    merged = []
    for r in results:
        prev = merged[-1] if merged else None
        same_para = prev is not None and prev["para"] == r["para"]
        # A language-neutral run (whitespace / zero-width / bidi control) folds
        # into the current segment rather than starting or breaking one.
        if same_para and (r["neutral"] or prev["effective"] == r["effective"]):
            prev["text"] += r["text"]
            if not r["neutral"]:
                prev["runs"] += 1
                prev["neutral"] = False  # segment now contains real text
        else:
            entry = dict(r)
            entry["runs"] = 1
            merged.append(entry)
    return merged


def analyze(path, merge=False):
    document, styles = load_docx(path)
    styles_by_id, doc_defaults_lang, default_para_style = build_style_index(styles)

    results = []
    body = document.find("w:body", NS)
    if body is None:
        return results

    # iter() finds runs anywhere (paragraphs, tables, etc.). To know each run's
    # parent paragraph for the pPr fallback, walk paragraphs explicitly.
    for para_index, paragraph in enumerate(document.iter(w("p"))):
        prev_slot = None  # last script-bearing slot seen in this paragraph
        for run in paragraph.findall("w:r", NS):
            text = run_text(run)
            if not text:
                continue  # skip truly empty runs

            # Language-neutral runs (whitespace, zero-width spaces, and bidi
            # formatting controls) carry no language of their own. They are
            # hidden in the per-run view but kept so that --merge can preserve
            # the spacing/formatting between the words it joins.
            neutral = is_neutral_run(text)

            lang, source = resolve_run_lang(
                run, paragraph, styles_by_id, doc_defaults_lang,
                default_para_style,
            )

            # Decide which lang slot applies to this run's characters:
            #   1. the dominant script of the characters themselves
            #   2. (script-neutral runs) Word's <w:rFonts w:hint="..."> signal
            #   3. (still neutral) inherit the previous run's slot in the para
            slot = classify_script(text)
            if slot is None:
                slot = slot_from_font_hint(run) or prev_slot
            else:
                prev_slot = slot

            # The "effective" language for this run's actual characters:
            # pick the lang attribute matching the chosen slot, falling back
            # to w:val if that slot is unset.
            effective = None
            if lang:
                if slot and slot in lang:
                    effective = lang[slot]
                else:
                    effective = lang.get("val") or next(iter(lang.values()))

            results.append({
                "text": text,
                "neutral": neutral,      # language-neutral (hidden unless merged)
                "para": para_index,      # parent paragraph index (for merging)
                "lang": lang,            # full dict (val/eastAsia/bidi) or None
                "source": source,        # where in the hierarchy it came from
                "script_slot": slot,     # which slot the characters fall under
                "effective": effective,  # best single language tag for the text
            })

    if merge:
        results = coalesce(results)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report the resolved language of every text run in a .docx"
    )
    parser.add_argument("docx", help="path to the .docx file")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    parser.add_argument("--merge", action="store_true",
                        help="coalesce adjacent runs of the same language")
    args = parser.parse_args(argv)

    try:
        results = analyze(args.docx, merge=args.merge)
    except (zipfile.BadZipFile, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if not results:
        print("No text runs found.")
        return 0

    shown = [r for r in results if not r.get("neutral")]
    for i, r in enumerate(shown, 1):
        preview = r["text"] if len(r["text"]) <= 60 else r["text"][:57] + "..."
        preview = preview.replace("\n", "\\n").replace("\t", "\\t")
        lang_str = (
            ", ".join(f"{k}={v}" for k, v in r["lang"].items())
            if r["lang"] else "(none)"
        )
        merged = f" (merged {r['runs']} runs)" if r.get("runs", 1) > 1 else ""
        print(f"[{i:>3}] {r['effective'] or '?':<8} | {preview}")
        print(f"      lang: {lang_str}")
        print(f"      via:  {r['source']}  (script slot: {r['script_slot']}){merged}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
