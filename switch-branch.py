#!/usr/bin/env python3
"""An interactive, full-screen branch switcher for Git -- pick a branch the way
you would in fzf or lazygit, then it checks it out.

The picker is *modal*, in the spirit of vim:

  NORMAL mode (the default)
    j / k  or  Up / Down     move the highlight cursor
    g / G                    jump to the top / bottom
    h / Left                 collapse the folder (or hop to the parent folder)
    l / Right                expand the folder (or descend into it)
    <digits> then Enter      select a branch by its number (the cursor follows
                             along as you type, so 12<Enter> lands on branch 12)
    Enter                    expand/collapse a folder, or switch to a branch
    /                        enter FILTER mode
    Tab  (or r)              toggle remote branches in / out of the list
    q / Esc                  quit without switching

  FILTER mode (entered with /)
    type                     a regular expression that filters the branch names
    Up / Down                move the cursor among the matches
    Enter                    switch to the highlighted branch
    Backspace                edit the expression
    Esc                      clear the filter and return to NORMAL
    Tab                      toggle remote branches

Branch names are split on "/" into a collapsible **folder tree**, so
`feature/login` and `feature/logout` tuck under a `feature/` folder. Folders
start collapsed; expand them on demand, or just start typing a filter -- a
filter auto-expands every folder that contains a match.

Press Tab (or `r`) to fold in **remote** branches. They nest under their remote
as a folder (`origin/ > feature/ > login`). Selecting a remote branch that has
no local counterpart creates a local tracking branch and switches to it
(`git switch -c <name> --track <remote>/<name>`); if a local branch of that name
already exists, it just switches to the local one.

Runs on macOS, Linux, and Windows using only the standard library (raw terminal
mode + ANSI escapes; no curses, no third-party packages).

Exit status:
    0   a branch was switched, or you quit without choosing
    1   not inside a Git repository, not an interactive terminal, or the
        underlying `git switch` failed
"""

import argparse
import os
import re
import subprocess
import sys
from collections import namedtuple

__version__ = "1.0.0"

# A single branch the picker can switch to.
#   name        full display name (local: "feature/x"; remote: "origin/feature/x")
#   kind        "local" or "remote"
#   ref         the ref to hand to git (same as name)
#   is_current  True for the currently checked-out branch
#   local_name  for a remote, the local branch name to create (prefix stripped)
Branch = namedtuple("Branch", "name kind ref is_current local_name")

# ─── ANSI escapes ────────────────────────────────────────────────────────────
CSI = "\x1b["
ALT_ON, ALT_OFF = CSI + "?1049h", CSI + "?1049l"   # alternate screen buffer
HIDE_CUR, SHOW_CUR = CSI + "?25l", CSI + "?25h"
HOME = CSI + "H"
CLEAR_EOL, CLEAR_EOS = CSI + "K", CSI + "J"
REVERSE, RESET = CSI + "7m", CSI + "0m"
GREEN, BLUE, CYAN, BOLD = CSI + "32m", CSI + "1;34m", CSI + "36m", CSI + "1m"


# ─── git plumbing ────────────────────────────────────────────────────────────
def _git(args):
    """Run a git command and return the CompletedProcess (text mode)."""
    return subprocess.run(["git"] + args, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


def in_git_repo():
    return _git(["rev-parse", "--git-dir"]).returncode == 0


def _strip_remote(ref, remote_names):
    """origin/feature/x -> feature/x, stripping whichever remote prefix matches."""
    for r in remote_names:
        if ref.startswith(r + "/"):
            return ref[len(r) + 1:]
    return ref.split("/", 1)[1] if "/" in ref else ref


def get_branches(show_remotes):
    """Collect the branches to offer. Returns (branches, locals_set,
    remote_names). Remotes are included only when show_remotes is true."""
    current = _git(["symbolic-ref", "--quiet", "--short", "HEAD"]).stdout.strip() or None

    local_names = [l for l in _git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"]
    ).stdout.splitlines() if l]
    locals_set = set(local_names)

    branches = [Branch(n, "local", n, n == current, n) for n in local_names]

    remote_names = set(_git(["remote"]).stdout.split())
    if show_remotes:
        for ref in _git(
            ["for-each-ref", "--format=%(refname:short)", "refs/remotes"]
        ).stdout.splitlines():
            # The "<remote>/HEAD" symref is a pointer, not a branch.
            if not ref or ref.endswith("/HEAD"):
                continue
            branches.append(Branch(ref, "remote", ref, False,
                                   _strip_remote(ref, remote_names)))

    return branches, locals_set, remote_names


# ─── folder tree ─────────────────────────────────────────────────────────────
class Node(object):
    """A node in the branch tree. Internal nodes (with children) render as
    folders; a node carrying a Branch is a selectable leaf."""
    __slots__ = ("seg", "path", "children", "branch")

    def __init__(self, seg, path):
        self.seg = seg          # this level's name segment
        self.path = path        # full slash-joined path to here
        self.children = {}      # seg -> Node
        self.branch = None      # Branch if this node is a leaf

    @property
    def is_folder(self):
        return bool(self.children)


def build_tree(branches):
    """Split every branch name on "/" and weave the segments into a tree."""
    root = Node("", "")
    for br in branches:
        parts = br.name.split("/")
        node = root
        for i, seg in enumerate(parts):
            path = "/".join(parts[:i + 1])
            child = node.children.get(seg)
            if child is None:
                child = Node(seg, path)
                node.children[seg] = child
            node = child
        node.branch = br
    return root


def _subtree_matches(node, filt, memo):
    """True if any branch at or below node matches the filter (memoized)."""
    cached = memo.get(id(node))
    if cached is not None:
        return cached
    hit = node.branch is not None and (filt is None or filt.search(node.branch.name))
    if not hit:
        hit = any(_subtree_matches(c, filt, memo) for c in node.children.values())
    memo[id(node)] = hit
    return hit


def build_visible(root, expanded, filt):
    """Flatten the tree into the ordered list of rows to draw. A folder is shown
    expanded when it's in `expanded`, or always while a filter is active (so
    matches aren't hidden). Branch rows are numbered 1..N in visible order.

    Each row is a dict: type ("folder"/"branch"), depth, node, expanded,
    number (None for folders), id (stable key for cursor tracking), and for
    branches, branch."""
    rows = []
    memo = {}
    counter = [0]

    def visit(node, depth):
        for seg in sorted(node.children, key=str.lower):
            child = node.children[seg]
            if child.is_folder:
                if filt is not None and not _subtree_matches(child, filt, memo):
                    continue
                is_open = filt is not None or child.path in expanded
                rows.append({"type": "folder", "depth": depth, "node": child,
                             "expanded": is_open, "number": None,
                             "id": "F:" + child.path})
                if is_open:
                    visit(child, depth + 1)
            else:
                br = child.branch
                if filt is not None and not filt.search(br.name):
                    continue
                counter[0] += 1
                rows.append({"type": "branch", "depth": depth, "node": child,
                             "expanded": False, "number": counter[0],
                             "id": "B:" + br.name, "branch": br})

    visit(root, 0)
    return rows


# ─── key input (per platform, stdlib only) ───────────────────────────────────
# read_key() returns one logical token: a single printable character, or one of
# the names below. "" means "ignore this key".
if os.name == "nt":
    import msvcrt

    _WIN_SPECIAL = {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT",
                    "G": "HOME", "O": "END", "S": "DELETE"}

    def read_key():
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return _WIN_SPECIAL.get(msvcrt.getwch(), "")
        if ch == "\r":
            return "ENTER"
        if ch == "\x08":
            return "BACKSPACE"
        if ch == "\t":
            return "TAB"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x1b":
            return "ESC"
        if ch < " ":
            return ""
        return ch
else:
    import select
    import termios
    import tty

    _POSIX_SEQ = {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT",
                  b"H": "HOME", b"F": "END"}

    def read_key():
        fd = sys.stdin.fileno()
        b = os.read(fd, 1)
        if not b:
            return "EOF"
        o = b[0]
        if o == 0x1b:                                   # ESC or escape sequence
            r, _, _ = select.select([fd], [], [], 0.03)
            if not r:
                return "ESC"
            nxt = os.read(fd, 1)
            if nxt not in (b"[", b"O"):
                return "ESC"
            code = os.read(fd, 1)
            if code in _POSIX_SEQ:
                return _POSIX_SEQ[code]
            while code and code not in b"~":            # swallow e.g. "3~"
                code = os.read(fd, 1)
            return ""
        if o in (0x0d, 0x0a):
            return "ENTER"
        if o in (0x7f, 0x08):
            return "BACKSPACE"
        if o == 0x09:
            return "TAB"
        if o == 0x03:
            raise KeyboardInterrupt
        if o == 0x04:
            return "EOF"
        if o < 0x20:
            return ""
        if o < 0x80:
            return chr(o)
        # Decode a UTF-8 multibyte character so filter text can be non-ASCII.
        n = 3 if o >= 0xf0 else 2 if o >= 0xe0 else 1 if o >= 0xc0 else 0
        try:
            return (b + os.read(fd, n)).decode("utf-8")
        except (UnicodeDecodeError, OSError):
            return ""


class TerminalSession(object):
    """Context manager: put the terminal in raw mode, switch to the alternate
    screen, hide the cursor -- and undo all of it on the way out, even on error."""
    def __init__(self):
        self._fd = None
        self._saved = None

    def __enter__(self):
        if os.name != "nt":
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        else:
            self._enable_windows_vt()
        sys.stderr.write(ALT_ON + HIDE_CUR)
        sys.stderr.flush()
        return self

    def __exit__(self, *exc):
        sys.stderr.write(SHOW_CUR + ALT_OFF + RESET)
        sys.stderr.flush()
        if os.name != "nt" and self._saved is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        return False

    @staticmethod
    def _enable_windows_vt():
        """Turn on ANSI escape processing for the console (Windows 10+)."""
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):                    # STDOUT, STDERR
            h = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h, mode.value | 0x0004)


def term_size():
    try:
        ts = os.get_terminal_size(sys.stderr.fileno())
        return ts.columns, ts.lines
    except OSError:
        import shutil
        ts = shutil.get_terminal_size((80, 24))
        return ts.columns, ts.lines


# ─── the interactive picker ──────────────────────────────────────────────────
class Picker(object):
    def __init__(self, show_remotes, use_color):
        self.show_remotes = show_remotes
        self.use_color = use_color
        self.expanded = set()      # folder paths the user has opened
        self.query = ""            # current filter expression
        self.filt = None           # compiled query (or None)
        self.bad_regex = False     # query was invalid; using it literally
        self.mode = "normal"       # "normal" | "filter"
        self.pending = ""          # digits typed for number-jump
        self.cursor = 0
        self.cur_id = None         # row id under the cursor, to survive rebuilds
        self.top = 0               # scroll offset
        self.rows = []
        self.locals_set = set()
        self.result = None         # the chosen Branch, or None to quit
        self._cache = {}           # show_remotes -> (branches, locals, remotes)

    # -- data ----------------------------------------------------------------
    def _branches(self):
        if self.show_remotes not in self._cache:
            self._cache[self.show_remotes] = get_branches(self.show_remotes)
        return self._cache[self.show_remotes]

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
        branches, self.locals_set, _ = self._branches()
        root = build_tree(branches)
        self._compile_filter()
        self.rows = build_visible(root, self.expanded, self.filt)
        # Keep the cursor on the same item across rebuilds when we can; when we
        # can't (e.g. the filter just changed), land on the first *branch* row
        # rather than a folder header so Enter switches instead of expanding.
        if self.cur_id is not None:
            for i, r in enumerate(self.rows):
                if r["id"] == self.cur_id:
                    self.cursor = i
                    break
            else:
                self.cursor = self._first_branch_index()
        else:
            self.cursor = self._first_branch_index()
        self.cursor = max(0, min(self.cursor, len(self.rows) - 1)) if self.rows else 0
        if self.rows:
            self.cur_id = self.rows[self.cursor]["id"]

    def _first_branch_index(self):
        for i, r in enumerate(self.rows):
            if r["type"] == "branch":
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
        self.pending = ""

    def move_to(self, index):
        if self.rows:
            self.cursor = max(0, min(index, len(self.rows) - 1))
            self._sync_id()
        self.pending = ""

    def jump_to_number(self):
        """Move the cursor to the branch whose number matches the pending
        digits (clamped to the largest number when it overruns)."""
        if not self.pending:
            return
        n = int(self.pending)
        target = None
        for i, r in enumerate(self.rows):
            if r["number"] is not None:
                target = i
                if r["number"] >= n:
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
            self.move(1)                       # already open -> step inside
        else:
            self.expanded.add(row["node"].path)

    def collapse(self):
        if not self.rows:
            return
        row = self.rows[self.cursor]
        if row["type"] == "folder" and row["expanded"]:
            self.expanded.discard(row["node"].path)
            return
        # On a branch or a closed folder: hop up to the parent folder.
        path = row["node"].path
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if not parent:
            return
        for i, r in enumerate(self.rows):
            if r["id"] == "F:" + parent:
                self.cursor = i
                self._sync_id()
                break

    def toggle_remotes(self):
        self.show_remotes = not self.show_remotes

    def activate(self):
        """Act on the current row. Returns False to end the loop (a branch was
        chosen), True to keep going (a folder was toggled)."""
        if not self.rows:
            return True
        row = self.rows[self.cursor]
        self.pending = ""
        if row["type"] == "folder":
            if row["expanded"]:
                self.expanded.discard(row["node"].path)
            else:
                self.expanded.add(row["node"].path)
            return True
        self.result = row["branch"]
        return False

    # -- key handling --------------------------------------------------------
    def handle(self, key):
        if key in ("", "DELETE"):
            return True
        if key == "EOF":
            self.result = None
            return False
        if key == "TAB":
            self.toggle_remotes()
            return True
        if self.mode == "filter":
            return self._handle_filter(key)
        return self._handle_normal(key)

    def _handle_normal(self, key):
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
            self.mode = "filter"
        elif key == "r":
            self.toggle_remotes()
        elif key in ("q", "ESC"):
            self.result = None
            return False
        elif key == "ENTER":
            return self.activate()
        elif key == "BACKSPACE":
            self.pending = self.pending[:-1]
            self.jump_to_number()
        elif len(key) == 1 and key.isdigit():
            self.pending += key
            self.jump_to_number()
        return True

    def _handle_filter(self, key):
        if key == "ESC":
            self.mode = "normal"
            self.query = ""
            return True
        if key == "ENTER":
            return self.activate()
        if key == "BACKSPACE":
            self.query = self.query[:-1]
            self.cur_id = None
            self.cursor = 0
        elif key == "UP":
            self.move(-1)
        elif key == "DOWN":
            self.move(1)
        elif key == "HOME":
            self.move_to(0)
        elif key == "END":
            self.move_to(len(self.rows) - 1)
        elif key in ("LEFT", "RIGHT"):
            pass
        elif len(key) == 1 and key >= " ":
            self.query += key
            self.cur_id = None
            self.cursor = 0
        return True

    # -- rendering -----------------------------------------------------------
    def _row_text(self, row):
        if row["type"] == "folder":
            arrow = "▾" if row["expanded"] else "▸"
            return "    " + "  " * row["depth"] + arrow + " " + row["node"].seg + "/"
        br = row["branch"]
        marker = " *" if br.is_current else ""
        return "{0:>3} ".format(row["number"]) + "  " * row["depth"] + row["node"].seg + marker

    def _color_for(self, row):
        if not self.use_color:
            return ""
        if row["type"] == "folder":
            return BLUE
        br = row["branch"]
        if br.is_current:
            return GREEN
        if br.kind == "remote":
            return CYAN
        return ""

    def render(self):
        cols, lines_h = term_size()
        area = max(1, lines_h - 4)

        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + area:
            self.top = self.cursor - area + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - area)))

        out = [HOME]
        scope = "local + remote" if self.show_remotes else "local"
        mode = "FILTER" if self.mode == "filter" else "NORMAL"
        header = " Switch branch    [{0}]    ({1})".format(mode, scope)
        out.append(self._line(header[:cols], cols, BOLD if self.use_color else ""))
        out.append(self._line("─" * cols, cols, ""))

        window = self.rows[self.top:self.top + area]
        for i, row in enumerate(window):
            idx = self.top + i
            text = self._row_text(row)[:cols]
            if self.use_color and idx == self.cursor:
                out.append(self._line(text, cols, REVERSE))
            else:
                out.append(self._line(text, cols, self._color_for(row)))
        if not self.rows:
            out.append(self._line("  (no branches match)", cols, ""))
        for _ in range(area - max(len(window), 0 if self.rows else 1)):
            out.append("" + CLEAR_EOL + "\r\n")

        out.append(self._line("─" * cols, cols, ""))
        out.append(self._line(self._footer()[:cols], cols, ""))
        sys.stderr.write("".join(out) + CLEAR_EOS)
        sys.stderr.flush()

    @staticmethod
    def _line(text, cols, color):
        text = text + " " * max(0, cols - len(text))
        if color:
            text = color + text + RESET
        return text + CLEAR_EOL + "\r\n"

    def _footer(self):
        if self.mode == "filter":
            tail = " (literal)" if self.bad_regex else ""
            return " ↑↓ move · ⏎ switch · ⌫ del · Esc clear · Tab remotes    /{0}{1}".format(
                self.query, tail)
        hint = " j/k move · #+⏎ select · / filter · h/l fold · Tab remotes · q quit"
        if self.pending:
            hint += "    #" + self.pending
        return hint

    # -- main loop -----------------------------------------------------------
    def run(self):
        self.rebuild()
        for i, row in enumerate(self.rows):       # start on the current branch
            if row["type"] == "branch" and row["branch"].is_current:
                self.cursor = i
                self.cur_id = row["id"]
                break
        while True:
            self.rebuild()
            self.render()
            try:
                key = read_key()
            except KeyboardInterrupt:
                self.result = None
                return None
            if not self.handle(key):
                return self.result


# ─── performing the switch ───────────────────────────────────────────────────
def _checkout_equivalent(cmd):
    """Translate a `git switch ...` invocation to the older `git checkout ...`."""
    if cmd[:2] == ["git", "switch"]:
        rest = cmd[2:]
        if rest[:1] == ["-c"]:                 # -c name --track ref  ->  -b name --track ref
            return ["git", "checkout", "-b"] + rest[1:]
        return ["git", "checkout"] + rest
    return cmd


def do_switch(branch, locals_set):
    """Run the git command for the chosen branch. Returns a process exit code."""
    if branch.is_current:
        print("Already on '{0}'.".format(branch.name))
        return 0

    if branch.kind == "local":
        cmd = ["git", "switch", branch.name]
        target = branch.name
    elif branch.local_name in locals_set:
        cmd = ["git", "switch", branch.local_name]   # local copy already exists
        target = branch.local_name
    else:
        cmd = ["git", "switch", "-c", branch.local_name, "--track", branch.ref]
        target = branch.local_name

    proc = subprocess.run(cmd, stderr=subprocess.PIPE, universal_newlines=True)
    err = proc.stderr or ""
    if proc.returncode != 0 and ("is not a git command" in err or "unknown switch" in err):
        proc = subprocess.run(_checkout_equivalent(cmd),
                              stderr=subprocess.PIPE, universal_newlines=True)
        err = proc.stderr or ""

    if proc.returncode == 0:
        print("Switched to '{0}'.".format(target))
    else:
        sys.stderr.write(err)
    return proc.returncode


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="switch-branch.py",
        description="Interactively pick a Git branch (vim-style) and switch to it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "keys:\n"
            "  j/k or arrows  move      g/G  top/bottom    h/l  collapse/expand\n"
            "  <num> + Enter  select    /    filter        Tab  toggle remotes\n"
            "  Enter          switch    Esc  back/clear     q   quit\n"
        ),
    )
    parser.add_argument("-r", "--remotes", action="store_true",
                        help="start with remote branches already included")
    parser.add_argument("--no-color", action="store_true",
                        help="disable colored output")
    parser.add_argument("--version", action="version",
                        version="%(prog)s {0}".format(__version__))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if not in_git_repo():
        sys.stderr.write("error: not inside a Git repository\n")
        return 1
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        sys.stderr.write("error: switch-branch needs an interactive terminal\n")
        return 1

    branches, _, _ = get_branches(args.remotes)
    if not branches:
        sys.stderr.write("error: no branches to choose from\n")
        return 1

    use_color = not args.no_color and "NO_COLOR" not in os.environ
    picker = Picker(args.remotes, use_color)

    with TerminalSession():
        choice = picker.run()

    if choice is None:
        return 0
    return do_switch(choice, picker.locals_set)


if __name__ == "__main__":
    raise SystemExit(main())
