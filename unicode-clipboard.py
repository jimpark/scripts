#!/usr/bin/env python3
"""Copy one or more Unicode characters to the system clipboard by codepoint.

Give the script one or more codepoints and it builds the corresponding string
and places it on the clipboard, so you can paste characters that are otherwise
impossible to type into any program that pastes from the clipboard.

Each codepoint may be written in any of these forms (case-insensitive):

    U+<hex>     e.g. U+1F600          \\u<hex>     e.g. \\u00e9 (4 hex digits)
    <hex>h      e.g. 1F600h           \\U<hex>     e.g. \\U0001F600 (8 hex digits)
    0x<hex>     e.g. 0x1F600          &#<dec>;    e.g. &#233;  (HTML decimal)
                                      &#x<hex>;   e.g. &#xE9;  (HTML hex)

Multiple codepoints are concatenated, in order, into a single string. So

    unicode-clipboard.py U+0048 U+0069       copies "Hi"

With --string/-s you instead pass a whole string whose embedded escapes are
decoded like a C/C++ string literal, then copied:

    unicode-clipboard.py -s "Hello\\u002c World\\u0021"   copies "Hello, World!"

Recognised escapes are the Unicode escapes \\uXXXX (exactly 4 hex digits) and
\\UXXXXXXXX (exactly 8 hex digits), \\N{NAME} (by Unicode name, e.g.
\\N{BULLET}), \\xH... (hex), \\ooo (1-3 octal digits), and the single-character
escapes \\a \\b \\f \\n \\r \\t \\v \\\\ \\' \\" \\?. Any other escape is an
error. Everything that isn't an escape is taken literally.

If the value is missing in either mode — no codepoints, or -s with no string —
it is read from stdin (codepoint mode splits stdin on whitespace; string mode
reads it whole, minus a trailing newline).

Clipboard backends (no third-party dependencies):

    * macOS    pbcopy
    * Windows  the Win32 clipboard API via ctypes (full Unicode; unlike the
               built-in clip.exe, which mangles non-ASCII to the code page)
    * Linux    wl-copy (Wayland), or xclip / xsel (X11) — whichever is found

Exit status:
    0   success
    1   no usable clipboard backend, or the copy failed
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import os
import re
import subprocess
import sys
import unicodedata

__version__ = "1.0.0"

MAX_CODEPOINT = 0x10FFFF
SURROGATE_RANGE = (0xD800, 0xDFFF)


# ── Codepoint parsing ───────────────────────────────────────────────────────

# Each pattern captures the digits; the second element is the numeric base.
_CODEPOINT_FORMS = [
    (re.compile(r"[Uu]\+([0-9A-Fa-f]+)"), 16),       # U+1F600
    (re.compile(r"([0-9A-Fa-f]+)[Hh]"), 16),         # 1F600h
    (re.compile(r"0[xX]([0-9A-Fa-f]+)"), 16),        # 0x1F600
    (re.compile(r"\\[uU]([0-9A-Fa-f]+)"), 16),       # é / \U0001F600
    (re.compile(r"&#[xX]([0-9A-Fa-f]+);?"), 16),     # &#xE9;
    (re.compile(r"&#([0-9]+);?"), 10),               # &#233;
]


def codepoint_fault(value):
    """Return a reason string if `value` isn't a valid character, else None."""
    if value > MAX_CODEPOINT:
        return "U+{0:X} is out of range (max U+10FFFF)".format(value)
    if SURROGATE_RANGE[0] <= value <= SURROGATE_RANGE[1]:
        return "U+{0:04X} is a UTF-16 surrogate and is not a valid character".format(value)
    return None


def parse_codepoint(arg):
    """Return the int codepoint for a single token, or raise on bad input."""
    text = arg.strip()
    value = None
    for pattern, base in _CODEPOINT_FORMS:
        match = pattern.fullmatch(text)
        if match:
            value = int(match.group(1), base)
            break
    if value is None:
        raise argparse.ArgumentTypeError(
            "unrecognised codepoint '{0}'; use U+<hex>, <hex>h, 0x<hex>, "
            "\\u<hex>, &#<dec>; or &#x<hex>;".format(arg)
        )
    fault = codepoint_fault(value)
    if fault:
        raise argparse.ArgumentTypeError("codepoint '{0}': {1}".format(arg, fault))
    return value


# ── String-literal escape decoding (--string mode) ──────────────────────────

class EscapeError(Exception):
    """Raised when an embedded escape sequence is malformed."""


_SIMPLE_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
    "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}
_HEX_DIGITS = "0123456789abcdefABCDEF"
_OCTAL_DIGITS = "01234567"


def _decode_unicode_escape(text, start, letter):
    """Decode \\uXXXX / \\UXXXXXXXX at text[start:]; return (char, next_index)."""
    width = 4 if letter == "u" else 8
    digits = text[start:start + width]
    if len(digits) < width or any(c not in _HEX_DIGITS for c in digits):
        raise EscapeError(
            "\\{0} needs exactly {1} hex digits, got '{2}'".format(
                letter, width, text[start:start + width]
            )
        )
    return _checked_char(int(digits, 16), "\\" + letter + digits), start + width


def _checked_char(value, source):
    """Return chr(value), or raise EscapeError naming `source` if invalid."""
    fault = codepoint_fault(value)
    if fault:
        raise EscapeError("escape '{0}': {1}".format(source, fault))
    return chr(value)


def decode_escapes(text):
    """Decode C/C++-style escapes in `text`, returning the resulting string."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            raise EscapeError("dangling backslash at end of string")
        esc = text[i]
        if esc in ("u", "U"):
            char, i = _decode_unicode_escape(text, i + 1, esc)
            out.append(char)
        elif esc == "N":
            if i + 1 >= n or text[i + 1] != "{":
                raise EscapeError(
                    "\\N must be followed by {NAME}, e.g. \\N{BULLET}"
                )
            end = text.find("}", i + 2)
            if end == -1:
                raise EscapeError("unterminated \\N{...} escape")
            name = text[i + 2:end]
            try:
                out.append(unicodedata.lookup(name))
            except KeyError:
                raise EscapeError(
                    "unknown character name in \\N{{{0}}}".format(name)
                )
            i = end + 1
        elif esc == "x":
            j = i + 1
            while j < n and text[j] in _HEX_DIGITS:
                j += 1
            if j == i + 1:
                raise EscapeError("\\x needs at least one hex digit")
            out.append(_checked_char(int(text[i + 1:j], 16), text[i - 1:j]))
            i = j
        elif esc in _OCTAL_DIGITS:
            j = i
            while j < n and j < i + 3 and text[j] in _OCTAL_DIGITS:
                j += 1
            out.append(_checked_char(int(text[i:j], 8), "\\" + text[i:j]))
            i = j
        elif esc in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[esc])
            i += 1
        else:
            raise EscapeError("unknown escape '\\{0}'".format(esc))
    return "".join(out)


# ── Clipboard backends ──────────────────────────────────────────────────────

class ClipboardError(Exception):
    """Raised when the text could not be placed on the clipboard."""


def _copy_macos(text):
    _run_pipe(["pbcopy"], text)


def _copy_windows(text):
    """Place text on the Windows clipboard as CF_UNICODETEXT via ctypes."""
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    # UTF-16-LE bytes plus a terminating NUL wide char.
    data = text.encode("utf-16-le") + b"\x00\x00"

    if not user32.OpenClipboard(None):
        raise ClipboardError("could not open the Windows clipboard")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("could not allocate clipboard memory")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise ClipboardError("could not lock clipboard memory")
        try:
            ctypes.memmove(locked, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise ClipboardError("SetClipboardData failed")
        # On success the system owns `handle`; don't free it.
    finally:
        user32.CloseClipboard()


def _copy_linux(text):
    """Try the common Linux clipboard tools, in order of preference."""
    import shutil

    candidates = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]
    for command in candidates:
        if shutil.which(command[0]):
            _run_pipe(command, text)
            return
    raise ClipboardError(
        "no clipboard tool found; install one of: wl-copy (Wayland), "
        "xclip, or xsel (X11)"
    )


def _run_pipe(command, text):
    """Feed `text` (UTF-8) to `command` on stdin; raise on failure."""
    try:
        proc = subprocess.run(
            command,
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        raise ClipboardError("clipboard tool not found: {0}".format(command[0]))
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise ClipboardError(
            "{0} failed{1}".format(command[0], ": " + detail if detail else "")
        )


def copy_to_clipboard(text):
    """Dispatch to the right backend for this platform."""
    if sys.platform == "darwin":
        _copy_macos(text)
    elif os.name == "nt":
        _copy_windows(text)
    else:
        _copy_linux(text)


# ── Entry point ─────────────────────────────────────────────────────────────

# Marker for "-s given with no value" — read the string from stdin.
_STDIN = object()


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="unicode-clipboard.py",
        description="Copy one or more Unicode characters to the system "
                    "clipboard by codepoint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "codepoint formats (case-insensitive):\n"
            "  U+<hex>     e.g. U+1F600\n"
            "  <hex>h      e.g. 1F600h\n"
            "  0x<hex>     e.g. 0x1F600\n"
            "  \\u<hex>     e.g. \\u00e9   (4 hex digits)\n"
            "  \\U<hex>     e.g. \\U0001F600 (8 hex digits)\n"
            "  &#<dec>;    e.g. &#233;   (HTML decimal entity)\n"
            "  &#x<hex>;   e.g. &#xE9;   (HTML hex entity)\n"
            "\n"
            "--string escapes (decoded like a Python/C++ string literal):\n"
            "  \\uXXXX      4 hex digits      \\xH...      hex (1+ digits)\n"
            "  \\UXXXXXXXX  8 hex digits      \\ooo        octal (1-3 digits)\n"
            "  \\N{NAME}    by Unicode name (e.g. \\N{BULLET}, \\N{SNOWMAN})\n"
            "  \\a \\b \\f \\n \\r \\t \\v \\\\ \\' \\\" \\?   single-character escapes\n"
            "\n"
            "examples:\n"
            "  unicode-clipboard.py U+1F600          # copy a grinning face\n"
            "  unicode-clipboard.py U+0048 U+0069    # copy \"Hi\"\n"
            "  unicode-clipboard.py 00e9h            # copy e-acute\n"
            "  echo 'U+2603 U+FE0F' | unicode-clipboard.py   # codepoints via stdin\n"
            "  unicode-clipboard.py -s 'Hello\\u002c World\\u0021'  # copy \"Hello, World!\"\n"
            "  echo 'caf\\u00e9' | unicode-clipboard.py -s          # string via stdin\n"
        ),
    )
    parser.add_argument(
        "codepoints",
        nargs="*",
        metavar="CODEPOINT",
        help="one or more codepoints; if omitted, read from stdin",
    )
    parser.add_argument(
        "-s", "--string",
        nargs="?",
        const=_STDIN,
        default=None,
        metavar="STRING",
        help="copy STRING, decoding embedded C/C++-style escapes "
             "(\\uXXXX, \\n, ...); with no STRING, read it from stdin",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="don't print the confirmation summary",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {0}".format(__version__),
    )
    return parser.parse_args(argv)


def _use_utf8_stdout():
    """Emit UTF-8 so the rendered glyph doesn't crash on legacy consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _resolve_text(args):
    """Return the string to copy, or raise EscapeError/ValueError on bad input.

    ValueError carries an exit code in its first arg via the (msg, code) tuple
    convention used below; the caller maps it to a message and return code.
    """
    if args.string is not None:
        if args.codepoints:
            raise ValueError(("--string cannot be combined with codepoint "
                              "arguments", 2))
        if args.string is _STDIN:
            # Drop the trailing newline the shell/echo adds; keep inner ones.
            raw = sys.stdin.read()
            if raw.endswith("\n"):
                raw = raw[:-1]
            if raw.endswith("\r"):
                raw = raw[:-1]
        else:
            raw = args.string
        return decode_escapes(raw)

    tokens = args.codepoints
    if not tokens:
        tokens = sys.stdin.read().split()
    if not tokens:
        raise ValueError(("no codepoints given", 2))
    return "".join(chr(parse_codepoint(token)) for token in tokens)


def main(argv=None):
    args = parse_args(argv)
    _use_utf8_stdout()

    try:
        text = _resolve_text(args)
    except (EscapeError, argparse.ArgumentTypeError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        message, code = exc.args[0]
        print("error: {0}".format(message), file=sys.stderr)
        return code

    try:
        copy_to_clipboard(text)
    except ClipboardError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1

    if not args.quiet:
        count = len(text)
        plural = "character" if count == 1 else "characters"
        print("Copied {0} {1} to the clipboard:\n".format(count, plural))
        print("  {0}\n".format(text.replace("\n", "\n  ")))
        # The codepoint listing is handy for short strings but noise for long
        # ones, so only show it up to a modest length.
        if count <= 32:
            joined = "  ".join("U+{0:04X}".format(ord(c)) for c in text)
            print("  {0}".format(joined))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
