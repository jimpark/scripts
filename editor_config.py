#!/usr/bin/env python3
"""Shared editor configuration for the file tools (`git-open.py` and
`git-grep.py`): finding, reading, and (on first run) creating `.git-open-config`,
and turning the chosen editor into the argv that opens a file -- optionally at a
specific line.

The config file is a tiny TOML document that lives next to the scripts and is
gitignored, so each user sets their own editor without it ever being committed:

    editor = "vim"
    line   = "+{line} {file}"

`editor` is run as a shell-style command. To open a plain file (git-open) the
file path is appended as the final argument. To open at a line (git-grep) the
`line` template is consulted instead: it is split shell-style and each token has
{file}, {line}, and {column} substituted -- so a path with spaces stays a single
argument. With no config file the template below is written out for the user to
edit, pre-filled with vim.

Standard library only (tomllib, shlex); works the same on macOS, Linux, Windows.
Lives next to the scripts that import it, so `uv run git-open.py` finds it.
"""

import os
import shlex
import tomllib
from pathlib import Path

CONFIG_NAME = ".git-open-config"
CONFIG_PATH = Path(__file__).resolve().parent / CONFIG_NAME

# The default line template (vim-style), used when the config omits `line`.
DEFAULT_LINE = "+{line} {file}"

CONFIG_TEMPLATE = '''\
# Editor configuration for git-open and git-grep.
# Both scripts read this file to decide how to open the file you pick. It lives
# next to the scripts and is gitignored, so customize it freely.
#
# `editor` is run as a shell-style command. git-open appends the file path.
#
# `line` tells git-grep how to open a file at a specific line: it is split
# shell-style and {file}, {line}, {column} are substituted into the pieces.
# Examples by editor:
#
#   editor = "vim"              line = "+{line} {file}"
#   editor = "nvim"             line = "+{line} {file}"
#   editor = "code -g"          line = "{file}:{line}:{column}"   # VS Code
#   editor = "subl"             line = "{file}:{line}:{column}"   # Sublime Text
#   editor = "emacsclient -nw"  line = "+{line} {file}"
#
# Delete the editor line to fall back to $VISUAL, then $EDITOR.
editor = "vim"
line   = "+{line} {file}"
'''


def _env_editor():
    return os.environ.get("VISUAL") or os.environ.get("EDITOR")


def _platform_default():
    return "notepad" if os.name == "nt" else "vi"


def _split(command):
    argv = shlex.split(command, posix=(os.name != "nt"))
    return argv or [_platform_default()]


def load_config():
    """Resolve the editor settings. Returns (editor_argv, line_template,
    notices): editor_argv is the command split into a list, line_template is the
    string used to open at a line, and notices are one-off messages to print
    after the UI exits.

    Editor precedence: the config's `editor`, then $VISUAL, then $EDITOR, then a
    platform default. A missing config file is created from the template (with
    the resolved fallback filled in) so there's something concrete to edit."""
    notices = []
    cfg_editor = None
    line_template = DEFAULT_LINE

    if CONFIG_PATH.exists():
        try:
            data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            notices.append("ignoring {0}: {1}".format(CONFIG_NAME, exc))
            data = {}
        # Accept the keys at the top level or under a [git-open] table.
        section = data.get("git-open")
        if not isinstance(section, dict):
            section = data
        value = section.get("editor")
        if isinstance(value, str) and value.strip():
            cfg_editor = value.strip()
        line_value = section.get("line")
        if isinstance(line_value, str) and line_value.strip():
            line_template = line_value.strip()
    else:
        fallback = _env_editor() or "vim"
        try:
            text = CONFIG_TEMPLATE
            if fallback != "vim":
                text = text.replace('editor = "vim"', 'editor = "{0}"'.format(fallback))
            CONFIG_PATH.write_text(text, encoding="utf-8")
            notices.append("created {0} -- set your editor there.".format(CONFIG_PATH))
        except OSError as exc:
            notices.append("could not create {0}: {1}".format(CONFIG_NAME, exc))

    editor = cfg_editor or _env_editor() or _platform_default()
    return _split(editor), line_template, notices


def open_args(editor_argv, line_template, path, line=None, column=1):
    """Build the full argv to open `path`. With no line, the path is appended as
    a single argument. With a line, the line template is split into tokens first
    and {file}/{line}/{column} substituted into each -- so a path containing
    spaces remains one argument."""
    if line is None:
        return editor_argv + [path]
    tokens = shlex.split(line_template, posix=(os.name != "nt")) or ["{file}"]
    pieces = [t.format(file=path, line=line, column=column) for t in tokens]
    return editor_argv + pieces
