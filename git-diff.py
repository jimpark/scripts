#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
r"""An interactive, full-screen front end for `git diff` -- every changed line
gathered into a collapsible tree of files, then hit Enter to jump straight to
that line in your editor. Open as many as you like; the browser stays put until
you quit.

Where git-grep starts from a pattern you type, git-diff starts from the diff
itself: it runs `git diff` (passing through whatever arguments you give -- see
below) and lays the result out as a browsable, *searchable* tree. Added lines
show in green with a leading "+", removed lines in red with a "-", and the
surrounding context dimmed; every row is tagged with the line it opens at and a
:N jump number.

Whatever you put after the command is handed straight to `git diff`, so the
usual selectors all work:

    git-diff.py                 unstaged changes (plain `git diff`)
    git-diff.py --staged        what's staged for the next commit
    git-diff.py HEAD            everything uncommitted (staged + unstaged)
    git-diff.py main            working tree vs the `main` branch
    git-diff.py v1.0 v1.1       between two commits
    git-diff.py -- src/         limit to a path

The picker is *modal*, in the spirit of vim -- you land in BROWSE mode on the
full diff and refine it from there:

  BROWSE mode
    j / k  or  Up / Down     move the highlight cursor
    g / G                    jump to the top / bottom
    n / p                    jump to the first line of the next / previous file
    h / Left                 hop up to the parent folder/file
    l / Right                expand / step into a folder or file
    Enter                    open the changed line under the cursor at its
                             position (on a folder/file row, fold it instead)
    r                        re-run `git diff` and refresh (handy after
                             editing or staging), keeping your place
    /                        refine: filter the current lines with a sub-grep
                             (push a level onto the stack)
    <                        back up one level (pop the last filter)
    \                        start fresh: drop every filter, show the whole diff
    0-9                      set the diff context to N lines (re-runs `git diff
                             -U N`; 0 = only the changed lines)
    + / -                    widen / narrow that context (+ goes past 9)
    :N                       jump the cursor to line number N (the leading
                             number on each row); Enter/Esc or any move closes
                             the prompt, ':' again starts a new number
    Tab                      toggle case-insensitive filter matching
    q                        quit (Esc never quits -- it only navigates)

  FILTER mode (a sub-grep over the current lines; reached with / from BROWSE)
    type                     a pattern that narrows the visible lines *live*,
                             matched against the file path and the line text
    !pattern                 exclude: keep the lines that do NOT match
    Enter                    push this filter onto the stack
    Esc                      cancel without pushing
    Up / Down                move through the filtered lines

Because filters stack, you can drill down in steps that a single regex can't
express -- e.g. `/` `TODO` to keep the lines mentioning it, then `/` `!_test.`
to drop the test files -- and `<` pops back up a level at a time while `\`
wipes the stack to show the whole diff again. The diff's own context lines are
part of the searchable set, so a filter can match on something that merely sits
*near* a change; 0-9 / +/- control how many of those context lines `git diff`
hands over.

Matching lines are grouped under their file, and files nest in a **folder tree**
split on "/", so changes in `src/app/main.c` and `src/lib/parse.c` sit under a
`src/` folder you can collapse. Everything starts expanded so the diff is
visible at a glance.

The editor, and *how to open it at a line*, come from `.git-open-config` -- the
same TOML file git-open and git-grep use, living next to these scripts and
gitignored:

    editor = "vim"
    line   = "+{line} {file}"

`line` is split shell-style with {file}/{line}/{column} substituted in, so it
adapts to any editor. The file is opened by its full path, and git diff is run
at the repository root, so this works from any subdirectory. A changed line
opens at its new-side position; a *removed* line, which no longer exists, opens
at the nearest surviving line. (When diffing two arbitrary commits the new side
isn't your working tree, so the opened file may not line up exactly.)

As with git-open and git-grep, when run inside the integrated terminal of VS
Code, a JetBrains IDE (CLion), or Zed, opening a line hands it to that
already-running editor (a `vscode://` URL or a `clion`/`zed` launcher) so it
lands in a new tab there rather than spawning the configured editor. Those
hand-offs are fire-and-forget, so the browser stays up. The detection lives in
editor_ide, shared with the other tools.

Runs on macOS, Linux, and Windows using only the standard library (raw terminal
mode + ANSI escapes; no curses, no third-party packages). Borrows its tree, key
input, and terminal handling from branch_tui, its editor handling from
editor_config, and its host-editor detection from editor_ide.

Exit status:
    0   you quit normally (whether or not you opened anything)
    1   not inside a Git repository, or not an interactive terminal
"""

import argparse
import os
import re
import subprocess
import sys
from collections import namedtuple

from branch_tui import (BLUE, BOLD, CLEAR_EOL, CLEAR_EOS, CYAN, GREEN, HOME,
                        RESET, REVERSE, Node, TerminalSession, build_visible,
                        git, in_git_repo, read_key, term_size)
from editor_config import load_config, open_args
from editor_ide import detect_ide

__version__ = "1.0.0"

DIM = "\x1b[2m"                  # context lines, carried over from the hunks
RED = "\x1b[31m"                 # removed lines

# One changed line from the diff. `file` is the new-side path (the old path for
# a deleted file). `line` is where Enter opens it: the new-side line number for
# an added/context line, the nearest surviving line for a removed one. `kind` is
# "add" | "del" | "ctx". `openable` is False for lines with nowhere to go (a
# deleted file, a binary change). `seq` is a per-run counter giving every line a
# unique key, and `name` is that key spelled out for branch_tui's row tracking.
DiffLine = namedtuple("DiffLine", "file line text kind openable seq name")

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
KIND = {" ": "ctx", "+": "add", "-": "del"}


# ─── git plumbing ────────────────────────────────────────────────────────────
def repo_toplevel():
    out = git(["rev-parse", "--show-toplevel"])
    return out.stdout.strip() if out.returncode == 0 else None


def _strip_prefix(path):
    """Undo git's `a/`/`b/` diff prefixes (and surrounding quotes when git added
    them for an unusual path)."""
    path = path.strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        path = path[1:-1]
    if path[:2] in ("a/", "b/"):
        path = path[2:]
    return path


def parse_diff(text):
    """Walk unified-diff text into a flat list of DiffLine. Tracks the new-side
    line counter through each hunk so added/context lines carry their real line
    number and removed lines point at the line that now sits in their place."""
    lines = []
    seq = 0
    new_path = old_path = None
    openable = True
    new_line = 0
    in_hunk = False
    for raw in text.split("\n"):
        if raw.startswith("diff --git"):
            new_path = old_path = None
            openable = True
            in_hunk = False
        elif raw.startswith("--- "):
            old_path = _strip_prefix(raw[4:])
            in_hunk = False
        elif raw.startswith("+++ "):
            rest = raw[4:].strip()
            if rest == "/dev/null":          # a deletion: file off the new side
                openable = False
                new_path = old_path
            else:
                openable = True
                new_path = _strip_prefix(rest)
            in_hunk = False
        elif raw.startswith("@@"):
            m = HUNK_RE.match(raw)
            if m:
                new_line = int(m.group(1))
                in_hunk = True
        elif raw.startswith("Binary files") and new_path:
            seq += 1
            lines.append(DiffLine(new_path, 1, "(binary file differs)", "ctx",
                                  False, seq, "{0}\x00{1}".format(new_path, seq)))
        elif in_hunk and raw[:1] in KIND:
            kind = KIND[raw[0]]
            path = new_path or old_path or "?"
            seq += 1
            lines.append(DiffLine(path, max(1, new_line), raw[1:], kind,
                                  openable, seq,
                                  "{0}\x00{1}".format(path, seq)))
            if kind != "del":                # removed lines don't advance it
                new_line += 1
    return lines


def run_diff(toplevel, diff_args, context):
    """Run `git diff -U<context>` at the repo root with the user's pass-through
    args. Returns (lines, None) on success or (None, message) when git itself
    errored (e.g. a bad revision)."""
    args = ["-C", toplevel, "diff", "-U{0}".format(context), "--no-color"]
    args += diff_args
    out = git(args)
    if out.returncode not in (0, 1):    # 1 only appears with --exit-code/--quiet
        return None, out.stderr.strip() or "git diff failed"
    return parse_diff(out.stdout), None


# ─── diff result tree ────────────────────────────────────────────────────────
def build_diff_tree(diff_lines):
    """Weave DiffLine items into a Node tree: directories and files are folders,
    each changed line a leaf under its file. Returns (root, folder_paths) so the
    caller can open every folder by default. Leaves are keyed (and so ordered)
    by `seq`, the order git emitted them, since several lines can share a single
    new-side line number."""
    root = Node("", "")
    folders = set()
    for d in diff_lines:
        parts = d.file.split("/")
        node = root
        for i, seg in enumerate(parts):
            path = "/".join(parts[:i + 1])
            child = node.children.get(seg)
            if child is None:
                child = Node(seg, path)
                node.children[seg] = child
            node = child
            folders.add(path)
        key = "{0:020d}".format(d.seq)
        leaf = Node(key, node.path + "\x00" + key)
        node.children[key] = leaf
        leaf.branch = d
    return root, folders


def is_file_node(node):
    """A file node is a folder whose children are diff leaves (not subfolders)."""
    for child in node.children.values():
        return child.branch is not None
    return False


# ─── terminal session that can step aside for the editor ─────────────────────
class Screen(TerminalSession):
    """A TerminalSession that can suspend itself (leave raw mode + the alternate
    screen) while an interactive editor runs, then resume cleanly afterwards."""

    def suspend(self):
        self.__exit__(None, None, None)

    def resume(self):
        self.__enter__()


# ─── the diff browser ────────────────────────────────────────────────────────
class DiffBrowser(object):
    title = "git-diff"

    def __init__(self, toplevel, repo_name, diff_args, editor_argv,
                 line_template, use_color):
        self.toplevel = toplevel
        self.repo_name = repo_name
        self.diff_args = diff_args
        self.diff_label = " ".join(diff_args) if diff_args else "working tree"
        self.editor_argv = editor_argv
        self.line_template = line_template
        self.use_color = use_color
        self.ide = detect_ide()     # editor whose terminal we're in, or None:
                                    # hand lines to it rather than spawning one

        self.ignore_case = False
        self.context = 3            # git diff's own default -U value
        self.mode = "browse"        # "browse" | "filter"
        self.all_lines = []         # the parsed diff (the base set)
        self.filters = []           # stack of {"pat", "neg"} refinements
        self.finput = ""            # the filter being typed in FILTER mode
        self.matches = []           # the displayed set after filters
        self.root = None
        self.expanded = set()
        self.jump_active = False    # in the ':' jump-to-number prompt?
        self.jump_buf = ""
        self.cursor = 0
        self.cur_id = None
        self.top = 0
        self.rows = []
        self.status = ""

    # -- running git diff ----------------------------------------------------
    def reload(self, preserve=False):
        """Run git diff for the current args/context and apply the filter stack.
        With preserve=True keep the cursor where it is (a refresh); otherwise
        land on the first changed line."""
        base, err = run_diff(self.toplevel, self.diff_args, self.context)
        if base is None:
            self.all_lines = []
            self._show([])
            self.status = "git diff: " + err
            return
        self.all_lines = base
        self._show(self._compute(), preserve=preserve)
        if preserve:
            self.status = "refreshed"
        elif not self.all_lines:
            self.status = "no changes"
        elif not self.matches:
            self.status = "no lines left after filters"
        else:
            self.status = ""

    # -- the filter stack ----------------------------------------------------
    @staticmethod
    def _parse_filter(raw):
        """Turn typed text into a filter dict, or None if empty. A leading '!'
        means exclude (keep the lines that do NOT match)."""
        raw = raw.strip()
        if not raw:
            return None
        neg = raw.startswith("!")
        pat = raw[1:].strip() if neg else raw
        if not pat:
            return None
        return {"pat": pat, "neg": neg}

    def _compute(self, extra=None):
        """Apply the filter stack to the base diff: start from every line, then
        for each filter keep the lines it matches (path + text). `extra` previews
        one more filter without committing it. A line survives on the new side's
        path and content, so context lines carried over from the hunks are
        searchable too."""
        flags = re.IGNORECASE if self.ignore_case else 0
        result = list(self.all_lines)
        chain = list(self.filters)
        if extra is not None:
            chain.append(extra)
        for f in chain:
            try:
                rx = re.compile(f["pat"], flags)
            except re.error:
                rx = re.compile(re.escape(f["pat"]), flags)
            kept = []
            for d in result:
                hit = bool(rx.search(d.file + "\t" + d.text))
                if (not hit) if f["neg"] else hit:
                    kept.append(d)
            result = kept
        return result

    def _show(self, matches, preserve=False):
        """Display a list of diff lines: rebuild the tree and place the cursor
        (on the first line, or the prior one when preserve=True)."""
        self.matches = matches
        if not matches:
            self.root, self.expanded, self.rows = None, set(), []
            self.cur_id, self.cursor = None, 0
            return
        self.root, self.expanded = build_diff_tree(matches)
        if not preserve:
            self.cur_id = None
        self.rebuild()
        if not preserve or self.cur_id is None:
            self.cursor = self._first_match_index()
        self._sync_id()

    def _refresh_filter_view(self):
        """While composing a filter, show the live narrowed result: the
        committed stack plus the in-progress pattern."""
        self._show(self._compute(extra=self._parse_filter(self.finput)))

    def push_filter(self):
        """Commit the typed filter onto the stack and return to BROWSE."""
        pending = self._parse_filter(self.finput)
        self.finput = ""
        self.mode = "browse"
        if pending is None:
            self._show(self._compute())                # nothing typed: restore
            return
        self.filters.append(pending)
        self._show(self._compute())
        n = len(self.matches)
        self.status = "{0} {1} -> {2} line{3}".format(
            "exclude" if pending["neg"] else "filter", pending["pat"],
            n, "" if n == 1 else "s")

    def pop_filter(self):
        """Back up one level: drop the most recent filter."""
        if not self.filters:
            self.status = "showing the whole diff"
            return
        f = self.filters.pop()
        self._show(self._compute())
        self.status = "popped {0}{1}".format("-" if f["neg"] else "+", f["pat"])

    def start_fresh(self):
        """Drop the entire filter stack and show the whole diff again."""
        if not self.filters and self.mode == "browse":
            self.status = "already showing the whole diff"
            return
        self.filters = []
        self.finput = ""
        self.mode = "browse"
        self._show(self._compute())
        self.status = "cleared filters"

    # -- the context window --------------------------------------------------
    def set_ctx(self, value):
        """Set git diff's -U context, clamped to [0, 999], and re-run (keeping
        the cursor). Unlike git-grep's window, this comes straight from git, so
        widening pulls fresh context lines into the searchable set."""
        value = max(0, min(value, 999))
        if value == self.context:
            return
        self.context = value
        self.reload(preserve=True)
        self.status = "context -U{0}".format(value)

    # -- building the visible rows ------------------------------------------
    def rebuild(self):
        self.rows = build_visible(self.root, self.expanded, None) if self.root else []
        if self.cur_id is not None:
            for i, row in enumerate(self.rows):
                if row["id"] == self.cur_id:
                    self.cursor = i
                    break
            else:
                self.cursor = self._first_match_index()
        self.cursor = max(0, min(self.cursor, len(self.rows) - 1)) if self.rows else 0
        self.cur_id = self.rows[self.cursor]["id"] if self.rows else None

    def _first_match_index(self):
        for i, row in enumerate(self.rows):
            if row["type"] == "branch":
                return i
        return 0

    def _file_count(self):
        return len({m.file for m in self.matches})

    # -- cursor movement -----------------------------------------------------
    def _sync_id(self):
        if self.rows:
            self.cur_id = self.rows[self.cursor]["id"]

    def move(self, delta):
        if self.rows:
            self.cursor = max(0, min(self.cursor + delta, len(self.rows) - 1))
            self._sync_id()

    def move_to(self, index):
        if self.rows:
            self.cursor = max(0, min(index, len(self.rows) - 1))
            self._sync_id()

    def _file_row_indices(self):
        return [i for i, r in enumerate(self.rows)
                if r["type"] == "folder" and is_file_node(r["node"])]

    def _owning_file_index(self, idx):
        """Row index of the file row that owns rows[idx] (itself, if it already
        is one), or None when the cursor sits on a plain directory folder."""
        row = self.rows[idx]
        if row["type"] == "folder" and is_file_node(row["node"]):
            return idx
        depth = row["depth"]
        for i in range(idx - 1, -1, -1):
            if self.rows[i]["depth"] < depth:
                parent = self.rows[i]
                return i if (parent["type"] == "folder" and
                            is_file_node(parent["node"])) else None
        return None

    def _goto_file(self, file_idx):
        """Land the cursor on the first line under the file at file_idx,
        expanding it first if it's currently folded."""
        row = self.rows[file_idx]
        if not row["expanded"]:
            self.expanded.add(row["node"].path)
            file_id = row["id"]
            self.rebuild()
            file_idx = next(i for i, r in enumerate(self.rows) if r["id"] == file_id)
        target = file_idx
        if target + 1 < len(self.rows) and \
                self.rows[target + 1]["depth"] > self.rows[target]["depth"]:
            target += 1
        self.move_to(target)

    def next_file(self):
        files = self._file_row_indices()
        if not files:
            return
        anchor = self._owning_file_index(self.cursor)
        if anchor is None:
            anchor = self.cursor
        nxt = next((i for i in files if i > anchor), None)
        if nxt is None:
            self.status = "already on the last file"
            return
        self._goto_file(nxt)

    def prev_file(self):
        files = self._file_row_indices()
        if not files:
            return
        anchor = self._owning_file_index(self.cursor)
        if anchor is None:
            anchor = self.cursor
        earlier = [i for i in files if i < anchor]
        if not earlier:
            self.status = "already on the first file"
            return
        self._goto_file(earlier[-1])

    def jump_to_number(self, buf):
        if not buf:
            return
        n = int(buf)
        target = None
        for i, row in enumerate(self.rows):
            if row["number"] is not None:
                target = i
                if row["number"] >= n:
                    break
        if target is not None:
            self.cursor = target
            self._sync_id()

    # -- folder open/close ---------------------------------------------------
    def expand(self):
        if not self.rows:
            return
        row = self.rows[self.cursor]
        if row["type"] != "folder":
            return
        if row["expanded"]:
            self.move(1)
        else:
            self.expanded.add(row["node"].path)

    def collapse(self):
        if not self.rows:
            return
        row = self.rows[self.cursor]
        if row["type"] == "folder" and row["expanded"]:
            self.expanded.discard(row["node"].path)
            return
        path = row["node"].path
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if not parent:
            return
        for i, r in enumerate(self.rows):
            if r["id"] == "F:" + parent:
                self.cursor = i
                self._sync_id()
                break

    def toggle_case(self):
        self.ignore_case = not self.ignore_case
        if self.mode == "filter":
            self._refresh_filter_view()    # recompile the live filter
        else:
            self._show(self._compute(), preserve=True)   # re-apply the stack

    # -- opening a line ------------------------------------------------------
    def open_current(self, screen):
        row = self.rows[self.cursor] if self.rows else None
        if row is None:
            return
        if row["type"] == "folder":
            if row["expanded"]:
                self.expanded.discard(row["node"].path)
            else:
                self.expanded.add(row["node"].path)
            return
        d = row["branch"]
        if not d.openable:
            self.status = "{0}: nothing to open here".format(d.file)
            return
        path = os.path.join(self.toplevel, d.file)
        if self.ide:
            # Inside an editor's integrated terminal: hand the line to that
            # running editor (a URL or CLI launcher that returns at once), so
            # the browser stays up -- no suspend.
            try:
                self.ide.open(path, d.line, 1)
                self.status = "opened {0}:{1} in {2}".format(
                    d.file, d.line, self.ide.label)
            except FileNotFoundError:
                self.status = self.ide.open_error()
            except OSError as exc:
                self.status = "could not open: {0}".format(exc)
            return
        argv = open_args(self.editor_argv, self.line_template, path,
                         line=d.line, column=1)
        screen.suspend()
        try:
            subprocess.call(argv)
            self.status = "opened {0}:{1}".format(d.file, d.line)
        except FileNotFoundError:
            self.status = "editor not found: " + " ".join(self.editor_argv)
        except OSError as exc:
            self.status = "could not open: {0}".format(exc)
        finally:
            screen.resume()

    # -- key handling --------------------------------------------------------
    def handle(self, key, screen):
        if key in ("", "DELETE"):
            return True
        if key == "EOF":
            return False
        if key == "TAB":
            self.status = ""
            self.toggle_case()
            return True
        self.status = ""
        if self.mode == "filter":
            return self._handle_filter(key)
        return self._handle_browse(key, screen)

    def _handle_filter(self, key):
        # FILTER mode: type a sub-grep over the current lines; the list narrows
        # live as you type. A leading '!' excludes. Enter pushes it on the stack.
        if key == "ESC":
            self.mode = "browse"
            self.finput = ""
            self._show(self._compute())                # restore committed view
        elif key == "ENTER":
            self.push_filter()
        elif key == "BACKSPACE":
            self.finput = self.finput[:-1]
            self._refresh_filter_view()
        elif key == "UP":
            self.move(-1)
        elif key == "DOWN":
            self.move(1)
        elif key == "HOME":
            self.move_to(0)
        elif key == "END":
            self.move_to(len(self.rows) - 1)
        elif len(key) == 1 and key >= " ":
            self.finput += key
            self._refresh_filter_view()
        return True

    def _handle_browse(self, key, screen):
        # The ':' jump prompt grabs digits while it's open; press ':' again to
        # start a fresh number, and any other key drops out of it (and is then
        # handled normally below).
        if self.jump_active:
            if key == ":":
                self.jump_buf = ""
                self.status = ":"
                return True
            if key == "BACKSPACE":
                self.jump_buf = self.jump_buf[:-1]
                self.jump_to_number(self.jump_buf)
                self.status = ":" + self.jump_buf
                return True
            if len(key) == 1 and key.isdigit():
                self.jump_buf += key
                self.jump_to_number(self.jump_buf)
                self.status = ":" + self.jump_buf
                return True
            self.jump_active = False         # any other key leaves the prompt
            self.status = ""
            if key in ("ENTER", "ESC"):
                return True                  # ... and that key just closes it

        if key == "q":
            return False
        if key in ("UP", "k"):
            self.move(-1)
        elif key in ("DOWN", "j"):
            self.move(1)
        elif key in ("HOME", "g"):
            self.move_to(0)
        elif key in ("END", "G"):
            self.move_to(len(self.rows) - 1)
        elif key in ("LEFT", "h"):
            self.collapse()
        elif key in ("RIGHT", "l"):
            self.expand()
        elif key == "n":
            self.next_file()
        elif key == "p":
            self.prev_file()
        elif key == "/":
            if self.all_lines:
                self.mode = "filter"     # refine: filter the current lines
                self.finput = ""
                self._refresh_filter_view()
            else:
                self.status = "nothing to filter"
        elif key == "<":
            self.pop_filter()            # back up one level
        elif key == "\\":
            self.start_fresh()           # clear every filter
        elif key == "+":
            self.set_ctx(self.context + 1)
        elif key == "-":
            self.set_ctx(self.context - 1)
        elif len(key) == 1 and key.isdigit():
            self.set_ctx(int(key))       # 0-9: set the -U context directly
        elif key == ":":
            self.jump_active = True      # open the jump-to-number prompt
            self.jump_buf = ""
            self.status = ":"
        elif key == "r":
            self.reload(preserve=True)   # re-run git diff, keep our place
        elif key == "ENTER":
            self.open_current(screen)
        return True

    # -- rendering -----------------------------------------------------------
    @staticmethod
    def _line(text, cols, color):
        text = text + " " * max(0, cols - len(text))
        if color:
            text = color + text + RESET
        return text + CLEAR_EOL + "\r\n"

    def _row_text(self, row):
        indent = "  " * row["depth"]
        node = row["node"]
        if row["type"] == "folder":
            arrow = "▾" if row["expanded"] else "▸"
            if is_file_node(node):
                return "      " + indent + arrow + " " + node.seg + \
                    "  ({0})".format(len(node.children))
            return "      " + indent + arrow + " " + node.seg + "/"
        d = row["branch"]
        sign = "+" if d.kind == "add" else "-" if d.kind == "del" else " "
        text = d.text.expandtabs(8)
        lineno = d.line if d.openable else ""
        # "<jump>  <line>  <±><text>": the leading number is the :N jump target;
        # the sign marks add/remove (also colored, see _color_for).
        return "{0:>4}  {1:>5}  ".format(row["number"], lineno) + \
            indent + sign + text

    def _color_for(self, row):
        if not self.use_color:
            return ""
        if row["type"] == "folder":
            return BLUE if not is_file_node(row["node"]) else CYAN
        kind = row["branch"].kind
        if kind == "add":
            return GREEN
        if kind == "del":
            return RED
        return DIM                           # dim the surrounding context lines

    def _stack_crumb(self):
        """A breadcrumb of the view: the diff target, then each filter as +pat
        (narrow) or -pat (exclude), plus the in-progress filter in FILTER mode."""
        crumb = self.diff_label
        for f in self.filters:
            crumb += "  " + ("-" if f["neg"] else "+") + f["pat"]
        if self.mode == "filter":
            pending = self._parse_filter(self.finput)
            if pending:
                crumb += "  " + ("-" if pending["neg"] else "+") + pending["pat"] + "…"
        return crumb

    def _footer(self):
        if self.mode == "filter":
            label = "exclude" if self.finput.strip().startswith("!") else "filter"
            n = len(self.matches)
            return (" {0}: {1}    Enter add · ! to exclude · Esc cancel · "
                    "{2} line{3}".format(label, self.finput, n,
                                         "" if n == 1 else "s"))
        base = (" j/k move · n/p next/prev file · Enter open · / filter · "
                "< back · \\ whole · 0-9/± context · :N jump · r refresh · q quit")
        return base + ("    " + self.status if self.status else "")

    def render(self):
        cols, lines_h = term_size()
        area = max(1, lines_h - 4)

        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + area:
            self.top = self.cursor - area + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - area)))

        mode = "FILTER" if self.mode == "filter" else "BROWSE"
        ic = "  -i" if self.ignore_case else ""
        if self.matches:
            files = self._file_count()
            adds = sum(1 for m in self.matches if m.kind == "add")
            dels = sum(1 for m in self.matches if m.kind == "del")
            tally = "+{0} -{1} in {2} file{3}".format(
                adds, dels, files, "" if files == 1 else "s")
        else:
            tally = "no changes" if not self.all_lines else "no matches"
        header = " {0}    [{1}]    {2}    {3}  ·U{4}{5}".format(
            self.title, mode, self.repo_name, tally, self.context, ic)
        crumb = self._stack_crumb()
        if crumb:
            header += "    " + crumb

        out = [HOME, self._line(header[:cols], cols, BOLD if self.use_color else ""),
               self._line("─" * cols, cols, "")]

        window = self.rows[self.top:self.top + area]
        for i, row in enumerate(window):
            idx = self.top + i
            text = self._row_text(row)[:cols]
            if self.use_color and idx == self.cursor:
                out.append(self._line(text, cols, REVERSE))
            else:
                out.append(self._line(text, cols, self._color_for(row)))
        if not self.rows:
            hint = "  (no changes to show)" if not self.all_lines else \
                "  (no lines match)"
            out.append(self._line(hint, cols, ""))
        drawn = max(len(window), 0 if self.rows else 1)
        for _ in range(area - drawn):
            out.append(CLEAR_EOL + "\r\n")

        out.append(self._line("─" * cols, cols, ""))
        out.append(self._line(self._footer()[:cols], cols, ""))
        frame = "".join(out)
        if frame.endswith("\r\n"):
            frame = frame[:-2]    # no newline on the bottom row → no scroll bounce
        sys.stderr.write(frame + CLEAR_EOS)
        sys.stderr.flush()

    # -- main loop -----------------------------------------------------------
    def run(self, screen):
        while True:
            self.rebuild()
            self.render()
            try:
                key = read_key()
            except KeyboardInterrupt:
                return
            if not self.handle(key, screen):
                return


def main():
    parser = argparse.ArgumentParser(
        description="Interactively browse git diff, then open a line at its spot.",
        epilog="Any further arguments are passed straight to `git diff` "
               "(e.g. --staged, HEAD, a branch, -- path).")
    parser.add_argument("--version", action="version",
                        version="git-diff " + __version__)
    # Everything else is forwarded to git diff. parse_known_args lets options
    # like --staged through without us having to declare each one.
    args, diff_args = parser.parse_known_args()

    if not in_git_repo():
        sys.stderr.write("git-diff: not inside a Git repository.\n")
        return 1
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        sys.stderr.write("git-diff: needs an interactive terminal.\n")
        return 1

    toplevel = repo_toplevel()
    editor_argv, line_template, notices = load_config()

    browser = DiffBrowser(toplevel,
                          os.path.basename(toplevel.rstrip("/")) or toplevel,
                          diff_args, editor_argv, line_template, use_color=True)
    browser.reload()

    with Screen() as screen:
        browser.run(screen)

    for note in notices:
        sys.stderr.write("git-diff: " + note + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
