#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
r"""An interactive, full-screen front end for `git grep` -- type a pattern, see
every matching line gathered into a collapsible tree of files, then hit Enter to
jump straight to that line in your editor. Open as many hits as you like; the
browser stays put until you quit.

The picker is *modal*, in the spirit of vim:

  PATTERN mode (where you land with no argument)
    type                     the git grep pattern (a basic regex, as git grep
                             takes it)
    Enter                    run git grep and drop into BROWSE mode on the hits
    Up / Down                move the highlight through the current results
    Esc                      switch to BROWSE mode to navigate with j/k (clears
                             a no-match pattern; an empty prompt just switches)
    Backspace                edit the pattern
    Tab                      toggle case-insensitive (-i) matching

  BROWSE mode (where you land when you pass a pattern, or after Enter)
    j / k  or  Up / Down     move the highlight cursor
    g / G                    jump to the top / bottom
    n / p                    jump to the first line of the next / previous file
    h / Left                 hop up to the parent folder/file
    l / Right                expand / step into a folder or file
    Enter                    open the match under the cursor at its line
                             (on a folder/file row, fold it instead)
    r                        re-run the whole stack and refresh the results
                             (handy after editing files), keeping your place
    /                        refine: filter the current hits with a sub-grep
                             (push a level onto the stack)
    <                        back up one level (pop the last filter)
    \                        start fresh: clear the whole stack, empty prompt
    0-9                      set the context window to N lines (0 = none)
    + / -                    widen / narrow the context (+ goes past 9; you
                             can't narrow below the parent level's context)
    :N                       jump the cursor to line number N (the leading
                             number on each row); Enter/Esc or any move closes
                             the prompt, ':' again starts a new number
    Tab                      toggle case-insensitive (-i) and re-run
    q                        quit (Esc never quits -- it only navigates)

  FILTER mode (a sub-grep over the current hits; reached with / from BROWSE)
    type                     a pattern that narrows the visible lines *live*,
                             matched against the file path and the line text
    !pattern                 exclude: keep the lines that do NOT match
    Enter                    push this filter onto the stack
    Esc                      cancel without pushing
    Up / Down                move through the filtered hits

Because filters stack, you can drill down in steps that a single regex can't
express -- e.g. grep `error`, then `/` `handler` to narrow, then `/` `!_test.`
to drop the test files -- and `<` pops back up a level at a time while `\`
wipes the stack to start over. `r` re-runs the base grep and re-applies every
filter, so after editing you see the same view, refreshed.

Each level also carries a *context window*: 0-9 (or +/-) pull in that many
lines around every hit, read from the working tree. Those context lines join
the searchable set, so the next filter can match on something that merely sits
*near* an earlier hit -- and the next level can only widen the window, never
shrink it below its parent. Context lines are shown dimmed; the real hits keep
their normal weight, and every row is labelled with its :N jump number.

Matching lines are grouped under their file, and files nest in a **folder tree**
split on "/", so hits in `src/app/main.c` and `src/lib/parse.c` sit under a
`src/` folder you can collapse. Everything starts expanded so the matches are
visible at a glance.

The editor, and *how to open it at a line*, come from `.git-open-config` -- the
same TOML file git-open uses, living next to these scripts and gitignored:

    editor = "vim"
    line   = "+{line} {file}"

`line` is split shell-style with {file}/{line}/{column} substituted in, so it
adapts to any editor (`code -g` wants `{file}:{line}`, vim wants `+{line}`). The
file is opened by its full path, and git grep is run at the repository root, so
this works from any subdirectory.

One exception: when run inside the integrated terminal of VS Code, a JetBrains
IDE (CLion and friends), or Zed, opening a hit hands it to that already-running
editor -- through a `vscode://` URL or a `clion`/`zed` launcher -- so it lands
in a new tab there rather than spawning the configured editor. Those hand-offs
are fire-and-forget, so the browser stays up instead of stepping aside. The
detection and launching live in editor_ide, shared with git-open.

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

from branch_tui import (BLUE, BOLD, CLEAR_EOL, CLEAR_EOS, CYAN, HOME, RESET,
                        REVERSE, Node, TerminalSession, build_visible, git,
                        in_git_repo, read_key, splice_collapse, splice_expand,
                        term_size)
from editor_config import load_config, open_args
from editor_ide import detect_ide

__version__ = "1.0.0"

DIM = "\x1b[2m"                  # for context lines pulled in around a match

# One line in the result set. `name` is a unique key (file + line) that
# branch_tui's tree builder uses as the row id for cursor tracking. `anchor`
# is True for a real hit (a grep match or a filter survivor) and False for a
# context line pulled in around one; it defaults True so plain grep hits need
# not pass it.
Match = namedtuple("Match", "file line column text name anchor",
                   defaults=(True,))


# ─── git plumbing ────────────────────────────────────────────────────────────
def repo_toplevel():
    out = git(["rev-parse", "--show-toplevel"])
    return out.stdout.strip() if out.returncode == 0 else None


def run_grep(toplevel, query, ignore_case):
    """Run git grep for `query` at the repo root. Returns a list of Match (empty
    when nothing matched) or None when git grep itself errored (e.g. a bad
    regex). Uses -z so file/line/text are NUL-separated and parse cleanly even
    when paths contain colons."""
    args = ["-C", toplevel, "grep", "-n", "-I", "-z", "--no-color"]
    if ignore_case:
        args.append("-i")
    args += ["-e", query]
    out = git(args)
    if out.returncode > 1:          # 0 = matches, 1 = none, >1 = real error
        return None
    matches = []
    for record in out.stdout.split("\n"):
        if not record:
            continue
        parts = record.split("\0")
        if len(parts) < 3:
            continue
        path, lineno, text = parts[0], parts[1], "\0".join(parts[2:])
        try:
            n = int(lineno)
        except ValueError:
            continue
        matches.append(Match(path, n, 1, text, "{0}\x00{1}".format(path, n)))
    return matches


# ─── grep result tree ────────────────────────────────────────────────────────
def build_grep_tree(matches):
    """Weave matches into a Node tree: directories and files are folders, each
    matching line is a leaf under its file. Returns (root, folder_paths) where
    folder_paths is every expandable node, so the caller can open them all by
    default. Match leaves sort by line number via a zero-padded key."""
    root = Node("", "")
    folders = set()
    for m in matches:
        parts = m.file.split("/")
        node = root
        for i, seg in enumerate(parts):
            path = "/".join(parts[:i + 1])
            child = node.children.get(seg)
            if child is None:
                child = Node(seg, path)
                node.children[seg] = child
            node = child
            folders.add(path)       # every directory, and the file itself
        key = "{0:020d}".format(m.line)
        leaf = node.children.get(key)
        if leaf is None:
            leaf = Node(key, node.path + "\x00" + key)
            node.children[key] = leaf
        leaf.branch = m
    return root, folders


def is_file_node(node):
    """A file node is a folder whose children are match leaves (not subfolders)."""
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


# ─── the grep browser ────────────────────────────────────────────────────────
class GrepBrowser(object):
    title = "git-grep"

    def __init__(self, toplevel, repo_name, editor_argv, line_template, use_color):
        self.toplevel = toplevel
        self.repo_name = repo_name
        self.editor_argv = editor_argv
        self.line_template = line_template
        self.use_color = use_color
        self.ide = detect_ide()     # editor whose terminal we're in, or None:
                                    # hand hits to it rather than spawning one

        self.query = ""
        self.ignore_case = False
        self.mode = "pattern"       # "pattern" | "filter" | "browse"
        self.base_matches = []      # raw git grep hits (the base, level 0)
        self.base_ctx = 0           # context lines (±N) around the base hits
        self.filters = []           # stack of {"pat","neg","ctx"} refinements
        self.finput = ""            # the filter being typed in FILTER mode
        self.matches = []           # the displayed set: anchors + their context
        self._linecache = {}        # file -> its lines, for pulling context
        self.root = None
        self.expanded = set()
        self.jump_active = False    # in the ':' jump-to-number prompt?
        self.jump_buf = ""          # digits typed there
        self.cursor = 0
        self.cur_id = None
        self.top = 0
        self.rows = []
        self.status = ""
        self.dirty = True           # rows need rebuilding before the next render
        self._tally = (0, 0)        # cached (hits, files) over self.matches

    # -- running git grep ----------------------------------------------------
    def search(self, preserve=False):
        """Run the base git grep for the current query, then (re)apply the whole
        filter stack on top. Returns True when something is left to browse. With
        preserve=True (a manual refresh) keep the cursor on the same match when
        it still exists, instead of snapping back to the first hit."""
        if not self.query:
            return False
        self._linecache = {}       # files may have changed since last run
        base = run_grep(self.toplevel, self.query, self.ignore_case)
        if base is None:
            self.base_matches = []
            self._show([])
            self.status = "git grep: bad pattern"
            return False
        self.base_matches = base
        if not base:
            self._show([])
            self.status = "no matches for /{0}".format(self.query)
            return False           # stay put so the pattern stays editable
        self._show(self._compute(), preserve=preserve)
        self.mode = "browse"
        if preserve:
            self.status = "refreshed"
        elif not self.matches:
            self.status = "no lines left after filters"
        else:
            self.status = ""
        return bool(self.matches)

    # -- the filter stack ----------------------------------------------------
    @staticmethod
    def _parse_filter(raw):
        """Turn typed text into a filter dict, or None if it's empty. A leading
        '!' means exclude (keep the lines that do NOT match)."""
        raw = raw.strip()
        if not raw:
            return None
        neg = raw.startswith("!")
        pat = raw[1:].strip() if neg else raw
        if not pat:
            return None
        return {"pat": pat, "neg": neg}

    def _file_lines(self, relpath):
        """The lines of a tracked file (cached), for pulling context around a
        hit. Reads the working tree -- the same thing git grep searched."""
        cached = self._linecache.get(relpath)
        if cached is not None:
            return cached
        try:
            with open(os.path.join(self.toplevel, relpath),
                      "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
            if lines and lines[-1] == "":
                lines.pop()             # drop the empty tail from a final newline
        except OSError:
            lines = []
        self._linecache[relpath] = lines
        return lines

    def _expand(self, anchors, n):
        """Grow a list of anchor matches into the set that includes ±n context
        lines around each. Returns a dict keyed by (file, line); anchors are
        flagged anchor=True (even if they arrived as context), context lines
        anchor=False. Overlapping windows are merged, anchors winning."""
        result = {}
        keys = {(m.file, m.line) for m in anchors}
        for m in anchors:
            result[(m.file, m.line)] = m._replace(anchor=True)
        if n > 0:
            for m in anchors:
                lines = self._file_lines(m.file)
                if not lines:
                    continue
                for ln in range(max(1, m.line - n), min(len(lines), m.line + n) + 1):
                    key = (m.file, ln)
                    if key in keys or key in result:
                        continue
                    result[key] = Match(m.file, ln, 1, lines[ln - 1],
                                        "{0}\x00{1}".format(m.file, ln), False)
        return result

    @staticmethod
    def _ordered(candidates):
        """A (file, line)-keyed dict flattened into display order."""
        return [candidates[k] for k in sorted(candidates)]

    def _compute(self, extra=None, extra_ctx=0):
        """The heart of the stack: start from the base hits, expand by the base
        context, then for each filter keep the candidate lines it matches (path
        + text) and re-expand by that level's context. `extra` previews one more
        filter (with `extra_ctx`) without committing it. Returns the display
        list. A context line that a filter matches becomes an anchor, so you can
        keep drilling on something that only appeared *near* an earlier hit."""
        flags = re.IGNORECASE if self.ignore_case else 0
        candidates = self._expand(self.base_matches, self.base_ctx)
        chain = list(self.filters)
        if extra is not None:
            chain.append({"pat": extra["pat"], "neg": extra["neg"],
                          "ctx": extra_ctx})
        for f in chain:
            try:
                rx = re.compile(f["pat"], flags)
            except re.error:
                rx = re.compile(re.escape(f["pat"]), flags)
            anchors = []
            for m in self._ordered(candidates):
                hit = bool(rx.search(m.file + "\t" + m.text))
                if (not hit) if f["neg"] else hit:
                    anchors.append(m)
            candidates = self._expand(anchors, f["ctx"])
        return self._ordered(candidates)

    def _show(self, matches, preserve=False):
        """Display a given list of matches: rebuild the tree and place the
        cursor (on the first hit, or the prior one when preserve=True)."""
        self.matches = matches
        # Tally the whole set once here rather than on every render.
        hits = sum(1 for m in matches if m.anchor)
        self._tally = (hits, len({m.file for m in matches}))
        if not matches:
            self.root, self.expanded, self.rows = None, set(), []
            self.cur_id, self.cursor = None, 0
            self.dirty = False
            return
        self.root, self.expanded = build_grep_tree(matches)
        if not preserve:
            self.cur_id = None
        self.rebuild()
        if not preserve or self.cur_id is None:
            self.cursor = self._first_match_index()
        self._sync_id()

    def _refresh_filter_view(self):
        """While composing a filter, show the live narrowed result -- the
        committed stack plus the in-progress pattern, which inherits the current
        top context window."""
        pending = self._parse_filter(self.finput)
        self._show(self._compute(extra=pending, extra_ctx=self._current_ctx()))

    def push_filter(self):
        """Commit the typed filter onto the stack and return to BROWSE. A new
        level inherits the current context window as its starting (and minimum)
        width."""
        pending = self._parse_filter(self.finput)
        self.finput = ""
        self.mode = "browse"
        if pending is None:
            self._show(self._compute())                # nothing typed: restore
            return
        pending["ctx"] = self._current_ctx()           # inherit parent context
        self.filters.append(pending)
        self._show(self._compute())
        n = len(self.matches)
        self.status = "{0} {1} -> {2} line{3}".format(
            "exclude" if pending["neg"] else "filter", pending["pat"],
            n, "" if n == 1 else "s")

    def pop_filter(self):
        """Back up one level: drop the most recent filter (and its context)."""
        if not self.filters:
            self.status = "already at the base grep"
            return
        f = self.filters.pop()
        self._show(self._compute())
        self.status = "popped {0}{1}".format("-" if f["neg"] else "+", f["pat"])

    def start_fresh(self):
        """Clear the entire stack -- base grep, every filter, all context -- and
        drop back to an empty PATTERN prompt to grep for something new."""
        self.query = ""
        self.filters = []
        self.base_matches = []
        self.base_ctx = 0
        self.finput = ""
        self._linecache = {}
        self._show([])
        self.mode = "pattern"
        self.status = ""

    # -- the context window --------------------------------------------------
    def _current_ctx(self):
        """The context width of the top level -- the one +/-/digits adjust."""
        return self.filters[-1]["ctx"] if self.filters else self.base_ctx

    def _ctx_floor(self):
        """You can't narrow below the parent level's context (the base's floor
        is 0). This keeps each refinement at least as wide as the one above."""
        if not self.filters:
            return 0
        if len(self.filters) == 1:
            return self.base_ctx
        return self.filters[-2]["ctx"]

    def set_ctx(self, value):
        """Set the top level's context window, clamped to [floor, 999], and
        refresh the view in place (keeping the cursor)."""
        value = max(self._ctx_floor(), min(value, 999))
        if self.filters:
            self.filters[-1]["ctx"] = value
        else:
            self.base_ctx = value
        self._show(self._compute(), preserve=True)
        self.status = "context ±{0}".format(value)

    # -- building the visible rows ------------------------------------------
    def rebuild(self):
        # O(all matches); the run loop only calls it when `dirty` marks a real
        # structural change, and expand/collapse splice the rows in place.
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
        self.dirty = False

    def _first_match_index(self):
        for i, row in enumerate(self.rows):
            if row["type"] == "branch":
                return i
        return 0

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
            splice_expand(self.rows, file_idx, self.expanded)   # file_idx unmoved
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
            splice_expand(self.rows, self.cursor, self.expanded)

    def collapse(self):
        if not self.rows:
            return
        row = self.rows[self.cursor]
        if row["type"] == "folder" and row["expanded"]:
            self.expanded.discard(row["node"].path)
            splice_collapse(self.rows, self.cursor)
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
        elif self.mode == "browse" and self.query:
            self.search()                  # re-run base + filters with the flag

    # -- opening a match -----------------------------------------------------
    def open_current(self, screen):
        row = self.rows[self.cursor] if self.rows else None
        if row is None:
            return
        if row["type"] == "folder":
            if row["expanded"]:
                self.expanded.discard(row["node"].path)
                splice_collapse(self.rows, self.cursor)
            else:
                self.expanded.add(row["node"].path)
                splice_expand(self.rows, self.cursor, self.expanded)
            return
        m = row["branch"]
        path = os.path.join(self.toplevel, m.file)
        if self.ide:
            # Inside an editor's integrated terminal: hand the hit to that
            # running editor (a URL or CLI launcher that returns at once), so
            # the browser stays up -- no suspend.
            try:
                self.ide.open(path, m.line, m.column)
                self.status = "opened {0}:{1} in {2}".format(
                    m.file, m.line, self.ide.label)
            except FileNotFoundError:
                self.status = self.ide.open_error()
            except OSError as exc:
                self.status = "could not open: {0}".format(exc)
            return
        argv = open_args(self.editor_argv, self.line_template, path,
                         line=m.line, column=m.column)
        screen.suspend()
        try:
            subprocess.call(argv)
            self.status = "opened {0}:{1}".format(m.file, m.line)
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
        if self.mode == "pattern":
            return self._handle_pattern(key)
        if self.mode == "filter":
            return self._handle_filter(key)
        return self._handle_browse(key, screen)

    def _handle_pattern(self, key):
        # PATTERN mode: type the grep pattern (Enter runs git grep). The arrows
        # still move through whatever the last search found, and Esc hands off to
        # BROWSE mode so j/k navigation is always one key away.
        if key == "ESC":
            if self.rows:
                self.mode = "browse"     # go navigate the current results
                if self.rows[self.cursor]["type"] == "folder":
                    self.cursor = self._first_match_index()
                    self._sync_id()
            elif self.query:
                self.query = ""          # clear a no-match query to retype
            else:
                self.mode = "browse"     # empty prompt: drop into BROWSE (q quits)
        elif key == "ENTER":
            self.search()
        elif key == "BACKSPACE":
            self.query = self.query[:-1]
        elif key == "UP":
            self.move(-1)
        elif key == "DOWN":
            self.move(1)
        elif key == "HOME":
            self.move_to(0)
        elif key == "END":
            self.move_to(len(self.rows) - 1)
        elif len(key) == 1 and key >= " ":
            self.query += key
        return True

    def _handle_filter(self, key):
        # FILTER mode: type a sub-grep over the current hits; the list narrows
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
            if self.base_matches:
                self.mode = "filter"     # refine: filter the current hits
                self.finput = ""
                self._refresh_filter_view()
            else:
                self.status = "nothing to filter yet"
        elif key == "<":
            self.pop_filter()            # back up one level
        elif key == "\\":
            self.start_fresh()           # clear the whole stack
        elif key == "+":
            self.set_ctx(self._current_ctx() + 1)
        elif key == "-":
            self.set_ctx(self._current_ctx() - 1)
        elif len(key) == 1 and key.isdigit():
            self.set_ctx(int(key))       # 0-9: set context width directly
        elif key == ":":
            self.jump_active = True      # open the jump-to-number prompt
            self.jump_buf = ""
            self.status = ":"
        elif key == "r":
            self.search(preserve=True)   # re-run the stack, keep our place
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
        m = row["branch"]
        text = m.text.expandtabs(8).strip()
        # "<jump>  <fileline>  <text>": the leading number is the :N jump target;
        # context lines are dimmed (see _color_for) rather than marked.
        return "{0:>4}  {1:>5}  ".format(row["number"], m.line) + indent + text

    def _color_for(self, row):
        if not self.use_color:
            return ""
        if row["type"] == "folder":
            return BLUE if not is_file_node(row["node"]) else CYAN
        if not row["branch"].anchor:
            return DIM                       # dim the surrounding context lines
        return ""

    def _stack_crumb(self):
        """A breadcrumb of the grep stack: the base query, then each filter as
        +pat (narrow) or -pat (exclude), each tagged with its context width when
        non-zero (±N), plus the in-progress filter in FILTER mode."""
        if not self.query:
            return ""

        def tag(c):
            return " ±{0}".format(c) if c else ""

        crumb = self.query + tag(self.base_ctx)
        for f in self.filters:
            crumb += "  " + ("-" if f["neg"] else "+") + f["pat"] + tag(f["ctx"])
        if self.mode == "filter":
            pending = self._parse_filter(self.finput)
            if pending:
                crumb += "  " + ("-" if pending["neg"] else "+") + pending["pat"] + "…"
        return crumb

    def _footer(self):
        if self.mode == "pattern":
            if self.query:
                hint = ("↑↓ move · Esc navigate" if self.rows else "Esc clear")
                return (" /{0}    Enter grep · {1} · Tab -i · ^C quit"
                        .format(self.query, hint))
            return " Type a pattern, Enter to grep    Tab ignore-case · Esc browse · ^C quit"
        if self.mode == "filter":
            label = "exclude" if self.finput.strip().startswith("!") else "filter"
            n = len(self.matches)
            return (" {0}: {1}    Enter add · ! to exclude · Esc cancel · "
                    "{2} line{3}".format(label, self.finput, n,
                                         "" if n == 1 else "s"))
        base = (" j/k move · n/p next/prev file · Enter open · / filter · "
                "< back · \\ fresh · 0-9/± context · :N jump · r refresh · q quit")
        return base + ("    " + self.status if self.status else "")

    def render(self):
        cols, lines_h = term_size()
        area = max(1, lines_h - 4)

        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + area:
            self.top = self.cursor - area + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - area)))

        mode = {"pattern": "PATTERN", "filter": "FILTER",
                "browse": "BROWSE"}[self.mode]
        ic = "  -i" if self.ignore_case else ""
        if self.matches:
            hits, files = self._tally
            ctx = len(self.matches) - hits
            tally = "{0} match{1}".format(hits, "" if hits == 1 else "es")
            if ctx:
                tally += " +{0} ctx".format(ctx)
            tally += " in {0} file{1}".format(files, "" if files == 1 else "s")
        else:
            tally = "type a pattern" if not self.query else "no matches"
        header = " {0}    [{1}]    {2}    {3}{4}".format(
            self.title, mode, self.repo_name, tally, ic)
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
            hint = "  (matches appear here)" if not self.matches else ""
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
            if self.dirty:              # only reflatten when the tree/expansion
                self.rebuild()          # changed -- not on plain cursor moves
            self.render()
            try:
                key = read_key()
            except KeyboardInterrupt:
                return
            if not self.handle(key, screen):
                return


def main():
    parser = argparse.ArgumentParser(
        description="Interactively git grep, then open a hit at its line.")
    parser.add_argument("pattern", nargs="?",
                        help="pattern to grep for; omit to start at an empty prompt")
    parser.add_argument("-i", "--ignore-case", action="store_true",
                        help="start with case-insensitive matching")
    parser.add_argument("--version", action="version",
                        version="git-grep " + __version__)
    args = parser.parse_args()

    if not in_git_repo():
        sys.stderr.write("git-grep: not inside a Git repository.\n")
        return 1
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        sys.stderr.write("git-grep: needs an interactive terminal.\n")
        return 1

    toplevel = repo_toplevel()
    editor_argv, line_template, notices = load_config()

    browser = GrepBrowser(toplevel,
                          os.path.basename(toplevel.rstrip("/")) or toplevel,
                          editor_argv, line_template, use_color=True)
    browser.ignore_case = args.ignore_case
    if args.pattern:
        browser.query = args.pattern
        browser.search()               # lands in BROWSE on hits, PATTERN if none

    with Screen() as screen:
        browser.run(screen)

    for note in notices:
        sys.stderr.write("git-grep: " + note + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
