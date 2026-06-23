#!/usr/bin/env python3
r"""Find C++ string/char literals that misuse \xNNNN to mean a Unicode code
point and rewrite them as the proper \uNNNN universal-character-name.

Why this matters: in C++ the \x escape is *greedy* -- it consumes every
hex digit that follows it, not just two or four. So "\x4E00" is a single
escape whose value is 0x4E00 (implementation-defined / often truncated in a
narrow string), and "\xABcat" silently swallows the c and a as hex digits.
When the intent was a 16-bit Unicode scalar, the correct spelling is the
fixed-width \uNNNN, which always takes exactly four hex digits.

This tool converts ONLY the unambiguous case: \x followed by exactly four
hex digits that are not followed by a fifth hex digit, e.g.

    "\x4E00"      ->  "一"
    u"\xFEFF;"    ->  u"﻿;"

It deliberately leaves alone:
  * 1-3 digit escapes (\x41, \xAB) -- those are genuine byte values;
  * 5+ digit escapes (\x10FFFF)    -- ambiguous; convert by hand if needed;
  * surrogate values \xD800-\xDFFF -- \u may not name a surrogate, so the
    conversion would turn legal code into ill-formed code;
  * anything outside a string or character literal;
  * comments (// and /* */), raw string literals R"(...)", and the contents
    of escaped backslashes (\\x... is a literal backslash then an x).

It is literal-aware rather than a blind regex, so it will not touch a
\xNNNN that happens to appear in a comment or in a raw string.

By default it edits files in place. Use --dry-run to print a report of the
changes it would make without writing anything.

Examples:
    cpp-unicode-escapes.py src/foo.cpp            # edit one file in place
    cpp-unicode-escapes.py src/ include/          # recurse directories
    cpp-unicode-escapes.py src/ --dry-run         # report only, write nothing
    cpp-unicode-escapes.py . --ext .cpp,.h,.cuh   # custom extension set

Exit status:
    0   success (whether or not anything changed)
    1   one or more files could not be read or written
    2   usage error (bad/missing arguments; handled by argparse)
"""

import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

__version__ = "1.0.0"

DEFAULT_EXTS = {
    ".c", ".cc", ".cpp", ".cxx", ".c++",
    ".h", ".hh", ".hpp", ".hxx", ".h++",
    ".inl", ".ipp", ".tcc", ".cu", ".cuh",
}

IDENT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

# A normal string / character literal start: optional encoding prefix + quote.
_LIT_RE = re.compile(r'(?:u8|u|U|L)?(["\'])')
# A raw string literal start: optional encoding prefix + R + opening quote.
_RAW_RE = re.compile(r'(?:u8|u|U|L)?R"')
# \x followed by EXACTLY four hex digits (not a fifth), inside literal text.
_X4_RE = re.compile(r"\\x([0-9A-Fa-f]{4})(?![0-9A-Fa-f])")
# Cheap byte-level pre-filter: a file can't have a candidate unless it has a
# backslash-x followed by at least four hex digits *somewhere*. This runs in C
# over the raw bytes and lets us skip the expensive char-by-char scan (and the
# decode) for the overwhelming majority of files in a large tree.
_PREFILTER_RE = re.compile(rb"\\x[0-9A-Fa-f]{4}")

# Directories that never hold source we want but can be enormous; pruned so
# os.walk doesn't descend into them.
PRUNE_DIRS = {".git", ".svn", ".hg"}


def _prefix_ok(text, start):
    """A literal's encoding prefix is real only if the character before it is
    not part of an identifier (otherwise we'd be looking at the tail of a
    name like `someValueL` rather than the `L` prefix of a literal)."""
    return start == 0 or text[start - 1] not in IDENT


def _match_raw_start(text, i):
    m = _RAW_RE.match(text, i)
    if not m or not _prefix_ok(text, i):
        return None
    return m


def _match_literal_start(text, i):
    m = _LIT_RE.match(text, i)
    if not m:
        return None
    has_prefix = (m.end() - i) > 1
    if has_prefix and not _prefix_ok(text, i):
        return None
    # Exclude the C++14 digit separator: the ' in 1'000'000 is preceded by a
    # digit. A real character literal's quote is preceded by punctuation.
    if m.group(1) == "'" and not has_prefix:
        if i > 0 and text[i - 1] in IDENT:
            return None
    return m


def convert_source(text):
    r"""Walk C++ source and convert \xNNNN -> \uNNNN inside string and
    character literals only. Returns (new_text, changes) where changes is a
    list of (lineno, old_fragment, new_fragment) tuples."""
    out = []
    changes = []
    i = 0
    n = len(text)
    line = 1  # 1-based line number at the current position i

    def consume(j):
        """Emit text[i:j] verbatim, advance, and keep the line counter."""
        nonlocal i, line
        seg = text[i:j]
        out.append(seg)
        line += seg.count("\n")
        i = j

    while i < n:
        c = text[i]

        # --- line comment ---------------------------------------------------
        if c == "/" and text[i:i + 2] == "//":
            j = text.find("\n", i)
            consume(n if j == -1 else j)
            continue

        # --- block comment --------------------------------------------------
        if c == "/" and text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            consume(n if j == -1 else j + 2)
            continue

        # --- raw string literal: escapes are literal, never converted -------
        rm = _match_raw_start(text, i)
        if rm:
            p = rm.end()                       # just past the opening quote
            d_end = p
            while d_end < n and text[d_end] != "(":
                d_end += 1
            delim = text[p:d_end]
            close = ")" + delim + '"'
            end = text.find(close, d_end + 1)
            consume(n if end == -1 else end + len(close))
            continue

        # --- normal string or character literal -----------------------------
        lm = _match_literal_start(text, i)
        if lm:
            quote = lm.group(1)
            consume(lm.end())                  # emit prefix + opening quote
            while i < n:
                ch = text[i]
                if ch == "\\":
                    xm = _X4_RE.match(text, i)
                    if xm and not (0xD800 <= int(xm.group(1), 16) <= 0xDFFF):
                        new = "\\u" + xm.group(1)
                        out.append(new)
                        changes.append((line, xm.group(0), new))
                        i = xm.end()           # no newline in the match
                        continue
                    # Any other escape -- including a surrogate \xD800-\xDFFF,
                    # which \u may not name (converting it would make legal code
                    # ill-formed) -- is copied verbatim: take the backslash and
                    # the next char together so an escaped quote/backslash can't
                    # end the literal early.
                    consume(min(i + 2, n))
                    continue
                if ch == quote:
                    consume(i + 1)             # closing quote
                    break
                if ch == "\n":
                    # Unterminated literal (shouldn't happen in valid source);
                    # bail back to code scanning to avoid running away.
                    consume(i + 1)
                    break
                consume(i + 1)
            continue

        # --- ordinary code character ----------------------------------------
        consume(i + 1)

    return "".join(out), changes


def process_file(path, dry_run):
    """Read, scan, and (unless dry_run) rewrite one file. Returns None when
    there's nothing to report, else (path, changes, error) where error is an
    error string or None. Safe to call from worker threads: each call touches
    only its own file."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return (path, None, str(exc))

    # Cheap byte-level pre-filter: skip the decode and char-by-char scan unless
    # a candidate could exist. This is what keeps a huge tree affordable.
    if not _PREFILTER_RE.search(raw):
        return None

    # latin-1 round-trips every byte losslessly and preserves line endings;
    # we only ever rewrite ASCII escape sequences.
    new_text, changes = convert_source(raw.decode("latin-1"))
    if not changes:
        return None

    if not dry_run:
        try:
            with open(path, "wb") as fh:
                fh.write(new_text.encode("latin-1"))
        except OSError as exc:
            return (path, changes, str(exc))

    return (path, changes, None)


def iter_target_files(paths, exts):
    """Yield files to process: each path as-is if it's a file, or every file
    under it with a matching extension if it's a directory."""
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
                for name in sorted(files):
                    if os.path.splitext(name)[1].lower() in exts:
                        yield os.path.join(root, name)
        else:
            yield path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="cpp-unicode-escapes.py",
        description=r"Convert misused \xNNNN escapes to \uNNNN in C++ literals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  cpp-unicode-escapes.py src/foo.cpp\n"
            "  cpp-unicode-escapes.py src/ include/\n"
            "  cpp-unicode-escapes.py src/ --dry-run\n"
            "  cpp-unicode-escapes.py . --ext .cpp,.h,.cuh\n"
        ),
    )
    parser.add_argument("paths", nargs="+",
                        help="C++ files or directories to process")
    parser.add_argument("--dry-run", action="store_true",
                        help="report changes without writing any files")
    parser.add_argument("--ext", default=None,
                        help="comma-separated extensions to scan when given a "
                             "directory (default: common C/C++ extensions)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress the per-change lines; show only a summary")
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help="number of worker threads (default: scales with "
                             "CPU count; use 1 to disable parallelism)")
    parser.add_argument("--version", action="version",
                        version="%(prog)s {0}".format(__version__))
    return parser.parse_args(argv)


def _use_utf8_stdout():
    """Emit UTF-8 so non-ASCII file paths don't crash a Windows console."""
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

    if args.ext:
        exts = {("." + e.lstrip(".")).lower() for e in args.ext.split(",") if e.strip()}
    else:
        exts = DEFAULT_EXTS

    # I/O-bound work (reading every file for the pre-filter) dominates, so
    # worker threads overlap the read latency much like ripgrep's pool. We feed
    # the pool from a streaming walk and cap the in-flight futures so memory
    # stays flat no matter how large the tree is.
    jobs = args.jobs if args.jobs and args.jobs > 0 else min(32, (os.cpu_count() or 4) * 4)

    results = []   # (path, changes, error) for files that changed or errored
    if jobs == 1:
        for path in iter_target_files(args.paths, exts):
            r = process_file(path, args.dry_run)
            if r is not None:
                results.append(r)
    else:
        cap = jobs * 4
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            inflight = set()
            for path in iter_target_files(args.paths, exts):
                inflight.add(pool.submit(process_file, path, args.dry_run))
                if len(inflight) >= cap:
                    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    results.extend(f.result() for f in done if f.result() is not None)
            for f in inflight:
                r = f.result()
                if r is not None:
                    results.append(r)

    # Deterministic output regardless of thread completion order.
    results.sort(key=lambda r: r[0])

    total_changes = 0
    files_changed = 0
    had_error = False
    verb = "would change" if args.dry_run else "changed"

    for path, changes, error in results:
        if changes:
            files_changed += 1
            total_changes += len(changes)
            print("{0}: {1} ({2} replacement{3})".format(
                verb, path, len(changes), "" if len(changes) == 1 else "s"))
            if not args.quiet:
                for lineno, old, new in changes:
                    print("    line {0}: {1} -> {2}".format(lineno, old, new))
        if error:
            print("error: {0}: {1}".format(path, error), file=sys.stderr)
            had_error = True

    suffix = " (dry run, no files written)" if args.dry_run else ""
    print("\n{0} replacement{1} across {2} file{3}{4}".format(
        total_changes, "" if total_changes == 1 else "s",
        files_changed, "" if files_changed == 1 else "s", suffix))

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
