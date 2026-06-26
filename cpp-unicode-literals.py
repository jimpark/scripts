#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "tree-sitter>=0.22",
#     "tree-sitter-cpp>=0.23",
# ]
# ///
r"""Classify C++ string literals by encoding type and migrate narrow/wide
literals to their Unicode-typed spellings:

    "..."   ->  u8"...."     (narrow  char[]    ->  char8_t[]  in C++20)
    L"..."  ->  u"...."      (wide    wchar_t[] ->  char16_t[])

and the matching raw-string forms:

    R"(...)"   ->  u8R"(...)"
    LR"(...)"  ->  uR"(...)"

Why a parser and not a regex: identifying a literal correctly means knowing
where literals begin and end, and *not* matching a `"` that lives in a comment,
in the body of a raw string, or after an escaped backslash. This tool parses
the translation unit with tree-sitter's C++ grammar and rewrites only the
opening prefix of real literal nodes, so comments and raw-string contents are
left untouched by construction.

A NOTE ON SOUNDNESS. tree-sitter is a parser, not a type checker: it can tell
you exactly *what kind* of literal each token is, but not how the literal is
*used*. The rewrites above are type-changing, not value-preserving:

  * narrow -> u8 changes char[] to char8_t[] (C++20). Anything that passed the
    literal where a `const char*` is expected will stop compiling.
  * L -> u changes wchar_t[] to char16_t[] -- a different type, and a different
    width on Linux/macOS where wchar_t is 32-bit.

So treat every rewrite as a reviewed suggestion, not a guaranteed-safe edit.
Run --dry-run first, and use --report to inventory every literal by type --
especially the plain narrow ones -- so you can decide which truly belong in
char8_t before you let the tool touch them. Your version-control diff is the
final safety net.

By default it edits files in place. Use --dry-run to preview the rewrites
without writing, or --report to only classify literals (no rewriting at all).

Examples:
    cpp-unicode-literals.py src/foo.cpp           # rewrite one file in place
    cpp-unicode-literals.py src/ include/         # recurse directories
    cpp-unicode-literals.py src/ --dry-run        # preview rewrites, write nothing
    cpp-unicode-literals.py src/ --report         # inventory literals by type
    cpp-unicode-literals.py src/ --report --json  # ... as JSON for scripting

Exit status:
    0   success (whether or not anything changed)
    1   one or more files could not be read or written
    2   usage error (bad/missing arguments; handled by argparse)
"""

import argparse
import json
import os
import sys
import threading
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser

__version__ = "1.0.0"

DEFAULT_EXTS = {
    ".c", ".cc", ".cpp", ".cxx", ".c++",
    ".h", ".hh", ".hpp", ".hxx", ".h++",
    ".inl", ".ipp", ".tcc", ".cu", ".cuh",
}

# Directories that never hold source we want but can be enormous; pruned so
# os.walk doesn't descend into them.
PRUNE_DIRS = {".git", ".svn", ".hg"}

# The literal-prefix rewrites. Key is the encoding prefix as it appears before
# the opening quote (empty string for a plain narrow literal); value is the
# prefix it becomes. A prefix not listed here is left exactly as written.
CONVERT = {
    "":   "u8",    # "..."  -> u8"..."
    "L":  "u",     # L"..." ->  u"..."
    "R":  "u8R",   # R"()"  -> u8R"()"
    "LR": "uR",    # LR"()" ->  uR"()"
}

# Every literal kind we classify, in report order, with a human label.
KIND_ORDER = ["", "L", "u8", "u", "U", "R", "LR", "u8R", "uR", "UR"]
KIND_LABEL = {
    "":    'narrow    "..."     (char,    execution charset)',
    "L":   'wide      L"..."    (wchar_t)',
    "u8":  'utf-8     u8"..."   (char8_t in C++20)',
    "u":   'utf-16    u"..."    (char16_t)',
    "U":   'utf-32    U"..."    (char32_t)',
    "R":   'raw       R"(...)"  (narrow)',
    "LR":  'raw wide  LR"(...)" (wchar_t)',
    "u8R": 'raw utf-8 u8R"(...)"',
    "uR":  'raw utf-16 uR"(...)"',
    "UR":  'raw utf-32 UR"(...)"',
}

# These node types are the literals we care about; char_literal is deliberately
# excluded (character-literal intent is murkier and out of scope).
_LITERAL_TYPES = {"string_literal", "raw_string_literal"}

# The C++ language is immutable and safe to share across threads; a Parser is
# not, so each worker thread lazily builds its own (kept in thread-local state).
_LANG = Language(tscpp.language())
_local = threading.local()

# What process_file hands back: counts is always populated; changes lists the
# rewrites (empty in --report mode); inventory lists every literal (only built
# in --report mode, since it can be large and is unused otherwise).
Result = namedtuple("Result", "path error changes counts inventory")


def _parser():
    p = getattr(_local, "parser", None)
    if p is None:
        try:
            p = Parser(_LANG)            # tree-sitter >= 0.22
        except TypeError:                # older API
            p = Parser()
            p.set_language(_LANG)
        _local.parser = p
    return p


def _snippet(raw, maxlen=60):
    """A one-line, display-safe rendering of a literal's bytes."""
    s = raw.decode("utf-8", "replace")
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if len(s) > maxlen:
        s = s[:maxlen - 1] + "…"
    return s


def _iter_literals(root):
    """Yield every string_literal / raw_string_literal node in the tree."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _LITERAL_TYPES:
            yield node
        stack.extend(node.children)


def scan_source(raw, want_inventory):
    """Parse C++ source bytes, classify every string literal, and compute the
    prefix rewrites. Returns (new_bytes_or_None, changes, counts, inventory).

    new_bytes is None when nothing was rewritten. changes is a list of
    (lineno, old_prefix, new_prefix, snippet); counts is a Counter of
    prefix -> n; inventory (only when want_inventory) is a list of
    (lineno, prefix, snippet)."""
    tree = _parser().parse(raw)
    counts = Counter()
    changes = []
    inventory = []
    edits = []   # (start_byte, end_byte, replacement_bytes) for the prefix token

    for node in _iter_literals(node_root := tree.root_node):
        if not node.children:
            continue
        delim = node.children[0]                       # the opening prefix+quote
        prefix = raw[delim.start_byte:delim.end_byte - 1].decode("ascii", "replace")
        counts[prefix] += 1
        lineno = node.start_point[0] + 1

        if want_inventory:
            inventory.append((lineno, prefix, _snippet(raw[node.start_byte:node.end_byte])))

        new_prefix = CONVERT.get(prefix)
        if new_prefix is not None:
            snip = _snippet(raw[node.start_byte:node.end_byte])
            changes.append((lineno, prefix, new_prefix, snip))
            edits.append((delim.start_byte, delim.end_byte,
                          (new_prefix + '"').encode("ascii")))

    if not edits:
        return None, changes, counts, inventory

    # Splice the new prefixes in from the back so earlier byte offsets stay
    # valid. Only ASCII prefix tokens change; every other byte is preserved.
    buf = bytearray(raw)
    for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        buf[start:end] = repl
    return bytes(buf), changes, counts, inventory


def process_file(path, mode):
    """Read, scan, and (in convert mode) rewrite one file. Always returns a
    Result. Safe to call from worker threads: each call touches only its own
    file and its own thread-local parser."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return Result(path, str(exc), [], Counter(), [])

    # Cheap pre-filter: no double-quote byte means no string literal to find.
    if b'"' not in raw:
        return Result(path, None, [], Counter(), [])

    new_raw, changes, counts, inventory = scan_source(raw, want_inventory=(mode == "report"))

    if mode == "convert" and new_raw is not None:
        try:
            with open(path, "wb") as fh:
                fh.write(new_raw)
        except OSError as exc:
            return Result(path, str(exc), changes, counts, inventory)

    return Result(path, None, changes, counts, inventory)


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


def run(paths, exts, mode, jobs):
    """Process every target file, aggregating counts across all of them and
    keeping only the files worth printing (a rewrite, an inventory, or an
    error). Returns (printable_results, total_counts)."""
    printable = []
    totals = Counter()

    def handle(r):
        totals.update(r.counts)
        if r.error or r.changes or r.inventory:
            printable.append(r)

    if jobs == 1:
        for path in iter_target_files(paths, exts):
            handle(process_file(path, mode))
    else:
        cap = jobs * 4
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            inflight = set()
            for path in iter_target_files(paths, exts):
                inflight.add(pool.submit(process_file, path, mode))
                if len(inflight) >= cap:
                    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    for f in done:
                        handle(f.result())
            for f in inflight:
                handle(f.result())

    printable.sort(key=lambda r: r.path)
    return printable, totals


def print_breakdown(totals):
    """Print the per-type tally that classifies every literal seen."""
    present = [k for k in KIND_ORDER if totals.get(k)]
    present += sorted(k for k in totals if k not in KIND_LABEL)  # unknown prefixes
    if not present:
        print("no string literals found")
        return
    width = max(len(KIND_LABEL.get(k, repr(k))) for k in present)
    print("literals by type:")
    for k in present:
        label = KIND_LABEL.get(k, "{0!r} (unrecognised prefix)".format(k))
        print("  {0:<{1}}  {2}".format(label, width, totals[k]))


def report_human(printable, totals, quiet):
    """--report human output: every literal grouped by encoding type."""
    by_kind = {}
    for r in printable:
        for lineno, prefix, snip in r.inventory:
            by_kind.setdefault(prefix, []).append((r.path, lineno, snip))

    order = [k for k in KIND_ORDER if k in by_kind]
    order += sorted(k for k in by_kind if k not in KIND_LABEL)
    for prefix in order:
        items = by_kind[prefix]
        label = KIND_LABEL.get(prefix, "{0!r} (unrecognised prefix)".format(prefix))
        print("\n=== {0} === {1} literal{2}".format(
            label, len(items), "" if len(items) == 1 else "s"))
        if not quiet:
            for path, lineno, snip in items:
                print("  {0}:{1}: {2}".format(path, lineno, snip))

    print()
    print_breakdown(totals)
    convertible = sum(totals.get(k, 0) for k in CONVERT)
    print("\n{0} literal(s) are narrow/wide candidates for u8/u "
          "(review before converting).".format(convertible))


def report_json(printable, totals):
    """--report JSON output: one record per literal, plus the tally."""
    literals = []
    for r in printable:
        for lineno, prefix, snip in r.inventory:
            literals.append({
                "file": r.path,
                "line": lineno,
                "prefix": prefix,
                "kind": KIND_LABEL.get(prefix, "unrecognised").split("  ")[0].strip(),
                "convertible": prefix in CONVERT,
                "text": snip,
            })
    out = {
        "literals": literals,
        "counts_by_prefix": dict(totals),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def print_conversions(printable, totals, dry_run, quiet):
    """Default / --dry-run output: the rewrites, then the type breakdown."""
    verb = "would change" if dry_run else "changed"
    total_changes = 0
    files_changed = 0
    had_error = False

    for r in printable:
        if r.changes:
            files_changed += 1
            total_changes += len(r.changes)
            print("{0}: {1} ({2} literal{3})".format(
                verb, r.path, len(r.changes), "" if len(r.changes) == 1 else "s"))
            if not quiet:
                for lineno, prefix, new_prefix, snip in r.changes:
                    new_snip = new_prefix + snip[len(prefix):]
                    print("    line {0}: {1}  ->  {2}".format(lineno, snip, new_snip))
        if r.error:
            print("error: {0}: {1}".format(r.path, r.error), file=sys.stderr)
            had_error = True

    print()
    print_breakdown(totals)
    suffix = " (dry run, no files written)" if dry_run else ""
    print("\n{0} rewrite{1} across {2} file{3}{4}".format(
        total_changes, "" if total_changes == 1 else "s",
        files_changed, "" if files_changed == 1 else "s", suffix))
    return had_error


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="cpp-unicode-literals.py",
        description="Classify C++ string literals by type and migrate narrow/wide "
                    "literals to u8/u (Unicode-typed) spellings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  cpp-unicode-literals.py src/foo.cpp\n"
            "  cpp-unicode-literals.py src/ include/\n"
            "  cpp-unicode-literals.py src/ --dry-run\n"
            "  cpp-unicode-literals.py src/ --report\n"
            "  cpp-unicode-literals.py src/ --report --json\n"
        ),
    )
    parser.add_argument("paths", nargs="+",
                        help="C++ files or directories to process")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview the rewrites without writing any files")
    parser.add_argument("--report", action="store_true",
                        help="don't rewrite; inventory every literal by type")
    parser.add_argument("--json", action="store_true",
                        help="with --report, emit JSON instead of a human listing")
    parser.add_argument("--ext", default=None,
                        help="comma-separated extensions to scan when given a "
                             "directory (default: common C/C++ extensions)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress the per-literal lines; show only summaries")
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help="number of worker threads (default: scales with CPU "
                             "count; use 1 to disable parallelism)")
    parser.add_argument("--version", action="version",
                        version="%(prog)s {0}".format(__version__))
    return parser.parse_args(argv)


def _use_utf8_stdout():
    """Emit UTF-8 so non-ASCII file paths/snippets don't crash a Windows console."""
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

    if args.json and not args.report:
        print("error: --json only applies with --report", file=sys.stderr)
        return 2

    if args.ext:
        exts = {("." + e.lstrip(".")).lower() for e in args.ext.split(",") if e.strip()}
    else:
        exts = DEFAULT_EXTS

    mode = "report" if args.report else ("dry-run" if args.dry_run else "convert")
    jobs = args.jobs if args.jobs and args.jobs > 0 else min(32, (os.cpu_count() or 4) * 4)

    printable, totals = run(args.paths, exts, mode, jobs)

    had_error = any(r.error for r in printable)

    if mode == "report":
        if args.json:
            report_json(printable, totals)
        else:
            report_human(printable, totals, args.quiet)
    else:
        had_error = print_conversions(printable, totals, args.dry_run, args.quiet) or had_error

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
