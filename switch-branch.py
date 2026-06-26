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
mode + ANSI escapes; no curses, no third-party packages). Shares its navigation
engine with delete-branch.py via the neighbouring branch_tui module.

Exit status:
    0   a branch was switched, or you quit without choosing
    1   not inside a Git repository, not an interactive terminal, or the
        underlying `git switch` failed
"""

import argparse
import os
import subprocess
import sys

from branch_tui import Picker, TerminalSession, get_branches, git, in_git_repo

__version__ = "1.1.0"


class SwitchPicker(Picker):
    title = "Switch branch"

    def on_enter(self):
        row = self.current_row()
        if row is None:
            return True
        self.pending = ""
        if row["type"] == "folder":
            self.toggle_folder(row)
            return True
        self.result = row["branch"]            # a Branch -> end the loop
        return False

    def initial_cursor(self):
        for i, row in enumerate(self.rows):    # open on the current branch
            if row["type"] == "branch" and row["branch"].is_current:
                self.cursor = i
                self.cur_id = row["id"]
                break

    def footer(self):
        if self.mode == "filter":
            return (" ↑↓ move · ⏎ switch · ⌫ del · Esc clear · Tab remotes"
                    + self._filter_suffix())
        hint = " j/k move · #+⏎ select · / filter · h/l fold · Tab remotes · q quit"
        if self.pending:
            hint += "    #" + self.pending
        return hint


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
    picker = SwitchPicker(args.remotes, use_color)

    with TerminalSession():
        choice = picker.run()

    if choice is None:
        return 0
    return do_switch(choice, picker.locals_set)


if __name__ == "__main__":
    raise SystemExit(main())
