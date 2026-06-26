#!/usr/bin/env python3
"""An interactive, full-screen branch *deleter* for Git -- the same vim-style
picker as switch-branch.py, but instead of switching you tick off as many
branches as you like (local and remote) and delete them in one go.

  NORMAL mode (the default)
    j / k  or  Up / Down     move the highlight cursor
    g / G                    jump to the top / bottom
    h / Left                 collapse the folder (or hop to the parent folder)
    l / Right                expand the folder (or descend into it)
    Space  or  Enter         check / uncheck the branch here; on a folder Enter
                             expands/collapses it while Space checks the whole
                             folder (so Enter never deletes -- same as switch-branch)
    <digits>                 jump the cursor to a branch by number
    d                        delete everything that's checked (asks first)
    F                        toggle force: git branch -D instead of -d
    /                        enter FILTER mode
    Tab  (or r)              toggle remote branches in / out of the list
    q / Esc                  quit without deleting

  FILTER mode (entered with /)
    type                     a regular expression that filters the branch names
    Space  or  Enter         check / uncheck the highlighted branch
    Up / Down                move the cursor among the matches
    Backspace                edit the expression
    Esc                      clear the filter and return to NORMAL
    (to delete after filtering, press Esc then d -- your checks are kept)

Checking a **folder** checks every branch under it (a `[~]` box means only some
of its branches are checked). The branch you're currently on is protected -- it
has no checkbox, because Git won't let you delete the branch you're standing on.

Nothing is deleted until you confirm. On Enter the picker closes and prints
exactly what will go -- **local** deletions (`git branch -d`, or `-D` with
force) and **remote** ones (`git push <remote> --delete`, which updates the
shared remote) called out separately -- then asks for a single y/N.

Local branches that aren't fully merged are refused by `git branch -d`; press F
to force (`-D`) if you really mean it. Remote deletions are always forced (that
is how `git push --delete` works).

Runs on macOS, Linux, and Windows using only the standard library. Shares its
navigation engine with switch-branch.py via the neighbouring branch_tui module.

Exit status:
    0   deletions ran (or you quit / aborted without deleting)
    1   not inside a Git repository, not an interactive terminal, or one or more
        deletions failed
"""

import argparse
import os
import sys
from collections import defaultdict

from branch_tui import (Picker, TerminalSession, branches_under, get_branches,
                        git, in_git_repo, split_remote_ref)

__version__ = "1.1.0"


class DeletePicker(Picker):
    title = "Delete branches"

    def __init__(self, show_remotes, use_color, force):
        super(DeletePicker, self).__init__(show_remotes, use_color)
        self.checked = set()       # Branch values marked for deletion
        self.force = force

    # -- checking ------------------------------------------------------------
    def _folder_targets(self, folder_path):
        """Deletable (non-current) branches under a folder."""
        return [b for b in branches_under(self.all_branches(), folder_path)
                if not b.is_current]

    def toggle_check_current(self):
        row = self.current_row()
        if row is None:
            return
        if row["type"] == "folder":
            targets = self._folder_targets(row["node"].path)
            if targets and all(b in self.checked for b in targets):
                self.checked.difference_update(targets)
            else:
                self.checked.update(targets)
        else:
            br = row["branch"]
            if br.is_current:
                return                         # protected: can't delete HEAD
            self.checked.discard(br) if br in self.checked else self.checked.add(br)

    # -- hooks ---------------------------------------------------------------
    def on_extra_key(self, key):
        # In FILTER mode, letters are regex input, so only Space (which is never
        # part of a branch name) acts as a command there; 'd'/'F' fall through to
        # the query. In NORMAL mode they are commands.
        if key == " ":
            self.toggle_check_current()
            return True
        if self.mode != "normal":
            return None
        if key == "F":
            self.force = not self.force
            return True
        if key == "d":                         # the dedicated delete trigger
            if not self.checked:
                return True                    # nothing ticked -> no-op
            self.result = "delete"             # close the picker; caller confirms
            return False
        return None

    def on_enter(self):
        # Enter keeps its switch-branch meaning so it never deletes: open/close a
        # folder, or tick/untick a branch. Deletion is on its own key ('d').
        row = self.current_row()
        if row is None:
            return True
        self.pending = ""
        if row["type"] == "folder":
            self.toggle_folder(row)
        else:
            self.toggle_check_current()
        return True

    def checkbox(self, row):
        if row["type"] == "folder":
            targets = self._folder_targets(row["node"].path)
            if not targets:
                return "    "
            checked = sum(1 for b in targets if b in self.checked)
            box = "[x]" if checked == len(targets) else "[ ]" if checked == 0 else "[~]"
            return box + " "
        br = row["branch"]
        if br.is_current:
            return "    "                      # protected, no checkbox
        return ("[x] " if br in self.checked else "[ ] ")

    def branch_suffix(self, branch):
        return " *  (current)" if branch.is_current else ""

    def header_extra(self):
        extra = "    {0} checked".format(len(self.checked))
        if self.force:
            extra += "    ⚠ FORCE (-D)"
        return extra

    def footer(self):
        if self.mode == "filter":
            return (" ↑↓ move · Space/⏎ check · ⌫ del · Esc then d to delete"
                    + self._filter_suffix())
        return (" Space/⏎ check · d delete · F force · / filter · h/l fold · "
                "Tab remotes · q quit")


def confirm_and_delete(checked, force, remote_names):
    """Print the deletion plan, get a y/N, then carry it out. Returns an exit
    code (0 unless a deletion failed)."""
    locals_ = sorted((b for b in checked if b.kind == "local"), key=lambda b: b.name)
    remotes_ = sorted((b for b in checked if b.kind == "remote"), key=lambda b: b.name)

    print("These branches will be DELETED:")
    if locals_:
        print("\n  Local  (git branch {0}):".format("-D, force" if force else "-d"))
        for b in locals_:
            print("    {0}".format(b.name))
    if remotes_:
        print("\n  Remote (git push --delete — updates the remote for everyone!):")
        for b in remotes_:
            print("    {0}".format(b.name))

    total = len(locals_) + len(remotes_)
    try:
        answer = input("\nDelete {0} branch(es)? [y/N] ".format(total)).strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("Aborted. Nothing was deleted.")
        return 0

    failed = False

    flag = "-D" if force else "-d"
    for b in locals_:
        proc = git(["branch", flag, b.name])
        if proc.returncode == 0:
            print("deleted local  {0}".format(b.name))
        else:
            sys.stderr.write(proc.stderr)
            failed = True

    # One push per remote deletes all of its branches in a single round-trip.
    by_remote = defaultdict(list)
    refs = {}
    for b in remotes_:
        remote, on_remote = split_remote_ref(b.name, remote_names)
        by_remote[remote].append(on_remote)
        refs[(remote, on_remote)] = b.name
    for remote, names in by_remote.items():
        proc = git(["push", remote, "--delete"] + names)
        if proc.returncode == 0:
            for n in names:
                print("deleted remote {0}".format(refs[(remote, n)]))
        else:
            sys.stderr.write(proc.stderr)
            failed = True

    return 1 if failed else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="delete-branch.py",
        description="Interactively check off Git branches (local and remote) and delete them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "keys:\n"
            "  j/k or arrows  move       Space/Enter  check    h/l  collapse/expand\n"
            "  d              delete     F            force    /    filter\n"
            "  Tab            remotes    q            quit     Esc  back/clear\n"
        ),
    )
    parser.add_argument("-r", "--remotes", action="store_true",
                        help="start with remote branches already included")
    parser.add_argument("-f", "--force", action="store_true",
                        help="start in force mode (git branch -D)")
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
        sys.stderr.write("error: delete-branch needs an interactive terminal\n")
        return 1

    branches, _, _ = get_branches(args.remotes)
    if not branches:
        sys.stderr.write("error: no branches to choose from\n")
        return 1

    use_color = not args.no_color and "NO_COLOR" not in os.environ
    picker = DeletePicker(args.remotes, use_color, args.force)

    with TerminalSession():
        outcome = picker.run()

    if outcome != "delete" or not picker.checked:
        return 0
    return confirm_and_delete(picker.checked, picker.force, picker.remote_names)


if __name__ == "__main__":
    raise SystemExit(main())
