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


class Clion(Ide):
    """CLion (or another JetBrains IDE), reached through the `clion` launcher,
    which routes the file to the running instance as a new tab."""

    def __init__(self):
        super().__init__("clion", "CLion")

    def open(self, path, line=1, column=1):
        subprocess.call(["clion", "--line", str(line), path])

    def open_error(self):
        return "clion not found on PATH"


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


def detect_ide():
    """The editor whose integrated terminal we're in, as an Ide, or None for a
    plain terminal. VS Code and Zed are told apart by TERM_PROGRAM; a JetBrains
    IDE announces itself through TERMINAL_EMULATOR with a value shared across
    CLion, IntelliJ, PyCharm, and the rest."""
    term_program = os.environ.get("TERM_PROGRAM")
    if term_program == "vscode":
        return VsCode()
    if os.environ.get("TERMINAL_EMULATOR") == "JetBrains-JediTerm":
        return Clion()
    if term_program == "zed" or os.environ.get("ZED_TERM") == "true":
        return Zed()
    return None
