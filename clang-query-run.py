#!/usr/bin/env python3
"""Run a clang-query AST matcher across every translation unit in a CMake/Bear
project and aggregate the results into an AI-ready investigation packet.

This is the reusable scaffolding behind a query: it auto-detects
compile_commands.json, fans the query out over all TUs in parallel, detects the
"compiled-but-failed-to-parse" case that clang-query hides behind exit 0, and
(for location-style queries) attaches source context and can emit JSON or a
Markdown report. The query itself is *not* hardcoded -- pick one from the
library, point at a .query file, or pass one inline.

Choosing a query:
  (no args)        pick from the library (scripts/clang-queries/*.query) via menu
  -f FILE          run a .query file
  -q 'QUERY...'    run an inline query string
  --list           list library queries and exit

Requires compile_commands.json (CMake: -DCMAKE_EXPORT_COMPILE_COMMANDS=ON, or
`bear -- make`). It's auto-detected: first by probing common build-dir names
(build, out, cmake-build-*, ...) up from the cwd to the repo root, then by a
bounded recursive scan down from the root that catches DBs buried in deep build
trees (e.g. .build/core/darwin/arm64/release/). The newest is chosen if several
exist. Pass -p to point at a build dir or the file directly to override.

macOS: clang-query must use headers matching the compiler in the DB. A DB built
by Apple clang++ usually omits -isysroot (Apple clang has a baked-in SDK), so
homebrew clang-query can't find <stdio.h>/<filesystem> and every TU fails. This
tool fixes that automatically: on macOS it probes the SDK (`xcrun
--show-sdk-path`) and clang-query's own resource dir and adds the matching
-isysroot/-resource-dir for you. Disable with --no-auto-sdk; override by passing
your own --extra-arg (auto-injection backs off if you already gave the flag).

Output modes:
  (default)   one file:line:col per hit (location queries) / raw clang-query
              output (dump/print/detailed-ast queries)
  --json      structured records with source context (location queries only)
  --report    Markdown investigation packet: a rubric followed by every match in
              context. Uses <query-stem>.rubric.md if present, else a generic
              header (location queries only).

Examples:
  ./clang-query-run.py                       # menu, then run on auto-detected DB
  ./clang-query-run.py -f clang-queries/path-string.query --report > out.md
  ./clang-query-run.py -q 'match cxxThrowExpr().bind("t")' -p out
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

QUERY_LIB = Path(__file__).with_name("clang-queries")
DIAG_RE = re.compile(r'^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):.*binds here')
# clang-query exits 0 even when a TU fails to compile, so we detect failures
# from the diagnostics themselves: "file:line:col: error:" / "fatal error:",
# or a bare "clang: error:".
ERROR_RE = re.compile(r': (?:fatal )?error:')
# `set output <mode>` lines that mean "this query dumps AST text, not source
# locations" -- those we stream raw instead of parsing into findings.
RAW_OUTPUT_RE = re.compile(r'^\s*(?:set|enable)\s+output\s+(dump|print|detailed-ast)\b',
                           re.MULTILINE)
# Library-query header metadata: "# title: ..." / "# description: ...".
META_RE = re.compile(r'^#\s*(title|description)\s*:\s*(.*)$', re.IGNORECASE)

DB_NAME = "compile_commands.json"
# Common spots a compile_commands.json lands, relative to a project root, in
# rough order of likelihood. "." covers a DB sitting at the root itself.
COMMON_BUILD_DIRS = (".", "build", "out", "_build", "cmake-build-debug",
                     "cmake-build-release", "Debug", "Release")
# Dirs never worth descending into during the recursive fallback. Note .build
# and other dotted build trees are deliberately NOT here -- the DB often lives
# deep inside one (e.g. .build/core/darwin/arm64/release/).
PRUNE_DIRS = {".git", ".hg", ".svn", "node_modules", ".cache", ".venv", "venv",
              "Pods", "Carthage", "DerivedData"}
RECURSE_MAX_DEPTH = 12   # how deep below the project root to scan for the DB

# Used when --report runs a query that has no <stem>.rubric.md sidecar.
GENERIC_RUBRIC = """\
# clang-query findings — investigation packet

Each finding below is a source location matched by the query, shown in context.

## Your task
For every finding, explain what it is, whether it warrants action, and what that
action should be. Note your confidence.

---
"""


def run_one(clang_query, build_dir, query_file, source, extra_args):
    """Run the query against one TU. Returns (hits, raw_stdout, err).

    hits is the parsed (file,line,col) list for diag-style queries; raw_stdout
    is the verbatim output (used by raw-mode queries). Both are always computed
    so the caller decides which to use based on the query's output mode.
    """
    cmd = [clang_query, "-p", build_dir, "-f", str(query_file), source]
    for a in extra_args:
        cmd += ["--extra-arg", a]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    hits = []
    for ln in proc.stdout.splitlines():
        m = DIAG_RE.match(ln.strip())
        if m:
            hits.append((m["file"], int(m["line"]), int(m["col"])))
    # A TU that fails to parse still exits 0, so don't trust the return code:
    # flag it whenever clang emits an error diagnostic. A partial failure can
    # produce some hits and still miss others, so this is independent of `hits`.
    failed = ERROR_RE.search(proc.stderr) or proc.returncode != 0
    err = proc.stderr.strip() if failed else ""
    return hits, proc.stdout, err


# Cache file contents so repeated lookups (header hit via many TUs) are cheap.
_file_cache = {}


def lines_of(path):
    if path not in _file_cache:
        try:
            _file_cache[path] = Path(path).read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            _file_cache[path] = []
    return _file_cache[path]


def context_snippet(file, line, context):
    """Return the source around `line` with the hit line marked."""
    src = lines_of(file)
    lo = max(1, line - context)
    hi = min(len(src), line + context)
    out = []
    for n in range(lo, hi + 1):
        mark = ">>" if n == line else "  "
        out.append(f"{mark} {n:>5} | {src[n - 1]}")
    return "\n".join(out)


def project_root():
    """Nearest ancestor holding a .git, else the cwd."""
    for a in [Path.cwd(), *Path.cwd().parents]:
        if (a / ".git").exists():
            return a
    return Path.cwd()


def recursive_db_search(root):
    """Find every compile_commands.json under root, newest first.

    Bounded by RECURSE_MAX_DEPTH and PRUNE_DIRS so it doesn't crawl a whole
    monorepo. Once a DB is found in a directory we stop descending into it --
    the object-file tree lives below the DB, never another DB worth preferring.
    """
    found, base_depth = [], len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if DB_NAME in filenames:
            found.append(Path(dirpath) / DB_NAME)
            dirnames[:] = []
            continue
        if len(Path(dirpath).parts) - base_depth >= RECURSE_MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def _capture(cmd):
    """Run cmd, return stripped stdout, or None if it can't run / is empty."""
    try:
        out = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL).stdout.strip()
        return out or None
    except OSError:
        return None


def macos_sdk_args(clang_query, existing):
    """On macOS, return extra clang args so a homebrew/upstream clang-query can
    find the headers Apple's compiler finds implicitly.

    A compile DB produced by Apple clang++ rarely spells out -isysroot (Apple
    clang has a default SDK baked in), so when homebrew clang-query replays the
    command it can't locate <stdio.h>/<filesystem> and every TU fails to parse.
    We supply two things Apple clang gets for free:
      -isysroot <SDK>        macOS SDK -> system + libc++ headers
      -resource-dir <dir>    builtin headers matching clang-query's OWN clang
                             version (derived from the sibling `clang`, so it
                             always matches the tool, never Apple's)
    Skipped if not on macOS, if the user already passed the flag, or if the
    underlying tool (xcrun / clang) isn't available.
    """
    if sys.platform != "darwin":
        return []
    joined = " ".join(existing)
    extra = []
    if "-isysroot" not in joined:
        sdk = _capture(["xcrun", "--show-sdk-path"])
        if sdk:
            extra += ["-isysroot", sdk]
    if "-resource-dir" not in joined:
        sib = Path(clang_query).with_name("clang")
        clang_bin = str(sib) if sib.exists() else "clang"
        rd = _capture([clang_bin, "-print-resource-dir"])
        if rd:
            extra += ["-resource-dir", rd]
    return extra


def find_compile_db(arg):
    """Resolve the directory holding a compile_commands.json.

    With an explicit arg, accept either that directory or a direct path to the
    JSON file. Without one: first probe common build-dir names while walking up
    from the cwd (cheap, predictable), then fall back to a bounded recursive
    scan down from the repo root -- which catches DBs buried in deep, oddly
    named build trees like .build/core/darwin/arm64/release/.

    Returns (db_dir, db_path, others) on success, where `others` lists any
    further candidate DBs not chosen; or (None, None, tried) on failure.
    """
    if arg is not None:
        p = Path(arg)
        if p.is_file():                       # pointed straight at the JSON
            return (p.parent, p, []) if p.name == DB_NAME else (None, None, [str(p)])
        cand = p / DB_NAME                     # pointed at the build dir
        return (p, cand, []) if cand.exists() else (None, None, [str(cand)])

    tried = []
    for ancestor in [Path.cwd(), *Path.cwd().parents]:
        for name in COMMON_BUILD_DIRS:
            cand = ancestor / name / DB_NAME
            tried.append(str(cand))
            if cand.exists():
                return cand.parent, cand, []
        if (ancestor / ".git").exists():       # don't climb past the repo root
            break

    root = project_root()
    found = recursive_db_search(root)
    if found:
        best = found[0]
        return best.parent, best, [str(p) for p in found[1:]]
    return None, None, tried + [f"(recursive scan under {root}/)"]


def query_meta(path):
    """Parse '# title:' / '# description:' header comments from a .query file.
    Title defaults to the filename stem; description to ''."""
    title, desc = path.stem, ""
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if s and not s.startswith("#"):    # header ends at first real line
                break
            m = META_RE.match(s)
            if m:
                key, val = m.group(1).lower(), m.group(2).strip()
                if key == "title":
                    title = val or title
                else:
                    desc = val
    except OSError:
        pass
    return title, desc


def library_queries():
    """Sorted list of (path, title, description) for the query library."""
    if not QUERY_LIB.is_dir():
        return []
    out = []
    for p in sorted(QUERY_LIB.glob("*.query")):
        title, desc = query_meta(p)
        out.append((p, title, desc))
    return out


def pick_from_menu():
    """Interactive picker over the library. Returns a Path or exits."""
    lib = library_queries()
    if not lib:
        sys.exit(f"error: no queries in {QUERY_LIB}/ and no -f/-q given. "
                 f"Add a .query file there or pass -f FILE / -q 'QUERY'.")
    if not sys.stdin.isatty():
        sys.exit("error: no query given (-f/-q) and stdin isn't a TTY for the "
                 "menu. Pass -f FILE, -q 'QUERY', or run --list to see names.")
    print("Available queries:", file=sys.stderr)
    for i, (p, title, desc) in enumerate(lib, 1):
        print(f"  {i:>2}. {title}  ({p.name})", file=sys.stderr)
        if desc:
            print(f"      {desc}", file=sys.stderr)
    while True:
        try:
            raw = input(f"Select 1-{len(lib)} (q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(130)
        if raw.lower() in ("q", "quit", ""):
            sys.exit(0)
        if raw.isdigit() and 1 <= int(raw) <= len(lib):
            return lib[int(raw) - 1][0]
        print("  not a valid choice", file=sys.stderr)


def resolve_query(args):
    """Resolve the query source into (query_file, cleanup, rubric_path).

    rubric_path is the <stem>.rubric.md sibling if it exists, else None. cleanup
    is a no-arg callable to remove any temp file we created for an inline query.
    """
    if args.query_file and args.inline:
        sys.exit("error: pass only one of -f/--query-file and -q/--query.")
    if args.inline:
        fd, name = tempfile.mkstemp(suffix=".query", prefix="clang-query-")
        with os.fdopen(fd, "w") as fh:
            fh.write(args.inline if args.inline.endswith("\n")
                     else args.inline + "\n")
        return Path(name), (lambda: os.unlink(name)), None
    path = Path(args.query_file) if args.query_file else pick_from_menu()
    if not path.exists():
        sys.exit(f"error: query file {path} not found")
    rubric = path.with_name(path.stem + ".rubric.md")
    return path, (lambda: None), (rubric if rubric.exists() else None)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--query-file", default=None,
                    help="run a .query file")
    ap.add_argument("-q", "--query", dest="inline", default=None,
                    help="run an inline query string")
    ap.add_argument("--list", action="store_true",
                    help="list library queries and exit")
    ap.add_argument("-p", "--build-dir", default=None,
                    help="directory containing compile_commands.json, or a path "
                         "straight to the file (default: auto-detect by probing "
                         "common build dirs up from the cwd)")
    ap.add_argument("--clang-query",
                    default=(shutil.which("clang-query")
                             or "/opt/homebrew/opt/llvm/bin/clang-query"))
    ap.add_argument("--extra-arg", action="append", default=[],
                    help="extra clang arg, repeatable (e.g. -isysroot=...)")
    ap.add_argument("--no-auto-sdk", action="store_true",
                    help="don't auto-add macOS -isysroot/-resource-dir "
                         "(on by default on macOS; see the macOS note)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("-c", "--context", type=int, default=6,
                    help="source context lines on each side (report/json)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="Markdown investigation packet for an AI")
    ap.add_argument("--show-errors", action="store_true")
    args = ap.parse_args()

    if args.list:
        lib = library_queries()
        if not lib:
            sys.exit(f"no queries in {QUERY_LIB}/")
        for p, title, desc in lib:
            print(f"{p.name}\t{title}")
            if desc:
                print(f"\t{desc}")
        return

    query_file, cleanup, rubric_path = resolve_query(args)
    try:
        run(args, query_file, rubric_path)
    finally:
        cleanup()


def run(args, query_file, rubric_path):
    raw_mode = bool(RAW_OUTPUT_RE.search(
        query_file.read_text(encoding="utf-8", errors="replace")))
    if raw_mode and (args.json or args.report):
        sys.exit("error: --json/--report need source locations, but this query "
                 "uses a dump/print/detailed-ast output mode (raw text, no "
                 "locations). Use a diag-output query, or drop --json/--report.")

    db_dir, db, others = find_compile_db(args.build_dir)
    if db_dir is None:
        tried = "\n  ".join(others[:8])
        more = f"\n  ... ({len(others) - 8} more)" if len(others) > 8 else ""
        sys.exit(f"error: no {DB_NAME} found. Tried:\n  {tried}{more}\n"
                 f"Generate one with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON (CMake) "
                 f"or `bear -- make`, then pass -p <build-dir> if it's elsewhere.")

    if args.build_dir is None:                 # report what auto-detection chose
        print(f"using {db}", file=sys.stderr)
        if others:
            print(f"  ({len(others)} other {DB_NAME} found; using the newest — "
                  f"pass -p to pick a different one)", file=sys.stderr)

    sources = sorted({e["file"] for e in json.loads(db.read_text())})

    extra = list(args.extra_arg)
    if not args.no_auto_sdk:
        auto = macos_sdk_args(args.clang_query, args.extra_arg)
        if auto:
            print("macOS: auto-added " + " ".join(auto) +
                  "  (disable with --no-auto-sdk)", file=sys.stderr)
            extra = auto + extra

    seen, raw_chunks, errors = set(), [], []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = [pool.submit(run_one, args.clang_query, str(db_dir), query_file,
                            s, extra) for s in sources]
        for i, (s, f) in enumerate(zip(sources, futs), 1):
            print(f"\r[{i}/{len(sources)}] scanning...", end="", file=sys.stderr)
            hits, raw, err = f.result()
            seen.update(hits)            # (file,line,col) dedups header hits
            if raw_mode and raw.strip():
                raw_chunks.append((s, raw.rstrip()))
            if err:
                errors.append(err)
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    if raw_mode:
        for s, chunk in raw_chunks:
            print(f"==== {s} ====")
            print(chunk)
        print(f"\n{len(raw_chunks)} of {len(sources)} TU(s) produced output",
              file=sys.stderr)
    else:
        findings = []
        for file, line, col in sorted(seen):
            findings.append({
                "file": file, "line": line, "col": col,
                "context": context_snippet(file, line, args.context),
            })
        if args.report:
            emit_report(findings, sources, rubric_path)
        elif args.json:
            print(json.dumps(findings, indent=2))
        else:
            for h in findings:
                print(f"{h['file']}:{h['line']}:{h['col']}")
            print(f"\n{len(findings)} match(es) across {len(sources)} TU(s)",
                  file=sys.stderr)

    if errors:
        n, total = len(errors), len(sources)
        head = (f"warning: {n} of {total} translation unit(s) failed to parse — "
                f"results are INCOMPLETE")
        empty = not raw_chunks if raw_mode else not seen
        if empty:
            head += (".\n  Zero results here most likely means the parse "
                     "failed, not that the code is clean. On macOS this tool "
                     "auto-adds -isysroot/-resource-dir to fix the usual "
                     "Apple-clang-DB header mismatch; if TUs still fail, the "
                     "SDK probe may have failed (is `xcrun` working?) or the "
                     "build needs a specific toolchain — see the macOS note in "
                     "--help, or pass --extra-arg yourself")
        if args.show_errors:
            sys.stderr.write(f"\n{head}:\n\n" + "\n\n".join(errors) + "\n")
        else:
            print(f"{head}; rerun with --show-errors to see them.",
                  file=sys.stderr)
        if n == total:                     # nothing parsed: a hard failure
            sys.exit(1)


def emit_report(findings, sources, rubric_path):
    w = sys.stdout.write
    if rubric_path:
        w(rubric_path.read_text(encoding="utf-8", errors="replace"))
        if not rubric_path.read_text().endswith("\n"):
            w("\n")
    else:
        w(GENERIC_RUBRIC)
    w(f"\n**{len(findings)} finding(s)** across {len(sources)} translation "
      f"unit(s).\n\n")
    for i, f in enumerate(findings, 1):
        w(f"## Finding {i}\n")
        w(f"`{f['file']}:{f['line']}:{f['col']}`\n\n")
        w("```cpp\n" + f["context"] + "\n```\n\n")


if __name__ == "__main__":
    main()
