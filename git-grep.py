#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""An interactive, full-screen front end for `git grep` -- type a pattern, see
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
                             a no-match pattern, or quits at an empty prompt)
    Backspace                edit the pattern
    Tab                      toggle case-insensitive (-i) matching

  BROWSE mode (where you land when you pass a pattern, or after Enter)
    j / k  or  Up / Down     move the highlight cursor
    g / G                    jump to the top / bottom
    h / Left                 hop up to the parent folder/file
    l / Right                expand / step into a folder or file
    <digits>                 jump the cursor to a match by its number
    Enter                    open the match under the cursor at its line
                             (on a folder/file row, fold it instead)
    Tab                      toggle case-insensitive (-i) and re-run
    /                        return to PATTERN mode to grep for something else
    q / Esc                  quit

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

Runs on macOS, Linux, and Windows using only the standard library (raw terminal
mode + ANSI escapes; no curses, no third-party packages). Borrows its tree, key
input, and terminal handling from branch_tui, and its editor handling from
editor_config.

Exit status:
    0   you quit normally (whether or not you opened anything)
    1   not inside a Git repository, or not an interactive terminal
"""

import argparse
import os
import subprocess
import sys
from collections import namedtuple

from branch_tui import (BLUE, BOLD, CLEAR_EOL, CLEAR_EOS, CYAN, HOME, RESET,
                        REVERSE, Node, TerminalSession, build_visible, git,
                        in_git_repo, read_key, term_size)
from editor_config import load_config, open_args

__version__ = "1.0.0"

# One matching line from git grep. `name` is a unique key (file + line) that
# branch_tui's tree builder uses as the row id for cursor tracking.
Match = namedtuple("Match", "file line column text name")


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

        self.query = ""
        self.ignore_case = False
        self.mode = "pattern"       # "pattern" | "browse"
        self.matches = []
        self.root = None
        self.expanded = set()
        self.pending = ""           # digits typed for number-jump
        self.cursor = 0
        self.cur_id = None
        self.top = 0
        self.rows = []
        self.status = ""

    # -- running git grep ----------------------------------------------------
    def search(self):
        """Run git grep for the current query and rebuild the tree. Returns True
        when there were matches (and switches to BROWSE), False otherwise."""
        if not self.query:
            return False
        matches = run_grep(self.toplevel, self.query, self.ignore_case)
        if matches is None:
            self.matches, self.root, self.expanded = [], None, set()
            self.status = "git grep: bad pattern"
            return False
        self.matches = matches
        if not matches:
            self.root, self.expanded = None, set()
            self.status = "no matches for /{0}".format(self.query)
            return False
        self.root, self.expanded = build_grep_tree(matches)
        self.status = ""
        self.cur_id = None
        self.mode = "browse"
        self.rebuild()
        self.cursor = self._first_match_index()
        self._sync_id()
        return True

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
        self.pending = ""

    def move_to(self, index):
        if self.rows:
            self.cursor = max(0, min(index, len(self.rows) - 1))
            self._sync_id()
        self.pending = ""

    def jump_to_number(self):
        if not self.pending:
            return
        n = int(self.pending)
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
        if self.mode == "browse" and self.query:
            self.search()              # re-run with the new flag

    # -- opening a match -----------------------------------------------------
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
        m = row["branch"]
        path = os.path.join(self.toplevel, m.file)
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
                return False             # empty prompt + Esc: quit
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

    def _handle_browse(self, key, screen):
        if key in ("q", "ESC"):
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
        elif key == "/":
            self.mode = "pattern"
        elif key == "ENTER":
            self.open_current(screen)
        elif key == "BACKSPACE":
            self.pending = self.pending[:-1]
            self.jump_to_number()
        elif len(key) == 1 and key.isdigit():
            self.pending += key
            self.jump_to_number()
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
        return "{0:>6}  ".format(m.line) + indent + text

    def _color_for(self, row):
        if not self.use_color:
            return ""
        if row["type"] == "folder":
            return BLUE if not is_file_node(row["node"]) else CYAN
        return ""

    def _footer(self):
        if self.mode == "pattern":
            if self.query:
                hint = ("↑↓ move · Esc navigate" if self.rows else "Esc clear")
                return (" /{0}    Enter grep · {1} · Tab -i · ^C quit"
                        .format(self.query, hint))
            return " Type a pattern, Enter to grep    Tab ignore-case · Esc/^C quit"
        base = " j/k ↑↓ move · Enter open · / search · Tab -i · q quit"
        return base + ("    " + self.status if self.status else "")

    def render(self):
        cols, lines_h = term_size()
        area = max(1, lines_h - 4)

        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + area:
            self.top = self.cursor - area + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - area)))

        mode = "PATTERN" if self.mode == "pattern" else "BROWSE"
        ic = "  -i" if self.ignore_case else ""
        if self.matches:
            n, m = len(self.matches), self._file_count()
            tally = "{0} match{1} in {2} file{3}".format(
                n, "" if n == 1 else "es", m, "" if m == 1 else "s")
        else:
            tally = "type a pattern" if not self.query else "no matches"
        header = " {0}    [{1}]    {2}    {3}{4}".format(
            self.title, mode, self.repo_name, tally, ic)

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
        sys.stderr.write("".join(out) + CLEAR_EOS)
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
