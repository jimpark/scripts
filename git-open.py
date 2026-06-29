#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""An interactive, full-screen file *finder* for Git -- type a regular
expression, watch the matching tracked files arrange themselves into a
collapsible folder tree, then hit Enter to open the one you want in your editor.
Open as many as you like; the picker stays put until you quit.

The picker is *modal*, in the spirit of vim:

  PATTERN mode (where you land with no argument) -- the "insert" mode
    type                     a regular expression; the file list filters live
    Up / Down                move the highlight through the matches
    Enter  Tab  or  Esc      switch to BROWSE mode to navigate with j/k
                             (Esc on an empty prompt quits)
    Backspace                edit the expression

  BROWSE mode (where you land when you pass a pattern, or after Enter/Tab/Esc)
    j / k  or  Up / Down     move the highlight cursor
    g / G                    jump to the top / bottom
    h / Left                 hop up to the parent folder
    l / Right                expand / step into a folder
    <digits>                 jump the cursor to a file by its number
    Enter                    open the file under the cursor (folder: fold it)
    Tab                      return to PATTERN mode to edit the expression
    /                        clear the pattern and start a fresh search
    q / Esc                  quit

Tracked file paths are split on "/" into a **folder tree**, so `src/app/main.c`
and `src/app/util.c` tuck under a `src/ > app/` folder. While a pattern is
active every folder that holds a match is shown expanded, so nothing hides.

The editor is whatever you put in `.git-open-config`, a TOML file that lives
next to this script and is gitignored, so you can set it to whatever you like:

    editor = "code -g"

The command is split like a shell line and the chosen file's path is appended as
the final argument. With no `editor` key (or no config file) git-open falls back
to $VISUAL, then $EDITOR, then a platform default. The first time you run it the
config file is created for you with examples in the comments. This is the same
config that git-grep uses; git-open simply ignores the `line` key.

Files come from `git ls-files` run at the repository root, so the whole repo is
searchable no matter which subdirectory you launch from, and the file opens by
its full path.

Runs on macOS, Linux, and Windows using only the standard library (raw terminal
mode + ANSI escapes; no curses, no third-party packages). Borrows its folder
tree, key input, and terminal handling from the neighbouring branch_tui module,
and its editor handling from editor_config.

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

from branch_tui import (BLUE, BOLD, CLEAR_EOL, CLEAR_EOS, HOME, RESET, REVERSE,
                        TerminalSession, build_tree, build_visible, git,
                        in_git_repo, read_key, term_size)
from editor_config import load_config

__version__ = "1.0.0"

# A tracked file, shaped just enough for branch_tui's tree builders, which only
# ever look at `.name` -- here the repo-root-relative path, split on "/".
File = namedtuple("File", "name")


# ─── git plumbing ────────────────────────────────────────────────────────────
def repo_toplevel():
    out = git(["rev-parse", "--show-toplevel"])
    return out.stdout.strip() if out.returncode == 0 else None


def list_files(toplevel):
    """Every tracked file in the repo, as File items, regardless of cwd."""
    out = git(["-C", toplevel, "ls-files"])
    return [File(line) for line in out.stdout.splitlines() if line]


# ─── terminal session that can step aside for the editor ─────────────────────
class Screen(TerminalSession):
    """A TerminalSession that can suspend itself (leave raw mode + the alternate
    screen) while an interactive editor runs, then resume cleanly afterwards."""

    def suspend(self):
        self.__exit__(None, None, None)

    def resume(self):
        self.__enter__()


# ─── the file finder ─────────────────────────────────────────────────────────
class FileFinder(object):
    title = "git-open"

    def __init__(self, files, repo_name, editor_argv, toplevel, use_color):
        self.files = files
        self.repo_name = repo_name
        self.editor_argv = editor_argv
        self.toplevel = toplevel
        self.use_color = use_color

        self.query = ""
        self.filt = None            # compiled query, or None
        self.bad_regex = False      # query was invalid; matched literally
        self.mode = "pattern"       # "pattern" | "browse"
        self.expanded = set()       # folder paths opened by hand
        self.pending = ""           # digits typed for number-jump
        self.cursor = 0
        self.cur_id = None          # row id under the cursor, to survive rebuilds
        self.top = 0                # scroll offset
        self.rows = []
        self.status = ""            # transient one-line message in the footer

    # -- building the visible rows ------------------------------------------
    def _compile_filter(self):
        if not self.query:
            self.filt, self.bad_regex = None, False
            return
        try:
            self.filt = re.compile(self.query, re.IGNORECASE)
            self.bad_regex = False
        except re.error:
            self.filt = re.compile(re.escape(self.query), re.IGNORECASE)
            self.bad_regex = True

    def rebuild(self):
        self._compile_filter()
        # Nothing is shown until there's a pattern -- the empty screen is the
        # prompt. With a pattern, every folder holding a match shows expanded.
        if self.query:
            root = build_tree(self.files)
            self.rows = build_visible(root, self.expanded, self.filt)
        else:
            self.rows = []

        if self.cur_id is not None:
            for i, row in enumerate(self.rows):
                if row["id"] == self.cur_id:
                    self.cursor = i
                    break
            else:
                self.cursor = self._first_file_index()
        else:
            self.cursor = self._first_file_index()
        self.cursor = max(0, min(self.cursor, len(self.rows) - 1)) if self.rows else 0
        self.cur_id = self.rows[self.cursor]["id"] if self.rows else None

    def _first_file_index(self):
        for i, row in enumerate(self.rows):
            if row["type"] == "branch":
                return i
        return 0

    def _match_count(self):
        return sum(1 for r in self.rows if r["type"] == "branch")

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

    # -- opening a file ------------------------------------------------------
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
        path = os.path.join(self.toplevel, row["branch"].name)
        screen.suspend()
        try:
            subprocess.call(self.editor_argv + [path])
            self.status = "opened " + row["branch"].name
        except FileNotFoundError:
            self.status = "editor not found: " + " ".join(self.editor_argv)
        except OSError as exc:
            self.status = "could not open: {0}".format(exc)
        finally:
            screen.resume()

    # -- key handling --------------------------------------------------------
    def handle(self, key, screen):
        """Return False to quit, True to keep going."""
        if key in ("", "DELETE"):
            return True
        if key == "EOF":
            return False
        self.status = ""
        if self.mode == "pattern":
            return self._handle_pattern(key, screen)
        return self._handle_browse(key, screen)

    def _to_browse(self):
        """Leave typing and go navigate the results with j/k. Land on a file row
        (not a folder header) so the keys act on something selectable."""
        self.mode = "browse"
        if self.rows and self.rows[self.cursor]["type"] == "folder":
            self.cursor = self._first_file_index()
            self._sync_id()

    def _handle_pattern(self, key, screen):
        # PATTERN mode is the "insert" mode: you type a regex, the list filters
        # live, the arrows move the highlight, and Enter/Tab/Esc hand off to
        # BROWSE mode for vim-style j/k navigation.
        if key in ("ENTER", "ESC", "TAB"):
            if self.rows:
                self._to_browse()        # commit the filter, start navigating
            elif self.query:
                self.query = ""          # a no-match query: clear it to retype
                self.cur_id = None
            elif key == "ESC":
                return False             # empty prompt + Esc: quit
        elif key == "BACKSPACE":
            self.query = self.query[:-1]
            self.cur_id = None
        elif key == "UP":
            self.move(-1)
        elif key == "DOWN":
            self.move(1)
        elif key == "HOME":
            self.move_to(0)
        elif key == "END":
            self.move_to(len(self.rows) - 1)
        elif len(key) == 1 and key >= " ":
            self.query += key            # j, k, q, etc. are just regex text here
            self.cur_id = None
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
        elif key == "TAB":
            self.mode = "pattern"        # back to editing the regex
        elif key == "/":
            self.query = ""              # clear it and start a fresh search
            self.cur_id = None
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
        if row["type"] == "folder":
            arrow = "▾" if row["expanded"] else "▸"
            return "     " + indent + arrow + " " + row["node"].seg + "/"
        return "{0:>3}  ".format(row["number"]) + indent + row["node"].seg

    def _footer(self):
        if self.mode == "pattern":
            if self.query:
                tail = "  (literal)" if self.bad_regex else ""
                return (" /{0}{1}    ↑↓ move · Enter/Tab/Esc navigate · ^C quit"
                        .format(self.query, tail))
            return " Type a regex to find files    Enter/Tab/Esc navigate · ^C quit"
        base = " j/k ↑↓ move · g/G ends · Enter open · Tab edit · / new · q quit"
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
        count = self._match_count()
        tally = "{0} match{1}".format(count, "" if count == 1 else "es") \
            if self.query else "type a pattern"
        header = " {0}    [{1}]    {2}    {3}".format(
            self.title, mode, self.repo_name, tally)

        out = [HOME, self._line(header[:cols], cols, BOLD if self.use_color else ""),
               self._line("─" * cols, cols, "")]

        window = self.rows[self.top:self.top + area]
        for i, row in enumerate(window):
            idx = self.top + i
            text = self._row_text(row)[:cols]
            if self.use_color and idx == self.cursor:
                out.append(self._line(text, cols, REVERSE))
            else:
                color = BLUE if (self.use_color and row["type"] == "folder") else ""
                out.append(self._line(text, cols, color))
        if not self.rows:
            hint = "  (matches appear here)" if self.query else ""
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
        description="Interactively find a tracked file by regex and open it.")
    parser.add_argument("pattern", nargs="?",
                        help="regex to match against file paths; omit to start "
                             "with an empty prompt")
    parser.add_argument("--version", action="version",
                        version="git-open " + __version__)
    args = parser.parse_args()

    if not in_git_repo():
        sys.stderr.write("git-open: not inside a Git repository.\n")
        return 1
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        sys.stderr.write("git-open: needs an interactive terminal.\n")
        return 1

    toplevel = repo_toplevel()
    files = list_files(toplevel)
    if not files:
        sys.stderr.write("git-open: no tracked files in this repository.\n")
        return 0

    editor_argv, _line_template, notices = load_config()

    finder = FileFinder(files, os.path.basename(toplevel.rstrip("/")) or toplevel,
                        editor_argv, toplevel, use_color=True)
    if args.pattern:
        finder.query = args.pattern
        finder.mode = "browse"

    with Screen() as screen:
        finder.run(screen)

    for note in notices:
        sys.stderr.write("git-open: " + note + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
