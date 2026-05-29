#!/usr/bin/env python3
"""Delete local Git branches that no longer exist on a remote.

Prunes stale remote-tracking references (git fetch --prune), then compares the
local branches against the branches still present on the remote. Any local
branch with no matching remote branch is listed and, after an interactive y/n
confirmation, deleted. The branch currently checked out is always skipped.

Run this from inside the Git repository you want to clean up.

A candidate branch is only deleted if its work is already in the target branch
(the current branch by default, or --into NAME). "Already in" is detected two
ways, so squash- and rebase-merged PRs count as merged even though Git's own
'git branch -d' would call them "not fully merged":

  * the branch is a direct ancestor of the target (normal/fast-forward merge);
  * 'git cherry' finds an equivalent patch already in the target (the branch's
    diff was applied under a different commit, i.e. squash or rebase merge).

Genuinely unmerged branches are kept and listed. Use --force to skip the check
and delete every candidate regardless (this discards unmerged commits, so
review the printed list first).

Caveat: the merge check compares against the *local* target branch, and
'git fetch' does not fast-forward it. If the target is behind its upstream the
result would be stale, so in safe mode the script detects this and exits with
instructions rather than risk keeping already-merged branches. Pass
--into <remote>/<branch> to compare against the fetched remote-tracking branch,
or --force to skip the check.

Examples:
    prune-branches.py
    prune-branches.py --into main
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


def upstream_of(ref: str):
    """Return the short upstream of `ref` (e.g. 'origin/main'), or None.

    A remote-tracking ref (already current after fetch) or a ref with no
    configured upstream returns None, so callers can skip the staleness check.
    """
    result = git("rev-parse", "--abbrev-ref", "--symbolic-full-name",
                 f"{ref}@{{upstream}}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def behind_count(ref: str, upstream: str) -> int:
    """How many commits `upstream` has that `ref` does not (0 if up to date)."""
    result = git("rev-list", "--count", f"{ref}..{upstream}", check=False)
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def is_merged_into(branch: str, target: str) -> bool:
    """Is `branch`'s work already contained in `target`?

    Handles the three ways a branch's changes can land in the target:
      * fast-forward / merge-commit: the branch tip is an ancestor of target.
      * squash or rebase merge: the branch's commits are gone, but their
        combined patch is already present. We synthesize a commit from the
        branch's tree on top of its merge-base, then ask `git cherry` whether
        an equivalent patch already exists in target (output line "- <sha>").
    """
    # Fast-forward / merge-commit case: tip is reachable from target.
    if git("merge-base", "--is-ancestor", branch, target, check=False).returncode == 0:
        return True

    # Squash/rebase case. Needs a common ancestor to diff against.
    base = git("merge-base", target, branch, check=False)
    if base.returncode != 0 or not base.stdout.strip():
        return False
    base = base.stdout.strip()

    tree = git("rev-parse", f"{branch}^{{tree}}", check=False)
    if tree.returncode != 0:
        return False
    fake_commit = git("commit-tree", tree.stdout.strip(), "-p", base, "-m", "_",
                      check=False)
    if fake_commit.returncode != 0:
        return False

    cherry = git("cherry", target, fake_commit.stdout.strip(), check=False)
    # "- <sha>" means an equivalent patch is already upstream; "+ <sha>" means not.
    return cherry.returncode == 0 and cherry.stdout.lstrip().startswith("-")


def confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() == "y"


def err(*args):
    """Print to stderr, flushing stdout first so the two streams stay ordered."""
    sys.stdout.flush()
    print(*args, file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="prune-branches.py",
        description="Delete local Git branches that no longer exist on a remote.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "notes:\n"
            "  A candidate is deleted only if its work is already in the target\n"
            "  branch (current branch, or --into NAME). Squash- and rebase-merged\n"
            "  branches count as merged via a 'git cherry' patch-equivalence\n"
            "  check. Genuinely unmerged branches are kept; pass --force to\n"
            "  delete every candidate regardless (this discards their commits).\n"
        ),
    )
    parser.add_argument("--remote", default="origin", metavar="NAME",
                        help="remote to compare against (default: origin)")
    parser.add_argument("--into", metavar="NAME",
                        help="branch to test merges against (default: current branch)")
    parser.add_argument("--force", action="store_true",
                        help="delete every candidate, even if not merged (uses 'git branch -D')")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt (non-interactive; use with care)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if not is_inside_work_tree():
        err("Error: not inside a Git repository.")
        return 1

    print(f"Fetching latest remote information from '{args.remote}'...")
    fetch = git("fetch", "--prune", args.remote, check=False, capture=False)
    if fetch.returncode != 0:
        err(f"Error: 'git fetch' from '{args.remote}' failed.")
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

    # Branch to test merges against: --into, else the current branch.
    target = args.into if args.into else "HEAD"

    candidates = []
    for branch in local_branches:
        if branch == current:
            print(f"Skipping current branch: {branch}")
            continue
        if branch not in remote_branches:
            candidates.append(branch)

    if not candidates:
        print(f"No branches to delete. All local branches exist on '{args.remote}'.")
        return 0

    # The merge check uses the *local* target branch, and 'git fetch' does not
    # fast-forward it. If the target is behind its upstream the result would be
    # stale (already-merged branches could be wrongly kept), so in safe mode we
    # stop with instructions rather than guess. --force opts out of the check.
    if not args.force:
        upstream = upstream_of(target)
        if upstream:
            behind = behind_count(target, upstream)
            if behind:
                label = args.into if args.into else current
                err(f"\nError: '{label}' is {behind} commit(s) behind '{upstream}', "
                    f"so the merge check would be stale and might keep branches "
                    f"that are in fact merged.")
                err("Do one of:")
                err(f"  * compare against the up-to-date remote branch:  "
                    f"--into {upstream}")
                err(f"  * update '{label}' first (e.g. 'git pull --ff-only' while "
                    f"it is checked out)")
                err("  * re-run with --force to skip this check")
                return 1

    # Split candidates into those safe to delete and those still unmerged.
    # --force skips the check and treats every candidate as deletable.
    to_delete, kept = [], []
    for branch in candidates:
        if args.force or is_merged_into(branch, target):
            to_delete.append(branch)
        else:
            kept.append(branch)

    if kept:
        target_label = args.into if args.into else current
        print(f"Keeping {len(kept)} branch(es) not yet merged into '{target_label}':")
        for branch in kept:
            print(f"  - {branch}")
        print("  (re-run with --force to delete these anyway)\n")

    if not to_delete:
        print("No merged branches to delete.")
        return 0

    print("The following branches will be deleted:")
    for branch in to_delete:
        print(f"  - {branch}")

    if not args.yes:
        if not confirm("\nDo you want to proceed? (y/n): "):
            print("\nOperation cancelled.")
            return 0

    # We've already confirmed each branch is merged (or --force was given), so
    # use -D: -d would still refuse squash/rebase-merged branches.
    failed = 0
    for branch in to_delete:
        print(f"Deleting branch: {branch}")
        # Don't let one failed delete abort the rest.
        result = git("branch", "-D", branch, check=False)
        if result.returncode != 0:
            failed += 1
            detail = result.stderr.strip()
            err(f"  Failed to delete '{branch}': {detail}")

    if failed:
        print(f"\nCleanup finished with {failed} failure(s).")
        return 1

    print("\nCleanup completed!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
