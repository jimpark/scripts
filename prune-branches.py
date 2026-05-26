#!/usr/bin/env python3
"""Delete local Git branches that no longer exist on a remote.

Prunes stale remote-tracking references (git fetch --prune), then compares the
local branches against the branches still present on the remote. Any local
branch with no matching remote branch is listed and, after an interactive y/n
confirmation, deleted. The branch currently checked out is always skipped.

Run this from inside the Git repository you want to clean up.

By default deletion uses 'git branch -d', which refuses to delete a branch that
is not fully merged into its upstream or HEAD, so it never discards unmerged
work. A branch whose remote was squash- or rebase-merged then deleted appears
unmerged locally and will be skipped; pass --force to remove those (this
discards their commits, so review the printed list first).

Examples:
    prune-branches.py
    prune-branches.py --remote upstream
    prune-branches.py --force --yes

Exit status:
    0   success (branches deleted, or nothing to delete)
    1   not a Git repository, or one or more deletions failed
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import subprocess
import sys

__version__ = "1.0.0"


def git(*args, check=True, capture=True):
    """Run a git command. Returns CompletedProcess; raises on failure if check."""
    return subprocess.run(
        ["git", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def git_lines(*args):
    """Run a git command and return its stdout as a list of non-empty lines."""
    out = git(*args).stdout
    return [line for line in out.splitlines() if line]


def is_inside_work_tree() -> bool:
    result = git("rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() == "y"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="prune-branches.py",
        description="Delete local Git branches that no longer exist on a remote.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "notes:\n"
            "  By default deletion uses 'git branch -d' (merged-only), so it\n"
            "  never discards unmerged work. Branches whose remote was squash-\n"
            "  or rebase-merged look unmerged locally and are kept; pass --force\n"
            "  to delete those (this discards their commits).\n"
        ),
    )
    parser.add_argument("--remote", default="origin", metavar="NAME",
                        help="remote to compare against (default: origin)")
    parser.add_argument("--force", action="store_true",
                        help="use 'git branch -D' (force) instead of '-d' (merged-only)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt (non-interactive; use with care)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if not is_inside_work_tree():
        print("Error: not inside a Git repository.", file=sys.stderr)
        return 1

    print(f"Fetching latest remote information from '{args.remote}'...")
    fetch = git("fetch", "--prune", args.remote, check=False, capture=False)
    if fetch.returncode != 0:
        print(f"Error: 'git fetch' from '{args.remote}' failed.", file=sys.stderr)
        return 1

    print(f"\nChecking for local branches that don't exist on '{args.remote}'...\n")

    # Current branch (may be "HEAD" when detached; that just won't match anything).
    current = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    # --format gives clean names: no '*'/'+' worktree markers, no whitespace.
    local_branches = git_lines("branch", "--format=%(refname:short)")

    # Remote branches come back as 'origin/foo'. Keep only those for our remote,
    # drop the 'origin/HEAD' symref, and strip the leading 'remote/' prefix only.
    prefix = f"{args.remote}/"
    remote_branches = {
        name[len(prefix):]
        for name in git_lines("branch", "-r", "--format=%(refname:short)")
        if name.startswith(prefix) and name != f"{args.remote}/HEAD"
    }

    to_delete = []
    for branch in local_branches:
        if branch == current:
            print(f"Skipping current branch: {branch}")
            continue
        if branch not in remote_branches:
            to_delete.append(branch)

    if not to_delete:
        print(f"No branches to delete. All local branches exist on '{args.remote}'.")
        return 0

    print("The following branches will be deleted:")
    for branch in to_delete:
        print(f"  - {branch}")

    if not args.yes:
        if not confirm("\nDo you want to proceed? (y/n): "):
            print("\nOperation cancelled.")
            return 0

    delete_flag = "-D" if args.force else "-d"
    failed = 0
    for branch in to_delete:
        print(f"Deleting branch: {branch}")
        # Don't let one failed delete (e.g. an unmerged branch without --force)
        # abort the rest.
        result = git("branch", delete_flag, branch, check=False)
        if result.returncode != 0:
            failed += 1
            detail = result.stderr.strip()
            print(f"  Failed to delete '{branch}': {detail}", file=sys.stderr)

    if failed:
        print(f"\nCleanup finished with {failed} failure(s).")
        if not args.force:
            print("Unmerged branches are kept; re-run with --force to delete them.")
        return 1

    print("\nCleanup completed!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
