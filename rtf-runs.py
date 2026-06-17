#!/usr/bin/env python3
"""Segment RTF body text into runs and report the language and character set
in effect for each run.

A "run" here is a maximal stretch of body text whose relevant character
properties are constant. By default runs break on a change to any of:
    \\lang     Western / Latin proofing language
    \\langfe   East-Asian (Far East) proofing language
    \\f        current font  (and therefore \\fcharset -> codepage)

Bytes written as \\'xx escapes are decoded with the codepage implied by the
active font's \\fcharset (falling back to the document \\ansicpg). \\uN
escapes are decoded directly and their \\ucN fallback bytes are skipped.

Known simplifications:
  * RTF keeps three font "slots" active at once (\\loch low-ANSI,
    \\hich high-ANSI, \\dbch double-byte). This tool tracks the single
    current font set by \\f, which is what \\hich/\\loch text usually
    resolves to in Word output; it does not model per-slot fonts
    separately. For the common case (Latin + one CJK font) the reported
    charset is correct; deeply mixed slots may need slot-aware decoding.
  * Header destinations (fonttbl, colortbl, stylesheet, info, and any
    {\\* ...} ignorable destination) are parsed but not emitted as runs.

Examples:
    rtf-runs.py FILE.rtf                      # human-readable table
    rtf-runs.py FILE.rtf --json                # one JSON object per run (JSON Lines)
    rtf-runs.py FILE.rtf --min-len 1            # drop whitespace-only runs
    rtf-runs.py FILE.rtf --break-on lang,langfe,font,charset

Exit status:
    0   success
    1   the file could not be read
    2   usage error (bad/missing arguments; handled by argparse)
"""

import argparse
import json
import re
import sys

__version__ = "1.0.0"

# ---- charset (\fcharsetN) -> Python codec used to decode \'xx bytes --------
CHARSET_CODEPAGE = {
    0:   "cp1252",   # ANSI (overridden by \ansicpg if present)
    2:   "cp1252",   # Symbol — no clean mapping; treat as Latin-1ish
    77:  "mac_roman",
    128: "cp932",    # ShiftJIS (Japanese)
    129: "cp949",    # Hangul (Korean)
    130: "cp1361",   # Johab (Korean)
    134: "cp936",    # GB2312 (Simplified Chinese)
    136: "cp950",    # Big5 (Traditional Chinese)
    161: "cp1253",   # Greek
    162: "cp1254",   # Turkish
    163: "cp1258",   # Vietnamese
    177: "cp1255",   # Hebrew
    178: "cp1256",   # Arabic
    186: "cp1257",   # Baltic
    204: "cp1251",   # Cyrillic
    222: "cp874",    # Thai
    238: "cp1250",   # Eastern European (Latin 2)
    254: "cp437",    # PC 437
    255: "cp850",    # OEM (approx)
}

CHARSET_NAME = {
    0: "ANSI", 2: "Symbol", 77: "Mac", 128: "ShiftJIS/Japanese",
    129: "Hangul/Korean", 130: "Johab", 134: "GB2312/SimplifiedChinese",
    136: "Big5/TraditionalChinese", 161: "Greek", 162: "Turkish",
    163: "Vietnamese", 177: "Hebrew", 178: "Arabic", 186: "Baltic",
    204: "Cyrillic", 222: "Thai", 238: "EasternEuropean", 254: "PC437",
    255: "OEM",
}

# ---- a usable subset of Windows LCIDs -> human names -----------------------
# Extend freely; unknown codes are reported as "LCID <n>".
LCID = {
    0: "None/NoProof", 1024: "Default", 1025: "Arabic (Saudi Arabia)",
    1028: "Chinese (Taiwan)", 1029: "Czech", 1030: "Danish",
    1031: "German (Germany)", 1032: "Greek", 1033: "English (United States)",
    1034: "Spanish (Spain)", 1035: "Finnish", 1036: "French (France)",
    1037: "Hebrew", 1040: "Italian (Italy)", 1041: "Japanese",
    1042: "Korean", 1043: "Dutch (Netherlands)", 1044: "Norwegian (Bokmal)",
    1045: "Polish", 1046: "Portuguese (Brazil)", 1049: "Russian",
    1051: "Slovak", 1053: "Swedish", 1054: "Thai", 1055: "Turkish",
    1057: "Indonesian", 1058: "Ukrainian", 1060: "Slovenian",
    1066: "Vietnamese", 1081: "Hindi", 1093: "Bengali (India)",
    1095: "Gujarati", 2052: "Chinese (PRC)", 2057: "English (United Kingdom)",
    2058: "Spanish (Mexico)", 3076: "Chinese (Hong Kong)",
    3082: "Spanish (Spain, Modern)", 4108: "French (Switzerland)",
}

def lcid_name(n):
    if n is None:
        return "unset"
    return LCID.get(n, f"LCID {n}")


# ---------------------------------------------------------------------------
# Font table: map font number -> (name, charset)
# ---------------------------------------------------------------------------
def parse_font_table(rtf):
    """Return {fontnum: {'name', 'charset'}} parsed from {\\fonttbl ...}."""
    start = rtf.find(r"{\fonttbl")
    if start == -1:
        return {}
    # find the matching closing brace for the fonttbl group
    depth = 0
    i = start
    while i < len(rtf):
        c = rtf[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = rtf[start:i + 1]

    # split the fonttbl into its balanced top-level subgroups, one per font.
    # entries may start with a theme keyword, e.g. {\fhiminor\f31506 ...},
    # so we can't assume the group opens with the font number.
    entries = []
    depth = 0
    buf = []
    inner = block[len(r"{\fonttbl"):-1]  # strip the outer {\fonttbl ... }
    for ch in inner:
        if ch == "{":
            if depth == 0:
                buf = []
            depth += 1
            buf.append(ch)
        elif ch == "}":
            buf.append(ch)
            depth -= 1
            if depth == 0:
                entries.append("".join(buf))
        elif depth > 0:
            buf.append(ch)

    fonts = {}
    for entry in entries:
        fm = re.search(r"\\f(\d+)\b", entry)        # first real font number
        if not fm:
            continue
        num = int(fm.group(1))
        cm = re.search(r"\\fcharset(\d+)", entry)
        charset = int(cm.group(1)) if cm else 0
        # font name = text before the ';', minus nested groups and control words
        name_part = entry.split(";")[0]
        name_part = re.sub(r"\{\\\*.*?\}", "", name_part, flags=re.DOTALL)
        name_part = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", name_part)
        name = name_part.strip().lstrip("{").strip() or f"f{num}"
        fonts[num] = {"name": name, "charset": charset}
    return fonts


# ---------------------------------------------------------------------------
# Run accumulator: buffers raw bytes (decoded per-codepage) + unicode text
# ---------------------------------------------------------------------------
class Run:
    def __init__(self, key, codepage):
        self.key = key            # (lang, langfe, font, charset)
        self.codepage = codepage
        self._parts = []          # list of ('b', bytearray) | ('t', str)

    def add_byte(self, b):
        if self._parts and self._parts[-1][0] == "b":
            self._parts[-1][1].append(b)
        else:
            self._parts.append(("b", bytearray([b])))

    def add_text(self, s):
        self._parts.append(("t", s))

    def text(self):
        out = []
        for kind, val in self._parts:
            if kind == "b":
                try:
                    out.append(bytes(val).decode(self.codepage))
                except (UnicodeDecodeError, LookupError):
                    out.append(bytes(val).decode("latin-1"))
            else:
                out.append(val)
        return "".join(out)


# ---------------------------------------------------------------------------
# Main walk
# ---------------------------------------------------------------------------
SKIP_DESTINATIONS = {
    "fonttbl", "colortbl", "stylesheet", "listtable", "listoverridetable",
    "rsidtbl", "generator", "info", "pgptbl", "themedata", "colorschememapping",
    "latentstyles", "datastore", "xmlnstbl", "pict", "fldinst", "fldrslt",
    "header", "footer", "footnote", "annotation", "wgrffmtfilter",
}

def analyze(rtf, break_on):
    fonts = parse_font_table(rtf)

    m = re.search(r"\\ansicpg(\d+)", rtf)
    ansicpg = f"cp{m.group(1)}" if m else "cp1252"

    m = re.search(r"\\deflang(\d+)", rtf)
    deflang = int(m.group(1)) if m else None
    m = re.search(r"\\deflangfe(\d+)", rtf)
    deflangfe = int(m.group(1)) if m else None

    def codepage_for(charset):
        if charset == 0:
            return ansicpg
        return CHARSET_CODEPAGE.get(charset, ansicpg)

    # character-property state
    state = {"lang": deflang, "langfe": deflangfe, "f": None, "uc": 1}
    stack = []
    skip_to_depth = None   # depth at which to resume emitting (for destinations)
    depth = 0
    runs = []
    cur = None
    pending_skip = 0       # \uc fallback characters still to swallow

    def cur_charset():
        f = state["f"]
        if f in fonts:
            return fonts[f]["charset"]
        return 0

    def make_key():
        f = state["f"]
        cs = cur_charset()
        full = (state["lang"], state["langfe"], f, cs)
        # the comparison key honours only the requested break fields
        sel = []
        if "lang" in break_on:    sel.append(full[0])
        if "langfe" in break_on:  sel.append(full[1])
        if "font" in break_on:    sel.append(full[2])
        if "charset" in break_on: sel.append(full[3])
        return tuple(sel), full

    def emit_char_byte(b=None, text=None):
        nonlocal cur
        if skip_to_depth is not None:
            return
        cmpkey, full = make_key()
        if cur is None or cur.key != cmpkey:
            if cur is not None:
                runs.append((cur, cur._full))
            cur = Run(cmpkey, codepage_for(full[3]))
            cur._full = full
        if b is not None:
            cur.add_byte(b)
        else:
            cur.add_text(text)

    i, n = 0, len(rtf)
    while i < n:
        c = rtf[i]

        if c == "{":
            stack.append((dict(state), skip_to_depth))
            depth += 1
            i += 1
            continue

        if c == "}":
            depth -= 1
            if skip_to_depth is not None and depth < skip_to_depth:
                skip_to_depth = None
            if stack:
                state, _saved_skip = stack.pop()
            i += 1
            continue

        if c == "\\":
            # control word with optional signed parameter
            m = re.match(r"\\([a-zA-Z]+)(-?\d+)?", rtf[i:])
            if m:
                word = m.group(1)
                param = int(m.group(2)) if m.group(2) is not None else None
                i += m.end()
                if i < n and rtf[i] == " ":   # delimiter space is consumed
                    i += 1

                if word == "u" and param is not None:    # \uN unicode char
                    cp = param if param >= 0 else 65536 + param
                    emit_char_byte(text=chr(cp))
                    pending_skip = state["uc"]
                    continue
                if word == "uc":
                    state["uc"] = param if param is not None else 1
                    continue
                if word == "lang" and param is not None:
                    state["lang"] = param; continue
                if word == "langfe" and param is not None:
                    state["langfe"] = param; continue
                if word == "f" and param is not None:
                    state["f"] = param; continue
                if word == "plain":
                    state["lang"] = deflang
                    state["langfe"] = deflangfe
                    continue
                if word == "par" or word == "line":
                    emit_char_byte(text="\n"); continue
                if word == "tab":
                    emit_char_byte(text="\t"); continue
                if word in SKIP_DESTINATIONS and skip_to_depth is None:
                    skip_to_depth = depth
                    continue
                # any other control word: ignore
                continue

            # control symbol
            sym = rtf[i + 1] if i + 1 < n else ""
            if sym == "'":                      # \'xx hex escape (one byte)
                hexpair = rtf[i + 2:i + 4]
                i += 4
                try:
                    b = int(hexpair, 16)
                except ValueError:
                    continue
                if pending_skip > 0:            # swallowed \uc fallback
                    pending_skip -= 1
                    continue
                emit_char_byte(b=b)
                continue
            if sym == "*":                      # ignorable destination
                if skip_to_depth is None:
                    skip_to_depth = depth
                i += 2
                continue
            if sym in ("{", "}", "\\"):         # escaped literal
                if pending_skip > 0:
                    pending_skip -= 1
                else:
                    emit_char_byte(text=sym)
                i += 2
                continue
            if sym == "~":
                emit_char_byte(text="\u00a0"); i += 2; continue
            if sym in ("-", "_"):               # optional/non-breaking hyphen
                i += 2; continue
            # other control symbol (\n in source, etc.)
            i += 2
            continue

        # plain literal character
        if c in ("\r", "\n"):                   # RTF line breaks are not text
            i += 1
            continue
        if pending_skip > 0:                    # \uc fallback literal char
            pending_skip -= 1
            i += 1
            continue
        emit_char_byte(text=c)
        i += 1

    if cur is not None:
        runs.append((cur, cur._full))

    # build result records
    records = []
    for run, full in runs:
        lang, langfe, font, charset = full
        records.append({
            "text": run.text(),
            "lang": lang,
            "lang_name": lcid_name(lang),
            "langfe": langfe,
            "langfe_name": lcid_name(langfe),
            "font": fonts.get(font, {}).get("name") if font in fonts else None,
            "font_num": font,
            "charset": charset,
            "charset_name": CHARSET_NAME.get(charset, f"charset{charset}"),
            "codepage": run.codepage,
        })
    return records


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="rtf-runs.py",
        description="Segment RTF body text into runs by language/charset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  rtf-runs.py FILE.rtf\n"
            "  rtf-runs.py FILE.rtf --json\n"
            "  rtf-runs.py FILE.rtf --min-len 1\n"
            "  rtf-runs.py FILE.rtf --break-on lang,langfe,font,charset\n"
        ),
    )
    parser.add_argument("file", help="RTF file to analyze")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON Lines (one object per run)")
    parser.add_argument("--min-len", type=int, default=0,
                        help="drop runs whose stripped text is shorter than this")
    parser.add_argument("--break-on", default="lang,langfe,font",
                        help="comma list of fields that start a new run: "
                             "lang,langfe,font,charset (default: %(default)s)")
    parser.add_argument("--version", action="version",
                        version="%(prog)s {0}".format(__version__))
    return parser.parse_args(argv)


def _use_utf8_stdout():
    """Emit UTF-8 so the run-break glyphs don't crash on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    args = parse_args(argv)
    _use_utf8_stdout()

    break_on = {x.strip() for x in args.break_on.split(",") if x.strip()}

    try:
        with open(args.file, "rb") as fh:
            rtf = fh.read().decode("latin-1")
    except OSError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1

    records = analyze(rtf, break_on)
    if args.min_len:
        records = [r for r in records if len(r["text"].strip()) >= args.min_len]

    if args.json:
        for r in records:
            print(json.dumps(r, ensure_ascii=False))
        return 0

    for idx, r in enumerate(records, 1):
        disp = r["text"].replace("\n", "\u23ce").replace("\t", "\u2192")
        if len(disp) > 60:
            disp = disp[:57] + "..."
        font = r["font"] or f"#{r['font_num']}"
        print(f"[{idx:>3}] {disp!r}")
        print(f"       lang   : {r['lang']}  ({r['lang_name']})")
        print(f"       langfe : {r['langfe']}  ({r['langfe_name']})")
        print(f"       font   : {font}  |  charset {r['charset']} "
              f"({r['charset_name']})  |  decode {r['codepage']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
