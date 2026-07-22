#!/usr/bin/env python3
"""list-scripts.py — List every script in this collection, with a one-line summary.

A directory of two dozen single-purpose tools is only useful if you can
remember what's in it. This prints the whole collection — the name you actually
type, plus one line on what it does — and filters it by keyword:

    list-scripts              # everything
    list-scripts git          # just the git tools
    list-scripts -l unicode   # matches, with a paragraph of detail each

WHERE THE SUMMARIES COME FROM
-----------------------------
The README's summary table is the source of truth. Those one-liners are already
written and curated, and duplicating them into the scripts would just create two
copies to keep in sync. A script that isn't in the table yet still shows up: its
summary is derived from its module docstring instead, and marked (undocumented)
so the gap is visible rather than silent.

What exists, on the other hand, is decided by the filesystem, not the README. A
`<name>.py` counts as a script if it has a launcher wrapper (`<name>` for bash,
`<name>.cmd` for Windows) or a row in the README table; anything else is a
shared module (`branch_tui.py`, `editor_config.py`, `editor_ide.py`) and is left
out. Because the two sources are independent, they can disagree — which is what
--check is for:

    list-scripts --check

reports scripts missing from the README, README rows with no script, missing
wrappers, table links pointing at absent sections, rows out of alphabetical
order, and stray .py files that look like scripts but are neither. It exits
non-zero on any of those, so it works as a pre-commit sanity check on the
collection itself.

--markdown regenerates the table rows in README format, so adding a script is:
write it, add its wrappers, then paste the regenerated table.

Output adapts to where it goes: when stdout is a terminal, summaries wrap to its
width and names are highlighted; when piped, every script stays on exactly one
line so the output greps cleanly. Colour follows NO_COLOR and --color.

Usage:
    list-scripts.py [-l] [--check | --markdown | --json | -1] [TERM ...]

Examples:
    list-scripts.py                    # the whole collection
    list-scripts.py git branch         # names/summaries matching both terms
    list-scripts.py -r '^git-'         # match names by regex
    list-scripts.py -l script-runs     # one entry, with detail
    list-scripts.py --check            # README vs. reality
    list-scripts.py --markdown         # regenerate the README table

Exit status:
    0   scripts were listed, or --check found nothing wrong
    1   no script matched the filter, or --check found a problem
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import ast
import json
import os
import re
import shutil
import sys
import textwrap

__version__ = "1.0.0"

# Modules that live beside the scripts but aren't scripts. They're detected by
# the absence of a wrapper/README row rather than by this list; it exists only
# so --check can stay quiet about them if they ever grow a __main__ block.
KNOWN_MODULES = {"branch_tui.py", "editor_config.py", "editor_ide.py"}

TABLE_HEADER_RE = re.compile(r"^\|\s*Script\s*\|\s*What it does\s*\|\s*$", re.I)
# A row is "| [`name.py`](#anchor) | summary |", or the same without the link.
TABLE_ROW_RE = re.compile(
    r"^\|\s*(?:\[\s*`(?P<linked>[^`]+)`\s*\]\((?P<anchor>[^)]*)\)|`(?P<bare>[^`]+)`)"
    r"\s*\|\s*(?P<summary>.*?)\s*\|\s*$"
)
SECTION_RE = re.compile(r"^##\s+`(?P<name>[^`]+)`\s*$")


class Script:
    """One entry in the collection: the .py plus whatever documents it."""

    def __init__(self, filename, directory):
        self.filename = filename                     # "git-batch.py"
        self.name = filename[:-3]                    # "git-batch" — what you type
        self.directory = directory
        self.summary = ""            # README one-liner, or docstring fallback
        self.from_readme = False     # False means the summary was derived
        self.has_bash = os.path.isfile(os.path.join(directory, self.name))
        self.has_cmd = os.path.isfile(os.path.join(directory, self.name + ".cmd"))

    @property
    def path(self):
        return os.path.join(self.directory, self.filename)

    def paragraphs(self):
        """The script's own description, as a list of unwrapped paragraphs.

        Docstrings in this collection open one of two ways — straight into the
        prose, or with a "name.py" title line (sometimes with the first
        sentence trailing an em dash on that same line). Both are normalised
        here to the prose alone, and each paragraph is re-joined into a single
        line so the caller can re-wrap it to the terminal.
        """
        doc = read_docstring(self.path)
        if not doc:
            return []
        lines = doc.strip().splitlines()

        # Strip a leading title line: the bare filename, or "filename — text",
        # in which case `text` is the real start of the first paragraph.
        title = re.match(r"^" + re.escape(self.filename) + r"\s*(?:[-—–]{1,2}\s*(.*))?$",
                         lines[0].strip())
        if title:
            lines = ([title.group(1)] if title.group(1) else []) + lines[1:]

        paragraphs, current = [], []
        for line in lines:
            if line.strip():
                current.append(line.strip())
            elif current:
                paragraphs.append(" ".join(current))
                current = []
        if current:
            paragraphs.append(" ".join(current))
        return paragraphs

    def detail(self, threshold=120):
        """The paragraphs to show under --long.

        The opening paragraph is often just the summary restated — that's the
        shape of a docstring whose first line is a title. When it's that short
        the paragraph after it is where the actual explanation lives, so both
        are returned; a substantial opener stands on its own.
        """
        paragraphs = self.paragraphs()
        if len(paragraphs) > 1 and len(paragraphs[0]) < threshold:
            return paragraphs[:2]
        return paragraphs[:1]


def read_docstring(path):
    """Return a file's module docstring, or "" if it has none/won't parse.

    Parsing with `ast` rather than regex means a docstring is only ever taken
    from where Python would take one, and a file that happens to open with a
    string-shaped comment can't fool it. A syntax error is not this tool's
    problem to report, so it degrades to no docstring.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError, ValueError):
        return ""
    return ast.get_docstring(tree) or ""


def first_sentence(text, limit=110):
    """Condense a paragraph to one line: its first sentence, length-capped.

    Used only for scripts the README doesn't cover, to keep the listing's
    single-line-per-script shape even when the summary has to be improvised.
    """
    text = " ".join(text.split())
    if not text:
        return ""
    # A period ends a sentence only when followed by a space and a capital, so
    # "the .docx file" and "e.g. this" don't split mid-sentence.
    match = re.search(r"(?<=[.!?])\s+(?=[A-Z(])", text)
    if match:
        text = text[:match.start()]
    if len(text) > limit:
        text = text[:limit - 1].rstrip(" ,;:") + "…"
    return text


def parse_readme(path):
    """Return (summaries, anchors, sections, order) parsed out of README.md.

    summaries: {"git-batch.py": "Run one git command ..."}  — the table
    anchors:   {"git-batch.py": "#git-batchpy"}             — the table's links
    sections:  {"git-batch.py"}                             — "## `name.py`" headings
    order:     the filenames in the order the table lists them

    Everything is best-effort: a README without a table simply yields empty
    results, and the listing falls back to docstrings for every script.
    """
    summaries, anchors, order, sections = {}, {}, [], set()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return summaries, anchors, sections, order

    in_table = False
    for line in lines:
        section = SECTION_RE.match(line)
        if section:
            sections.add(section.group("name"))
        if TABLE_HEADER_RE.match(line):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):                 # blank line ends the table
            in_table = False
            continue
        row = TABLE_ROW_RE.match(line)
        if not row:                                  # the |---|---| separator
            continue
        name = row.group("linked") or row.group("bare")
        summaries[name] = row.group("summary")
        anchors[name] = row.group("anchor") or ""
        order.append(name)
    return summaries, anchors, sections, order


def github_anchor(heading):
    """The fragment GitHub generates for a "## `name.py`" heading."""
    slug = heading.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)             # backticks, dots, etc. drop out
    return "#" + re.sub(r"\s+", "-", slug.strip())


def discover(directory, readme_summaries):
    """Find the scripts in `directory`, in name order.

    A .py is a script if it can be launched by name (it has a wrapper) or the
    README table lists it. The two are OR'd rather than AND'd so that a
    half-finished addition — script written, wrappers not yet made, or the
    reverse — still appears in the listing and in --check, instead of
    vanishing from both.
    """
    try:
        entries = sorted(os.listdir(directory), key=str.lower)
    except OSError as exc:
        raise SystemExit(f"Error: cannot list '{directory}': {exc}")

    scripts = []
    for filename in entries:
        if not filename.endswith(".py"):
            continue
        script = Script(filename, directory)
        if not (script.has_bash or script.has_cmd or filename in readme_summaries):
            continue
        if filename in readme_summaries:
            script.summary = readme_summaries[filename]
            script.from_readme = True
        else:
            paragraphs = script.paragraphs()
            script.summary = first_sentence(paragraphs[0]) if paragraphs else ""
        scripts.append(script)
    return scripts


def matches(script, terms, use_regex):
    """True if the script satisfies every search term (AND, case-insensitive).

    Both the name and the summary are searched, so "branch" finds git-switch
    even though its name doesn't contain the word.
    """
    haystack = f"{script.name} {script.summary}".lower()
    for term in terms:
        if use_regex:
            try:
                if not re.search(term, haystack, re.I):
                    return False
            except re.error as exc:
                raise SystemExit(f"Error: bad regex '{term}': {exc}")
        elif term.lower() not in haystack:
            return False
    return True


class Style:
    """ANSI styling that switches itself off when the output isn't a terminal."""

    def __init__(self, enabled):
        self.enabled = enabled

    def _wrap(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def name(self, text):
        return self._wrap("1;36", text)              # bold cyan

    def dim(self, text):
        return self._wrap("2", text)

    def code(self, text):
        """Render a markdown `code` span: coloured if we can, else unbacktick."""
        return self._wrap("36", text)

    def render(self, text):
        """Turn a markdown one-liner into a single line of terminal text."""
        return self.render_wrapped(text, width=None)[0]

    def render_wrapped(self, text, width):
        """Render markdown `code` spans, wrapped to `width` (None: one line).

        Wrapping happens *before* styling, on the text with its backticks
        still in place, because ANSI escapes have no printed width — wrapping
        the styled string would break lines many columns early. The open/close
        state is carried across lines so a span that straddles a line break is
        still styled on both halves.
        """
        lines = (textwrap.wrap(text, width) or [""]) if width else [text]
        rendered, in_span = [], False
        for line in lines:
            out, is_code = "", in_span
            parts = line.split("`")
            for index, part in enumerate(parts):
                out += self.code(part) if is_code and part else part
                if index != len(parts) - 1:          # flip once per backtick,
                    is_code = not is_code            # not once per part
            in_span = is_code            # an odd count leaves the span open
            rendered.append(out)
        return rendered


def print_listing(scripts, style, long, width, wrap):
    """Print the human-readable listing.

    Names are padded into a column so the summaries line up, but the column is
    capped: one very long name shouldn't push every summary to the right. When
    wrapping is off (piped output) each script stays on a single line, which is
    what makes `list-scripts | grep` behave.
    """
    column = min(max(len(s.name) for s in scripts) + 2, 26)
    for index, script in enumerate(scripts):
        summary = script.summary or "(no summary)"
        if not script.from_readme and script.summary:
            summary += " (undocumented)"

        if not wrap:
            print(f"{style.name(script.name)}\t{style.render(summary)}")
        else:
            body = style.render_wrapped(summary, max(width - column, 24))
            print(style.name(script.name.ljust(column)) + body[0])
            for line in body[1:]:
                print(" " * column + line)

        if long:
            indent = "    "
            for paragraph in script.detail():
                print(textwrap.indent(
                    textwrap.fill(paragraph, max(width - len(indent), 40))
                    if wrap else paragraph, indent))
            if index != len(scripts) - 1:
                print()


def check(scripts, directory, anchors, sections, order):
    """Report every way the README and the filesystem disagree.

    Returns a list of problem strings; empty means the collection is
    self-consistent. Each check exists because it catches a mistake that is
    easy to make when adding a script and invisible until someone else trips
    over it.
    """
    problems = []
    by_filename = {s.filename: s for s in scripts}

    for script in scripts:
        if not script.from_readme:
            problems.append(f"{script.filename}: no row in the README summary table")
        if not script.has_bash:
            problems.append(f"{script.filename}: no bash wrapper ('{script.name}')")
        if not script.has_cmd:
            problems.append(f"{script.filename}: no Windows wrapper ('{script.name}.cmd')")

    for filename in order:
        if filename not in by_filename:
            problems.append(f"{filename}: listed in the README table but not in {directory}")
            continue
        # A table link that points nowhere renders as a dead link on GitHub.
        anchor = anchors.get(filename, "")
        if anchor and anchor != github_anchor(filename):
            problems.append(f"{filename}: table link '{anchor}' should be "
                            f"'{github_anchor(filename)}'")
        if anchor and filename not in sections:
            problems.append(f"{filename}: table links to a '## `{filename}`' "
                            "section that doesn't exist")

    if order != sorted(order, key=str.lower):
        problems.append("README table rows are not in alphabetical order")

    # A .py with no wrapper and no README row was skipped by discover(). That's
    # right for the shared modules, and wrong for a script whose wrappers were
    # forgotten — which a __main__ block distinguishes.
    for filename in sorted(os.listdir(directory), key=str.lower):
        if not filename.endswith(".py") or filename in by_filename:
            continue
        if filename in KNOWN_MODULES:
            continue
        if re.search(r"^if __name__\s*==", read_source(os.path.join(directory, filename)),
                     re.M):
            problems.append(f"{filename}: looks like a script but has no wrapper "
                            "and no README row")
    return problems


def read_source(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="list-scripts.py",
        description="List every script in this collection, with a one-line summary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "notes:\n"
            "  Summaries come from the README's table; scripts it doesn't cover\n"
            "  fall back to their docstring and are marked (undocumented).\n"
            "  Terms are ANDed and match the name and the summary.\n"
            "\n"
            "examples:\n"
            "  list-scripts.py                 # the whole collection\n"
            "  list-scripts.py git branch      # matching both terms\n"
            "  list-scripts.py -l script-runs  # with a paragraph of detail\n"
            "  list-scripts.py --check         # README vs. reality\n"
        ),
    )
    parser.add_argument("terms", nargs="*", metavar="TERM",
                        help="only list scripts matching every term (name or summary)")
    parser.add_argument("-l", "--long", action="store_true",
                        help="also print a paragraph of detail from each script")
    parser.add_argument("-r", "--regex", action="store_true",
                        help="treat TERMs as regular expressions")
    parser.add_argument("-1", "--names-only", action="store_true",
                        help="print bare names, one per line (for scripting)")
    parser.add_argument("--markdown", action="store_true",
                        help="print the summary table in README format")
    parser.add_argument("--json", action="store_true",
                        help="print the listing as JSON")
    parser.add_argument("--check", action="store_true",
                        help="report README/filesystem inconsistencies and exit")
    parser.add_argument("-C", dest="directory", default=None, metavar="DIR",
                        help="list this directory instead of the script's own")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="colourise the output (default: auto)")
    parser.add_argument("-V", "--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    exclusive = [name for name in ("names_only", "markdown", "json", "check")
                 if getattr(args, name)]
    if len(exclusive) > 1:
        parser.error("--check, --markdown, --json and -1 are mutually exclusive")

    # Default to the directory holding this script, resolved through symlinks,
    # so the listing is of *this* collection no matter where you run it from —
    # the same trick update-scripts.py uses to find its own checkout.
    directory = args.directory or os.path.dirname(os.path.realpath(__file__))
    readme = os.path.join(directory, "README.md")
    summaries, anchors, sections, order = parse_readme(readme)
    scripts = discover(directory, summaries)

    if not scripts:
        print(f"No scripts found in {directory}.", file=sys.stderr)
        return 1

    if args.check:
        problems = check(scripts, directory, anchors, sections, order)
        for problem in problems:
            print(problem)
        count = len(scripts)
        print(f"\n{count} script{'s' if count != 1 else ''}, "
              f"{len(problems)} problem{'s' if len(problems) != 1 else ''}.")
        return 1 if problems else 0

    selected = [s for s in scripts if matches(s, args.terms, args.regex)]
    if not selected:
        print("No scripts matched " + " ".join(args.terms) + ".", file=sys.stderr)
        return 1

    if args.names_only:
        for script in selected:
            print(script.name)
        return 0

    if args.json:
        print(json.dumps([{"name": s.name, "file": s.filename, "summary": s.summary,
                           "documented": s.from_readme, "bash_wrapper": s.has_bash,
                           "cmd_wrapper": s.has_cmd} for s in selected], indent=2))
        return 0

    if args.markdown:
        print("| Script | What it does |")
        print("| ------ | ------------ |")
        for script in sorted(selected, key=lambda s: s.filename.lower()):
            summary = script.summary or "TODO"
            print(f"| [`{script.filename}`]({github_anchor(script.filename)}) | {summary} |")
        return 0

    isatty = sys.stdout.isatty()
    color = args.color == "always" or (
        args.color == "auto" and isatty and not os.environ.get("NO_COLOR"))
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    print_listing(selected, Style(color), args.long, width, wrap=isatty)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # `list-scripts | head` closes the pipe early; that's not an error.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
