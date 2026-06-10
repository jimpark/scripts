#!/usr/bin/env python3
"""Fetch and display Unicode character information from unicodeplus.com.

Given a single codepoint, the script fetches its page from unicodeplus.com,
parses the property tables, and prints them grouped into readable sections:

    * name, codepoint, Unicode version, block, and plane
    * bidirectional class, mirroring, and case mappings
    * category, script, and combining class
    * encodings and escape sequences (UTF-8/16/32, HTML, URL, and the forms
      used by CSS, JavaScript, JSON, C/C++, Java, Python, Rust, and Ruby)

The codepoint may be written as U+<hex> (e.g. U+0041) or <hex>h (e.g. 0041h).

Examples:
    unicode-info.py U+0041
    unicode-info.py 1F600h
    unicode-info.py u+00e9

Exit status:
    0   success
    1   network error, or the page could not be parsed
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser

__version__ = "1.0.0"

URL_TEMPLATE = "https://unicodeplus.com/U+{0}"
USER_AGENT = "unicode-info/{0}".format(__version__)
FETCH_TIMEOUT = 10


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_codepoint(arg):
    """Return the hex string (upper-case, zero-padded) from U+<hex> or <hex>h."""
    text = arg.strip()
    match = re.fullmatch(r"[Uu]\+([0-9A-Fa-f]+)", text)
    if not match:
        match = re.fullmatch(r"([0-9A-Fa-f]+)[Hh]", text)
    if not match:
        raise argparse.ArgumentTypeError(
            "unrecognised codepoint '{0}'; use U+<hex> (e.g. U+0041) "
            "or <hex>h (e.g. 0041h)".format(arg)
        )
    return match.group(1).upper().zfill(4)


# ── HTML parser ───────────────────────────────────────────────────────────────

class UnicodePageParser(HTMLParser):
    """Extract character properties from a unicodeplus.com codepoint page."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self._in_th = False
        self._in_td = False
        self._in_h1 = False
        self._in_header_char = False  # <strong class="header-char">
        self._current_key = None
        self.display_char = ""
        self.h1_text = ""
        self.properties = {}
        self._buf = ""

    def _attrs_dict(self, attrs):
        return {key: (value or "") for key, value in attrs}

    def handle_starttag(self, tag, attrs):
        classes = self._attrs_dict(attrs).get("class", "").split()

        if tag == "strong" and "header-char" in classes:
            self._in_header_char = True
            self._buf = ""
        elif tag == "h1":
            self._in_h1 = True
            self._buf = ""
        elif tag == "th":
            self._in_th = True
            self._buf = ""
        elif tag == "td" and self._current_key:
            self._in_td = True
            self._buf = ""

    def handle_endtag(self, tag):
        if tag == "strong" and self._in_header_char:
            self._in_header_char = False
            self.display_char = self._buf.strip()
            self._buf = ""
        elif tag == "h1" and self._in_h1:
            self._in_h1 = False
            self.h1_text = self._buf.strip()
            self._buf = ""
        elif tag == "th" and self._in_th:
            self._in_th = False
            self._current_key = self._buf.strip()
            self._buf = ""
        elif tag == "td" and self._in_td:
            self._in_td = False
            value = self._buf.strip()
            if self._current_key and self._current_key not in self.properties:
                self.properties[self._current_key] = value
            self._current_key = None
            self._buf = ""

    def handle_data(self, data):
        if self._in_header_char or self._in_h1 or self._in_th or self._in_td:
            self._buf += data


# ── Fetch ─────────────────────────────────────────────────────────────────────

class FetchError(Exception):
    """Raised when the page cannot be retrieved."""


def fetch_page(hex_str):
    """Return the HTML of the unicodeplus.com page for the given codepoint."""
    url = URL_TEMPLATE.format(hex_str)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise FetchError("HTTP {0} fetching {1}".format(exc.code, url))
    except urllib.error.URLError as exc:
        raise FetchError("network error fetching {0}: {1}".format(url, exc.reason))
    except TimeoutError:
        raise FetchError("timed out fetching {0}".format(url))


# ── Formatting helpers ────────────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _supports_color():
    """Return True if it's reasonable to emit ANSI colour to stdout."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _b(text):
    return "{0}{1}{2}".format(BOLD, text, RESET) if _supports_color() else text


def _c(text):
    return "{0}{1}{2}".format(CYAN, text, RESET) if _supports_color() else text


def _d(text):
    return "{0}{1}{2}".format(DIM, text, RESET) if _supports_color() else text


def section(title):
    rule = "─" * 60
    print()
    print(_c("  {0}".format(rule)))
    print(_c("  {0}".format(title)))
    print(_c("  {0}".format(rule)))


def row(label, value, width=26):
    if value and value != "-":
        print("  {0} {1}".format(_b(label.ljust(width)), value))


# ── Display ───────────────────────────────────────────────────────────────────

SECTION_GROUPS = {
    "Main Unicode Properties": [
        "Name", "Unicode Codepoint", "Unicode Version", "Block", "Plane",
    ],
    "Bidirectional & Case": [
        "Bidirectional class", "Is mirrored?", "Case",
        "Lowercase character", "Uppercase character", "Titlecase character",
    ],
    "Classification": [
        "Category", "Script", "Combining Class",
    ],
    "Encodings & Escape Sequences": [
        "UTF-8 (hex)", "UTF-16 (hex)", "UTF-32 (hex)",
        "HTML (decimal)", "HTML (hex)", "HTML (named)",
        "URL Escape Code", "CSS", "JavaScript", "JSON",
        "C, C++", "Java", "Python", "Rust", "Ruby",
    ],
}


def display(hex_str, char, h1, props):
    cp = "U+{0}".format(hex_str)

    # ── Header ──────────────────────────────────────────────────────────────
    print()
    name = props.get("Name", h1 or cp)
    print("  {0}  {1}".format(_b(cp), name))
    if char:
        # Try to print the actual character; may not render in all terminals.
        try:
            actual = chr(int(hex_str, 16))
            print("\n  Character : {0}  {1}".format(actual, _d("(rendered)")))
        except (ValueError, OverflowError):
            pass

    # ── Sections ────────────────────────────────────────────────────────────
    shown = set()
    for sec_title, keys in SECTION_GROUPS.items():
        values = {k: props[k] for k in keys if props.get(k) not in (None, "", "-")}
        if not values:
            continue
        section(sec_title)
        for k in keys:
            if k in values:
                row(k, values[k])
                shown.add(k)

    # Any properties not covered by the groups.
    leftover = {
        k: v for k, v in props.items() if k not in shown and v not in ("", "-")
    }
    if leftover:
        section("Other Properties")
        for k, v in leftover.items():
            row(k, v)

    print()
    print(_d("  Source: {0}".format(URL_TEMPLATE.format(hex_str))))
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="unicode-info.py",
        description="Fetch and display Unicode character information from "
                    "unicodeplus.com.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "codepoint formats:\n"
            "  U+<hex>   e.g. U+0041\n"
            "  <hex>h    e.g. 0041h\n"
            "\n"
            "examples:\n"
            "  unicode-info.py U+0041\n"
            "  unicode-info.py 1F600h\n"
            "  unicode-info.py u+00e9\n"
        ),
    )
    parser.add_argument(
        "codepoint",
        type=parse_codepoint,
        help="codepoint to look up, as U+<hex> or <hex>h",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {0}".format(__version__),
    )
    return parser.parse_args(argv)


def _use_utf8_stdout():
    """Emit UTF-8 so box rules and the rendered glyph don't crash on Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    args = parse_args(argv)
    hex_str = args.codepoint
    _use_utf8_stdout()

    try:
        html = fetch_page(hex_str)
    except FetchError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1

    parser = UnicodePageParser()
    parser.feed(html)

    if not parser.properties:
        print(
            "error: could not parse data for U+{0}; check {1} manually".format(
                hex_str, URL_TEMPLATE.format(hex_str)
            ),
            file=sys.stderr,
        )
        return 1

    display(hex_str, parser.display_char, parser.h1_text, parser.properties)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
