#!/usr/bin/env python3
"""An interactive, full-screen branch manager for Git -- pick a branch the way
you would in fzf or lazygit, then switch to it, delete it, or rename it.

The picker is *modal*, in the spirit of vim:

  NORMAL mode (the default)
    j / k  or  Up / Down     move the highlight cursor
    g / G                    jump to the top / bottom
    h / Left                 collapse the folder (or hop to the parent folder)
    l / Right                expand the folder (or descend into it)
    <digits> then Enter      select a branch by its number (the cursor follows
                             along as you type, so 12<Enter> lands on branch 12)
    Enter                    expand/collapse a folder, or switch to a branch
    D                        delete the branch under the cursor (asks first)
    R                        rename the branch under the cursor (local only)
    /                        enter FILTER mode
    Tab  (or r)              toggle remote branches in / out of the list
    q / Esc                  quit without doing anything

  FILTER mode (entered with /)
    type                     a regular expression that filters the branch names
                             (letters like D/R are query text here, not commands)
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

Each of Enter/D/R acts on a single branch. The picker closes first, then the
action runs on the ordinary terminal so it can confirm (delete) or prompt for
the new name (rename). Rename takes the **full** name -- `JP/foo/bar` can become
`foo-bar` -- via `git branch -m`, and works on local branches only. Delete is a
single-branch shortcut; use delete-branch.py to tick off and remove several at
once.

Runs on macOS, Linux, and Windows using only the standard library (raw terminal
mode + ANSI escapes; no curses, no third-party packages). Shares its navigation
engine with delete-branch.py via the neighbouring branch_tui module.

Exit status:
    0   a branch was switched/deleted/renamed, or you quit without choosing
    1   not inside a Git repository, not an interactive terminal, or the
        underlying git command failed
"""

import argparse
import os
import subprocess
import sys

from branch_tui import (Picker, TerminalSession, get_branches, git,
                        in_git_repo, split_remote_ref)

__version__ = "2.0.0"


class BranchPicker(Picker):
    title = "Branches"

    def __init__(self, show_remotes, use_color):
        super(BranchPicker, self).__init__(show_remotes, use_color)
        self.action = None         # "switch" | "delete" | "rename"
        self.target = None         # the Branch the action applies to

    def _act_on_branch(self, action, branch):
        """Record an action on a branch and end the loop so the caller runs it."""
        self.action = action
        self.target = branch
        self.result = branch                   # non-None -> run() stops
        return False

    def on_enter(self):
        row = self.current_row()
        if row is None:
            return True
        self.pending = ""
        if row["type"] == "folder":
            self.toggle_folder(row)
            return True
        return self._act_on_branch("switch", row["branch"])

    def on_extra_key(self, key):
        # D/R are commands only in NORMAL mode; in FILTER mode a letter is part
        # of the regex, so fall through (return None) and let it type.
        if self.mode != "normal":
            return None
        if key == "D":
            return self._request_delete()
        if key == "R":
            return self._request_rename()
        return None

    def _request_delete(self):
        row = self.current_row()
        if row is None or row["type"] == "folder":
            return True                        # not on a branch -> ignore
        br = row["branch"]
        if br.is_current:
            self.status = "can't delete the current branch"
            return True
        return self._act_on_branch("delete", br)

    def _request_rename(self):
        row = self.current_row()
        if row is None or row["type"] == "folder":
            return True
        br = row["branch"]
        if br.kind == "remote":
            self.status = "rename works on local branches only"
            return True
        return self._act_on_branch("rename", br)

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
        hint = (" j/k move · ⏎ switch · D delete · R rename · / filter · "
                "h/l fold · Tab remotes · q quit")
        if self.pending:
            hint += "    #" + self.pending
        if self.status:
            hint += "    " + self.status
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

    proc = subprocess.run(cmd, stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace")
    err = proc.stderr or ""
    if proc.returncode != 0 and ("is not a git command" in err or "unknown switch" in err):
        proc = subprocess.run(_checkout_equivalent(cmd),
                              stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace")
        err = proc.stderr or ""

    if proc.returncode == 0:
        print("Switched to '{0}'.".format(target))
    else:
        sys.stderr.write(err)
    return proc.returncode


def _yes(prompt):
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def do_delete(branch, remote_names):
    """Delete a single branch (local or remote) after a y/N confirm. A local
    branch that isn't fully merged is refused by `git branch -d`; we then offer
    to force it with -D. Returns a process exit code."""
    if branch.kind == "remote":
        remote, on_remote = split_remote_ref(branch.name, remote_names)
        print("This DELETES the remote branch '{0}' "
              "(git push {1} --delete {2}) — it updates the remote for "
              "everyone!".format(branch.name, remote, on_remote))
        if not _yes("Delete remote branch '{0}'? [y/N] ".format(branch.name)):
            print("Aborted. Nothing was deleted.")
            return 0
        proc = git(["push", remote, "--delete", on_remote])
        if proc.returncode == 0:
            print("Deleted remote branch '{0}'.".format(branch.name))
            return 0
        sys.stderr.write(proc.stderr)
        return 1

    if not _yes("Delete local branch '{0}'? [y/N] ".format(branch.name)):
        print("Aborted. Nothing was deleted.")
        return 0
    proc = git(["branch", "-d", branch.name])
    if proc.returncode == 0:
        print("Deleted branch '{0}'.".format(branch.name))
        return 0
    if "not fully merged" in (proc.stderr or ""):
        print("'{0}' is not fully merged.".format(branch.name))
        if not _yes("Force-delete with 'git branch -D' (this discards its "
                    "unmerged commits)? [y/N] "):
            print("Kept '{0}'.".format(branch.name))
            return 0
        proc = git(["branch", "-D", branch.name])
        if proc.returncode == 0:
            print("Force-deleted branch '{0}'.".format(branch.name))
            return 0
    sys.stderr.write(proc.stderr)
    return 1


def do_rename(branch):
    """Rename a local branch to a new full name via `git branch -m`. Prompts for
    the name on the ordinary terminal. Returns a process exit code."""
    print("Rename local branch '{0}'.".format(branch.name))
    try:
        new = input("New name (full): ").strip()
    except EOFError:
        new = ""
    if not new:
        print("Aborted. No new name given.")
        return 0
    if new == branch.name:
        print("Name unchanged.")
        return 0
    proc = git(["branch", "-m", branch.name, new])
    if proc.returncode == 0:
        print("Renamed '{0}' -> '{1}'.".format(branch.name, new))
        return 0
    sys.stderr.write(proc.stderr)
    return 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="git-branch.py",
        description="Interactively pick a Git branch (vim-style) and switch, "
                    "delete, or rename it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "keys:\n"
            "  j/k or arrows  move      g/G  top/bottom    h/l  collapse/expand\n"
            "  Enter          switch    D    delete        R    rename\n"
            "  <num> + Enter  select    /    filter        Tab  toggle remotes\n"
            "  Esc            back/clear                     q   quit\n"
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
        sys.stderr.write("error: git-branch needs an interactive terminal\n")
        return 1

    branches, _, _ = get_branches(args.remotes)
    if not branches:
        sys.stderr.write("error: no branches to choose from\n")
        return 1

    use_color = not args.no_color and "NO_COLOR" not in os.environ
    picker = BranchPicker(args.remotes, use_color)

    with TerminalSession():
        picker.run()

    if picker.result is None or picker.action is None:
        return 0                               # quit without choosing
    if picker.action == "switch":
        return do_switch(picker.target, picker.locals_set)
    if picker.action == "delete":
        return do_delete(picker.target, picker.remote_names)
    if picker.action == "rename":
        return do_rename(picker.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
