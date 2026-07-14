#!/usr/bin/env python3
"""Shared building blocks for the interactive branch tools (`git-switch.py`
and `delete-branch.py`): Git plumbing, the collapsible folder tree, raw-terminal
key input, and a reusable modal Picker that both scripts subclass.

This is the one place in the repo where two scripts share code rather than each
being fully self-contained -- the branch picker is large enough that copying it
twice would be the bigger sin. It lives next to the scripts that import it, so
`uv run git-switch.py` (or running the .py directly) finds it on sys.path.

Standard library only; works on macOS, Linux, and Windows (raw mode + ANSI, no
curses).
"""

import os
import re
import subprocess
import sys
from collections import namedtuple

# A single branch the tools can act on.
#   name        full display name (local: "feature/x"; remote: "origin/feature/x")
#   kind        "local" or "remote"
#   ref         the ref to hand to git (same as name)
#   is_current  True for the currently checked-out branch
#   local_name  for a remote, the local branch name it maps to (prefix stripped)
Branch = namedtuple("Branch", "name kind ref is_current local_name")

# ─── ANSI escapes ────────────────────────────────────────────────────────────
CSI = "\x1b["
ALT_ON, ALT_OFF = CSI + "?1049h", CSI + "?1049l"   # alternate screen buffer
HIDE_CUR, SHOW_CUR = CSI + "?25l", CSI + "?25h"
HOME = CSI + "H"
CLEAR_EOL, CLEAR_EOS = CSI + "K", CSI + "J"
REVERSE, RESET = CSI + "7m", CSI + "0m"
GREEN, BLUE, CYAN, YELLOW, BOLD = (CSI + "32m", CSI + "1;34m", CSI + "36m",
                                   CSI + "33m", CSI + "1m")


# ─── git plumbing ────────────────────────────────────────────────────────────
def git(args):
    """Run a git command and return the CompletedProcess (text mode)."""
    return subprocess.run(["git"] + args, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace")


def in_git_repo():
    return git(["rev-parse", "--git-dir"]).returncode == 0


def split_remote_ref(ref, remote_names):
    """origin/feature/x -> ("origin", "feature/x"), splitting on whichever
    remote prefix matches (falls back to the first path segment)."""
    for r in remote_names:
        if ref.startswith(r + "/"):
            return r, ref[len(r) + 1:]
    head, _, tail = ref.partition("/")
    return head, tail


def get_branches(show_remotes):
    """Collect the branches to offer. Returns (branches, locals_set,
    remote_names). Remotes are included only when show_remotes is true."""
    current = git(["symbolic-ref", "--quiet", "--short", "HEAD"]).stdout.strip() or None

    local_names = [l for l in git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"]
    ).stdout.splitlines() if l]
    locals_set = set(local_names)

    branches = [Branch(n, "local", n, n == current, n) for n in local_names]

    remote_names = set(git(["remote"]).stdout.split())
    if show_remotes:
        for ref in git(
            ["for-each-ref", "--format=%(refname:short)", "refs/remotes"]
        ).stdout.splitlines():
            # The "<remote>/HEAD" symref is a pointer, not a branch.
            if not ref or ref.endswith("/HEAD"):
                continue
            _, local_name = split_remote_ref(ref, remote_names)
            branches.append(Branch(ref, "remote", ref, False, local_name))

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


class Row(object):
    """One visible line in a browser. A slotted object rather than a dict: on a
    huge tree (git-diff can be hundreds of thousands of rows) we build one per
    line, and a slotted class is lighter and quicker to allocate than a dict.

    Fields: type ("folder"/"branch"), depth, node, expanded, number (None for
    folders; a 1..N counter over branch rows in visible order for the ":N" jump),
    id (a stable key for cursor tracking across rebuilds), and branch (the
    Branch/Match/DiffLine on a branch row, None on a folder).

    It supports item access (row["type"]) as well as attribute access so the
    callers that predate the class keep working unchanged."""
    __slots__ = ("type", "depth", "node", "expanded", "number", "id", "branch")

    def __init__(self, type, depth, node, expanded, number, id, branch):
        self.type = type
        self.depth = depth
        self.node = node
        self.expanded = expanded
        self.number = number
        self.id = id
        self.branch = branch

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def __eq__(self, other):
        return isinstance(other, Row) and all(
            getattr(self, s) == getattr(other, s) for s in self.__slots__)

    __hash__ = None     # rows live in lists and are compared, never hashed

    def __repr__(self):
        return "Row({0} {1} d{2} #{3})".format(self.type, self.id, self.depth,
                                                self.number)


def _flatten(node, expanded, depth=0):
    """The unfiltered flatten: rows for `node`'s visible descendants (not node
    itself), in display order, with `number` left None. A folder is descended
    into only when its path is in `expanded`. Shared by the full build and by
    the incremental splice, so the two can't drift apart. Numbers are assigned
    afterwards by `_renumber`."""
    rows = []

    def visit(node, depth):
        for seg in sorted(node.children, key=str.lower):
            child = node.children[seg]
            if child.is_folder:
                is_open = child.path in expanded
                rows.append(Row("folder", depth, child, is_open, None,
                                "F:" + child.path, None))
                if is_open:
                    visit(child, depth + 1)
            else:
                rows.append(Row("branch", depth, child, False, None,
                                "B:" + child.branch.name, child.branch))

    visit(node, depth)
    return rows


def _renumber(rows, start=0, start_num=0):
    """Assign branch rows a running 1..N number in visible order, walking from
    `rows[start]` with the counter continuing at `start_num`. Folder rows keep
    number=None. Used both for the full build (start=0) and after a splice (from
    the splice point, so only the tail is retouched)."""
    n = start_num
    for i in range(start, len(rows)):
        r = rows[i]
        if r.type == "branch":
            n += 1
            r.number = n


def build_visible(root, expanded, filt):
    """Flatten the tree into the ordered list of Row rows to draw. A folder is
    shown expanded when it's in `expanded`, or always while a filter is active
    (so matches aren't hidden). Branch rows are numbered 1..N in visible order.

    With no filter this is `_flatten` + `_renumber` -- the same path the
    incremental splice helpers build on. With a filter every folder that holds a
    match is forced open and non-matching rows are dropped."""
    if filt is None:
        rows = _flatten(root, expanded)
        _renumber(rows)
        return rows

    rows = []
    memo = {}
    counter = [0]

    def visit(node, depth):
        for seg in sorted(node.children, key=str.lower):
            child = node.children[seg]
            if child.is_folder:
                if not _subtree_matches(child, filt, memo):
                    continue
                rows.append(Row("folder", depth, child, True, None,
                                "F:" + child.path, None))
                visit(child, depth + 1)
            else:
                br = child.branch
                if not filt.search(br.name):
                    continue
                counter[0] += 1
                rows.append(Row("branch", depth, child, False, counter[0],
                               "B:" + br.name, br))

    visit(root, 0)
    return rows


def _preceding_number(rows, index):
    """The branch number in effect at rows[index]: the number of the nearest row
    at or before it that carries one (folders carry None), or 0 if there is
    none. Lets a splice renumber only its tail, continuing the count correctly."""
    for i in range(index, -1, -1):
        if rows[i].number is not None:
            return rows[i].number
    return 0


def splice_expand(rows, index, expanded):
    """Reveal the subtree of the folder at rows[index] in place: mark it open,
    insert its now-visible descendants after it, and renumber from there. The
    folder's path must already be in `expanded`. Mutates `rows`."""
    folder = rows[index]
    folder.expanded = True
    sub = _flatten(folder.node, expanded, folder.depth + 1)
    rows[index + 1:index + 1] = sub
    _renumber(rows, index + 1, _preceding_number(rows, index))


def splice_collapse(rows, index):
    """Hide the subtree of the expanded folder at rows[index] in place: drop the
    contiguous run of deeper rows that follow it, mark it closed, and renumber
    from there. Mutates `rows`. (The folder's descendants keep their own entries
    in the caller's `expanded` set, so re-expanding restores their state.)"""
    folder = rows[index]
    depth = folder.depth
    end = index + 1
    while end < len(rows) and rows[end].depth > depth:
        end += 1
    del rows[index + 1:end]
    folder.expanded = False
    _renumber(rows, index + 1, _preceding_number(rows, index))


def branches_under(branches, folder_path):
    """Every branch whose name falls under the given folder prefix."""
    prefix = folder_path + "/"
    return [b for b in branches if b.name.startswith(prefix)]


# ─── key input (per platform, stdlib only) ───────────────────────────────────
# read_key() returns one logical token: a single printable character, or one of
# the names below. "" means "ignore this key". input_pending() reports, without
# blocking, whether another key is already buffered -- the run loops use it to
# handle a whole burst of keys per redraw, so held-key autorepeat can't queue
# up stale frames faster than the terminal draws them.
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
        if ch == "\x0c":
            return "REDRAW"
        if ch == "\x1b":
            return "ESC"
        if ch < " ":
            return ""
        return ch

    def input_pending():
        return msvcrt.kbhit()
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
        if o == 0x0c:
            return "REDRAW"
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

    def input_pending():
        r, _, _ = select.select([sys.stdin.fileno()], [], [], 0)
        return bool(r)


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


def screen_line(text, cols, color, pad=False):
    """One line of a frame, ready for FramePainter: `text` (already clipped to
    `cols`) wrapped in `color`. The painter's erase-to-EOL wipes whatever the
    previous frame left on the line, so no space padding is needed -- except on
    the reversed cursor row (pad=True), where the highlight bar must span the
    width with real reverse-video spaces."""
    if pad:
        text = text + " " * max(0, cols - len(text))
    if color:
        return color + text + RESET
    return text


class FramePainter(object):
    """Paints whole frames with minimal output: remembers the lines it last
    painted and rewrites only the ones that changed, each addressed with an
    absolute cursor-position escape (curses' virtual-screen idea, at line
    granularity). A cursor move thus costs two short writes instead of a full
    frame -- what keeps ssh and VM terminals responsive -- while a scroll,
    a resize, or the first paint naturally falls back to a full repaint.

    The frame must be a list of exactly one string per screen line, none of
    which moves the cursor itself (colors are fine). reset() forgets the
    cache; call it whenever the screen may no longer show what was painted
    (after suspending for an editor, or on Ctrl-L)."""

    def __init__(self):
        self._lines = None
        self._size = None

    def reset(self):
        self._lines = None

    def paint(self, lines, size):
        if (self._lines is None or self._size != size or
                len(self._lines) != len(lines)):
            # Full repaint: home, every line (erasing its tail), then erase
            # any remnant below. No trailing newline -> no scroll bounce.
            out = HOME + (CLEAR_EOL + "\r\n").join(lines) + CLEAR_EOS
        else:
            out = "".join("\x1b[{0};1H".format(i + 1) + line + CLEAR_EOL
                          for i, line in enumerate(lines)
                          if line != self._lines[i])
        self._lines = lines
        self._size = size
        if out:
            sys.stderr.write(out)
            sys.stderr.flush()


def is_file_node(node):
    """A file node is a folder whose children are leaves (not subfolders)."""
    for child in node.children.values():
        return child.branch is not None
    return False


def repo_toplevel():
    out = git(["rev-parse", "--show-toplevel"])
    return out.stdout.strip() if out.returncode == 0 else None


# ─── the reusable tree browser ───────────────────────────────────────────────
class TreeBrowser(object):
    """The machinery every full-screen tree TUI here shares: a flat `rows`
    view over a Node tree, cursor movement whose position survives rebuilds,
    folder fold/unfold with in-place row splicing, file-to-file jumps, the
    frame layout (header, rule, rows with a reversed cursor bar, rule, footer)
    painted through a diffing FramePainter, and the key loop that drains
    buffered input so held-key autorepeat can't outrun the terminal.

    Subclasses provide the content: `_build_rows()` (what to show; the
    default flattens `self.root`), `_row_text`/`_color_for` (how a row
    looks), `_header`/`_footer`/`_empty_hint` (the chrome), and
    `_dispatch(key, screen)` (what keys do, beyond the universal ones
    handle() consumes: Ctrl-L repaint, EOF quit, ignored keys)."""

    title = "?"

    def __init__(self, use_color):
        self.use_color = use_color
        self.root = None            # the Node tree, when _build_rows uses one
        self.expanded = set()       # folder paths currently open
        self.cursor = 0
        self.cur_id = None          # row id under the cursor, to survive rebuilds
        self.top = 0                # scroll offset
        self.rows = []
        self.status = ""            # transient one-line message in the footer
        self.result = None          # what run() returns (pickers set it)
        self.dirty = True           # rows need rebuilding before the next render
        self.painter = FramePainter()

    # -- building the visible rows --------------------------------------------
    def _build_rows(self):
        return build_visible(self.root, self.expanded, None) if self.root else []

    def rebuild(self):
        # O(whole tree); the run loop only calls it when `dirty` marks a real
        # structural change -- not on plain cursor moves -- and expand/collapse
        # splice the rows in place instead. Keeps the cursor on the same item
        # when it still exists; otherwise lands on the first branch row rather
        # than a folder header, so Enter acts on a leaf.
        self.rows = self._build_rows()
        if self.cur_id is not None:
            for i, row in enumerate(self.rows):
                if row["id"] == self.cur_id:
                    self.cursor = i
                    break
            else:
                self.cursor = self._first_branch_index()
        else:
            self.cursor = self._first_branch_index()
        self.cursor = max(0, min(self.cursor, len(self.rows) - 1)) if self.rows else 0
        self.cur_id = self.rows[self.cursor]["id"] if self.rows else None
        self.dirty = False

    def _first_branch_index(self):
        for i, row in enumerate(self.rows):
            if row["type"] == "branch":
                return i
        return 0

    def current_row(self):
        return self.rows[self.cursor] if self.rows else None

    # -- cursor movement -------------------------------------------------------
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

    def jump_to_number(self, buf):
        """Move the cursor to the branch row whose displayed number matches
        the typed digits (clamped to the largest number when it overruns)."""
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

    # -- folder open/close -------------------------------------------------------
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
            splice_expand(self.rows, self.cursor, self.expanded)

    def collapse(self):
        if not self.rows:
            return
        row = self.rows[self.cursor]
        if row["type"] == "folder" and row["expanded"]:
            self.expanded.discard(row["node"].path)
            splice_collapse(self.rows, self.cursor)
            return
        # On a leaf or a closed folder: hop up to the parent folder.
        path = row["node"].path
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if not parent:
            return
        for i, r in enumerate(self.rows):
            if r["id"] == "F:" + parent:
                self.cursor = i
                self._sync_id()
                break

    # -- file-to-file jumps ------------------------------------------------------
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

    # -- rendering: the frame layout; subclasses fill in the content ------------
    def _header(self):
        raise NotImplementedError

    def _footer(self):
        raise NotImplementedError

    def _row_text(self, row):
        raise NotImplementedError

    def _color_for(self, row):
        return ""

    def _empty_hint(self):
        return ""

    def render(self):
        cols, lines_h = term_size()
        area = max(1, lines_h - 4)

        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + area:
            self.top = self.cursor - area + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - area)))

        lines = [screen_line(self._header()[:cols], cols,
                             BOLD if self.use_color else ""),
                 screen_line("─" * cols, cols, "")]

        window = self.rows[self.top:self.top + area]
        for i, row in enumerate(window):
            idx = self.top + i
            text = self._row_text(row)[:cols]
            if self.use_color and idx == self.cursor:
                lines.append(screen_line(text, cols, REVERSE, pad=True))
            else:
                lines.append(screen_line(text, cols, self._color_for(row)))
        if not self.rows:
            lines.append(screen_line(self._empty_hint(), cols, ""))
        lines.extend([""] * (area - max(len(window), 0 if self.rows else 1)))

        lines.append(screen_line("─" * cols, cols, ""))
        lines.append(screen_line(self._footer()[:cols], cols, ""))
        self.painter.paint(lines, (cols, lines_h))

    # -- key handling / main loop ------------------------------------------------
    def _dispatch(self, key, screen):
        raise NotImplementedError

    def handle(self, key, screen=None):
        if key in ("", "DELETE"):
            return True
        if key == "REDRAW":
            self.painter.reset()     # Ctrl-L: repaint from scratch
            return True
        if key == "EOF":
            return False
        return self._dispatch(key, screen)

    def run(self, screen=None):
        while True:
            if self.dirty:              # only reflatten when the tree/expansion
                self.rebuild()          # changed -- not on plain cursor moves
            self.render()
            # Drain the whole burst of buffered keys before redrawing, so a
            # held-down key can't queue frames faster than the terminal draws.
            while True:
                try:
                    key = read_key()
                except KeyboardInterrupt:
                    self.result = None
                    return self.result
                if not self.handle(key, screen):
                    return self.result
                if not input_pending():
                    break


# ─── the reusable modal picker ───────────────────────────────────────────────
class Picker(TreeBrowser):
    """A modal (NORMAL / FILTER) branch picker over the folder tree. Subclasses
    decide what Enter does and may add per-row decorations and extra keys by
    overriding the hook methods near the bottom."""

    title = "Branches"

    def __init__(self, show_remotes, use_color):
        TreeBrowser.__init__(self, use_color)
        self.show_remotes = show_remotes
        self.query = ""            # current filter expression
        self.filt = None           # compiled query (or None)
        self.bad_regex = False     # query was invalid; using it literally
        self.mode = "normal"       # "normal" | "filter"
        self.pending = ""          # digits typed for number-jump
        self.locals_set = set()
        self.remote_names = set()
        self._cache = {}           # show_remotes -> (branches, locals, remotes)
        self._root_cache = {}      # show_remotes -> the built folder tree

    # -- data ----------------------------------------------------------------
    def _branches(self):
        if self.show_remotes not in self._cache:
            self._cache[self.show_remotes] = get_branches(self.show_remotes)
        return self._cache[self.show_remotes]

    def all_branches(self):
        return self._branches()[0]

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

    def _build_rows(self):
        branches, self.locals_set, self.remote_names = self._branches()
        if self.show_remotes not in self._root_cache:  # the tree is fixed per
            self._root_cache[self.show_remotes] = build_tree(branches)  # scope
        self._compile_filter()
        return build_visible(self._root_cache[self.show_remotes],
                             self.expanded, self.filt)

    # -- cursor movement -----------------------------------------------------
    def move(self, delta):
        TreeBrowser.move(self, delta)
        self.pending = ""

    def move_to(self, index):
        TreeBrowser.move_to(self, index)
        self.pending = ""

    # -- folder open/close ---------------------------------------------------
    def toggle_folder(self, row):
        if row["expanded"]:
            self.expanded.discard(row["node"].path)
        else:
            self.expanded.add(row["node"].path)

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

    # -- key handling --------------------------------------------------------
    def _dispatch(self, key, screen):
        # The rows depend only on the query, the mode, the local/remote scope,
        # and which folders are open; if a key touches any of those, mark for a
        # rebuild. Plain cursor moves touch none, so navigation skips it. The
        # check is in a finally so it fires no matter how the handler returns.
        before = (self.query, self.mode, self.show_remotes, frozenset(self.expanded))
        try:
            if key == "TAB":
                self.toggle_remotes()
                return True
            if self.mode == "filter":
                return self._handle_filter(key)
            return self._handle_normal(key)
        finally:
            if (self.query, self.mode, self.show_remotes,
                    frozenset(self.expanded)) != before:
                self.dirty = True

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
            return self.on_enter()
        elif key == "BACKSPACE":
            self.pending = self.pending[:-1]
            self.jump_to_number(self.pending)
        elif len(key) == 1 and key.isdigit():
            self.pending += key
            self.jump_to_number(self.pending)
        else:
            handled = self.on_extra_key(key)
            if handled is not None:
                return handled
        return True

    def _handle_filter(self, key):
        if key == "ESC":
            self.mode = "normal"
            self.query = ""
            return True
        if key == "ENTER":
            return self.on_enter()
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
            handled = self.on_extra_key(key)   # e.g. Space = check, even here
            if handled is not None:
                return handled
            self.query += key
            self.cur_id = None
            self.cursor = 0
        return True

    # -- rendering -----------------------------------------------------------
    def _row_text(self, row):
        prefix = self.checkbox(row)
        indent = "  " * row["depth"]
        if row["type"] == "folder":
            arrow = "▾" if row["expanded"] else "▸"
            return prefix + "    " + indent + arrow + " " + row["node"].seg + "/"
        br = row["branch"]
        marker = self.branch_suffix(br)
        return prefix + "{0:>3} ".format(row["number"]) + indent + row["node"].seg + marker

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

    def _header(self):
        scope = "local + remote" if self.show_remotes else "local"
        mode = "FILTER" if self.mode == "filter" else "NORMAL"
        return " {0}    [{1}]    ({2}){3}".format(
            self.title, mode, scope, self.header_extra())

    def _footer(self):
        return self.footer()           # the overridable subclass hook

    def _empty_hint(self):
        return "  (no branches match)"

    def _filter_suffix(self):
        tail = " (literal)" if self.bad_regex else ""
        return "    /{0}{1}".format(self.query, tail)

    # -- main loop -----------------------------------------------------------
    def run(self):
        self.rebuild()
        self.initial_cursor()
        return TreeBrowser.run(self)

    # -- hooks for subclasses ------------------------------------------------
    def on_enter(self):
        """Act on Enter. Return False to end the loop, True to keep going."""
        raise NotImplementedError

    def on_extra_key(self, key):
        """Handle a key the base doesn't. Return True/False to consume it
        (and whether to keep looping), or None to let the base ignore it."""
        return None

    def checkbox(self, row):
        """A left-hand decoration for the row (e.g. a checkbox). Empty by default."""
        return ""

    def branch_suffix(self, branch):
        """Trailing decoration on a branch row (e.g. the current-branch mark)."""
        return " *" if branch.is_current else ""

    def header_extra(self):
        return ""

    def footer(self):
        return " (override footer)"

    def initial_cursor(self):
        """Place the cursor when the picker first opens."""
        pass
