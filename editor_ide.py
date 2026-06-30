#!/usr/bin/env python3
"""Shared detection of the editor whose integrated terminal we're running in,
plus a fire-and-forget way to open a file there at a given line.

`git-open.py` and `git-grep.py` both prefer this over the editor configured in
`.git-open-config`: when you launch them from inside VS Code, a JetBrains IDE
(CLion and friends), or Zed, opening your pick should drop it into a tab of that
*already-running* editor rather than spawning a separate one. Every route here
returns at once -- a `vscode://` URL hand-off, or a CLI launcher that talks to
the running instance -- so the caller's full-screen TUI never has to step aside
the way it must for a terminal editor like vim.

Detection keys off the environment variables each editor injects into its
integrated terminal:

    VS Code     TERM_PROGRAM == "vscode"
    JetBrains   TERMINAL_EMULATOR == "JetBrains-JediTerm"   (CLion, IntelliJ, …)
    Zed         TERM_PROGRAM == "zed"  or  ZED_TERM == "true"

Call `detect_ide()`; it returns an `Ide` (whose `.open(path, line, column)`
does the hand-off and `.label` names it for a status line) or None for a plain
terminal. Standard library only; works the same on macOS, Linux, and Windows.
Lives next to the scripts that import it, so `uv run git-open.py` finds it.
"""

import os
import shutil
import subprocess
import sys


class Ide(object):
    """An editor we can hand a file to. `name` is a stable key, `label` is how
    it reads in a status message. Subclasses implement open()."""

    def __init__(self, name, label):
        self.name = name
        self.label = label

    def open(self, path, line=1, column=1):
        """Open absolute `path` at line/column in the running editor and return
        at once. May raise OSError -- FileNotFoundError in particular when a CLI
        launcher isn't installed on PATH."""
        raise NotImplementedError

    def open_error(self):
        """A short status hint for when open() raised FileNotFoundError."""
        return "{0} not found on PATH".format(self.name)


class VsCode(Ide):
    """VS Code, reached through its `vscode://file/<path>:<line>:<column>` URL
    scheme handed to the platform's URL opener."""

    def __init__(self):
        super().__init__("vscode", "VS Code")

    def open(self, path, line=1, column=1):
        _open_url("vscode://file{0}:{1}:{2}".format(path, line, column))


class JetBrains(Ide):
    """A JetBrains IDE (CLion, IntelliJ, PyCharm, …), reached through its CLI
    launcher, which routes the file to the running instance as a new tab. `name`
    is the launcher binary, so the inherited open_error() reads correctly."""

    def open(self, path, line=1, column=1):
        # The launchers share a `--line <n> <path>` syntax. They don't reliably
        # accept a column flag, so we drop `column` rather than risk a bad flag
        # aborting the open.
        subprocess.call([self.name, "--line", str(line), path])


class Zed(Ide):
    """Zed, reached through its `zed` launcher using path:line:column syntax,
    which routes the file to the focused window as a new tab."""

    def __init__(self):
        super().__init__("zed", "Zed")

    def open(self, path, line=1, column=1):
        subprocess.call(["zed", "{0}:{1}:{2}".format(path, line, column)])

    def open_error(self):
        return "zed not found on PATH (run 'zed: install cli')"


def _open_url(url):
    """Hand a URL to the platform's own handler, fire-and-forget."""
    if sys.platform == "darwin":
        subprocess.call(["open", url])
    elif os.name == "nt":
        os.startfile(url)              # the platform's URL handler (Windows only)
    else:
        subprocess.call(["xdg-open", url])


# JetBrains products, in the order we'd guess if all else fails. Each entry is
# (substrings, launcher, label): the substrings are what appears in the macOS
# bundle id (XPC_SERVICE_NAME) or in an owning process's command line, the
# launcher is the CLI that talks to the running instance, and the label names it
# for a status line. We match on "intellij" rather than "idea" for IntelliJ: the
# bundle id and app path both contain "IntelliJ", and "idea" is too generic a
# word to match safely against a whole command line. The launcher stays "idea".
_JETBRAINS = [
    (("intellij",), "idea", "IntelliJ IDEA"),
    (("pycharm",), "pycharm", "PyCharm"),
    (("clion",), "clion", "CLion"),
    (("webstorm",), "webstorm", "WebStorm"),
    (("goland",), "goland", "GoLand"),
    (("rider",), "rider", "Rider"),
    (("phpstorm",), "phpstorm", "PhpStorm"),
    (("rubymine",), "rubymine", "RubyMine"),
    (("datagrip",), "datagrip", "DataGrip"),
]


def _jetbrains_from_blob(blob):
    """First (launcher, label) whose substring appears in `blob`, or None."""
    blob = (blob or "").lower()
    for substrings, launcher, label in _JETBRAINS:
        if any(s in blob for s in substrings):
            return launcher, label
    return None


def _jetbrains_ancestor():
    """The JetBrains IDE that spawned this shell, found by walking the process
    tree -- the owning IDE is necessarily an ancestor, so this is ground truth
    on multi-IDE machines. Returns (launcher, label) or None. Uses `ps`, so it's
    a no-op on Windows (where XPC_SERVICE_NAME is absent too and we fall back to
    a PATH guess)."""
    if os.name == "nt":
        return None
    pid = os.getppid()
    for _ in range(30):                # bound the walk; shells aren't deep
        if pid <= 1:
            break
        try:
            line = subprocess.run(
                ["ps", "-o", "ppid=,command=", "-p", str(pid)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            ).stdout.strip()
        except OSError:
            break
        if not line:
            break
        ppid, _, command = line.partition(" ")
        found = _jetbrains_from_blob(command)
        if found:
            return found
        try:
            pid = int(ppid)
        except ValueError:
            break
    return None


def _jetbrains_owner():
    """Which JetBrains IDE owns this terminal, as an Ide. Prefer macOS's
    XPC_SERVICE_NAME (the bundle id is right there in the environment), then the
    process tree. Only if both come up empty do we guess the first launcher on
    PATH -- a guess, not detection, so it's last."""
    found = (_jetbrains_from_blob(os.environ.get("XPC_SERVICE_NAME"))
             or _jetbrains_ancestor())
    if found:
        return JetBrains(*found)
    for substrings, launcher, label in _JETBRAINS:
        if shutil.which(launcher):
            return JetBrains(launcher, label)
    return JetBrains("idea", "JetBrains IDE")


def detect_ide():
    """The editor whose integrated terminal we're in, as an Ide, or None for a
    plain terminal. VS Code and Zed are told apart by TERM_PROGRAM; a JetBrains
    IDE announces itself through TERMINAL_EMULATOR with a value shared across
    CLion, IntelliJ, PyCharm, and the rest, so we then resolve *which* product
    owns the terminal rather than assuming one."""
    term_program = os.environ.get("TERM_PROGRAM")
    if term_program == "vscode":
        return VsCode()
    if os.environ.get("TERMINAL_EMULATOR") == "JetBrains-JediTerm":
        return _jetbrains_owner()
    if term_program == "zed" or os.environ.get("ZED_TERM") == "true":
        return Zed()
    return None
