#!/usr/bin/env python3
"""Update this collection of scripts in place, by pulling from git.

The scripts are run from their git checkout — the directory holding them is
on PATH, and there is no separate install step — so updating them is just
bringing that checkout up to date. This script finds the repository it is
itself stored in and fast-forwards it, then reports what changed:

    update-scripts.py

Because it locates the repository from its own path rather than a hardcoded
one, it updates whichever checkout it was run from, wherever that lives on
whichever machine.

It is deliberately conservative — it is an update button, not a merge tool,
and it should never be the reason unfinished work goes missing:

  - Uncommitted changes stop it before anything is touched, and are listed
    so you can deal with them. --stash overrides that, setting the changes
    aside and restoring them afterwards.
  - The update is fast-forward only. If the branch has diverged from its
    upstream — local commits *and* upstream commits — it stops and shows how
    to resolve it, rather than quietly rebasing or writing a merge commit.
  - Nothing is pushed, and no branch is ever switched.

Updating the very script that is running is safe: Python reads the source
fully before executing it, so replacing the file mid-run cannot affect the
run in progress. The new version simply takes effect next time.

Examples:
    update-scripts.py               fast-forward the checkout and report
    update-scripts.py --dry-run     show what would arrive, change nothing
    update-scripts.py --stash       set local changes aside, update, restore
    update-scripts.py --quiet       report only the summary line

Exit status:
    0   updated, or already up to date
    1   uncommitted changes, diverged branch, no upstream, not a repo, or git
        failed
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import os
import subprocess
import sys

__version__ = "1.0.0"


def git(repo, *args):
    """Run a git command in `repo`; return (returncode, stdout, stderr).

    Never raises for a non-zero status — every caller here has a more useful
    thing to say about a failure than a traceback.

    Output is only *right*-stripped. Leading whitespace is load-bearing in
    what we ask git for: porcelain status codes are a two-column field where
    ' M' (modified) and 'M ' (staged) differ by it, and --stat indents every
    line to align its columns. Stripping both ends would silently corrupt the
    first line of each and leave the rest correct.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise SystemExit("Error: 'git' is not on PATH.")
    return proc.returncode, proc.stdout.rstrip("\n"), proc.stderr.strip()


def git_ok(repo, *args):
    """Run a git command, returning stdout; exit with git's own error if it fails."""
    code, out, err = git(repo, *args)
    if code != 0:
        raise SystemExit(f"Error: git {' '.join(args)} failed: {err or out}")
    return out


def find_repo():
    """Return the root of the git repository holding this script.

    Resolved through symlinks first: the script is typically reached through
    a PATH entry, and on some setups that entry is itself a link, so the
    script's own real location — not the caller's cwd — is what identifies
    the checkout to update.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    code, out, _ = git(here, "rev-parse", "--show-toplevel")
    if code != 0:
        raise SystemExit(
            f"Error: {here} is not inside a git repository, so there is "
            "nothing to update from."
        )
    return out


def describe_status(entries):
    """Format porcelain status lines for display, most useful detail first."""
    return "\n".join(f"  {line}" for line in entries)


def error(*message):
    """Print to stderr, after flushing stdout so the two stay in order.

    The streams are buffered differently the moment output is not a terminal
    — stdout in blocks, stderr not at all — so an unflushed "Fetching…" would
    surface *after* the error explaining why the fetch never happened.
    """
    sys.stdout.flush()
    print(*message, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Update this collection of scripts in place, by pulling from git.",
        epilog="Fast-forward only: it never rebases, merges, pushes, or switches branch.",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="fetch and report what would be updated, but change nothing",
    )
    parser.add_argument(
        "-s", "--stash",
        action="store_true",
        help="set uncommitted changes aside during the update, then restore them",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="print only the summary line",
    )
    parser.add_argument("-V", "--version", action="version", version=__version__)
    args = parser.parse_args()

    repo = find_repo()

    def say(*message):
        if not args.quiet:
            print(*message)

    branch = git_ok(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        error(
            f"Error: {repo} has a detached HEAD (no branch to update).\n"
            "Check out a branch first.",
        )
        return 1

    code, upstream, _ = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if code != 0:
        error(
            f"Error: branch '{branch}' has no upstream, so there is nothing to pull.\n"
            f"Set one with: git -C {repo} branch --set-upstream-to=origin/{branch}",
        )
        return 1
    remote = upstream.split("/", 1)[0]

    # Check for local changes before fetching: if we are going to refuse, do
    # it without side effects and without waiting on the network.
    dirty = [line for line in git_ok(repo, "status", "--porcelain").splitlines() if line]
    stashed = False
    if dirty and not args.dry_run:
        if not args.stash:
            error(
                f"Error: {len(dirty)} uncommitted change(s) in {repo}:\n"
                f"{describe_status(dirty)}\n\n"
                "Commit or stash them first, or re-run with --stash."
            )
            return 1
        # -u so untracked files travel with the stash; leaving them behind
        # could let an incoming commit collide with one mid-update.
        code, _, err = git(repo, "stash", "push", "-u", "-m", "update-scripts")
        if code != 0:
            error(f"Error: could not stash local changes: {err}")
            return 1
        stashed = True
        say(f"Stashed {len(dirty)} local change(s).")

    def restore():
        """Pop the stash, if we made one. Reports rather than raises."""
        if not stashed:
            return 0
        # A conflicting pop reports which files clashed on *stdout* and only
        # incidental noise on stderr, so both are needed to say anything
        # useful about what went wrong.
        code, out, err = git(repo, "stash", "pop")
        if code != 0:
            detail = "\n".join(part for part in (out, err) if part)
            error(
                f"\nWarning: your changes are still stashed — restoring them hit a "
                f"conflict:\n{detail}\n"
                f"\nThey are safe; recover them with: git -C {repo} stash pop"
            )
            return 1
        say(f"Restored {len(dirty)} local change(s).")
        return 0

    say(f"Fetching {remote}…")
    code, _, err = git(repo, "fetch", "--prune", remote)
    if code != 0:
        error(f"Error: fetch from {remote} failed: {err}")
        restore()
        return 1

    # --left-right --count gives "<ahead>\t<behind>" for HEAD vs upstream, the
    # one call that distinguishes all four cases below.
    counts = git_ok(repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    ahead, behind = (int(part) for part in counts.split())

    if behind == 0:
        extra = f" ({ahead} unpushed commit(s) of your own)" if ahead else ""
        print(f"Already up to date with {upstream}{extra}.")
        restore()
        return 0

    if ahead:
        error(
            f"Error: {branch} has diverged from {upstream}\n"
            f"  {ahead} local commit(s) not on {remote}\n"
            f"  {behind} commit(s) on {remote} not local\n\n"
            f"Not fast-forwardable. Resolve with:\n"
            f"  git -C {repo} rebase {upstream}",
        )
        restore()
        return 1

    incoming = git_ok(repo, "log", "--oneline", f"HEAD..{upstream}")
    if args.dry_run:
        print(f"{behind} commit(s) would be applied to {branch}:")
        print("\n".join(f"  {line}" for line in incoming.splitlines()))
        if dirty:
            print(f"\n({len(dirty)} uncommitted change(s) would block this; "
                  "use --stash.)")
        return 0

    before = git_ok(repo, "rev-parse", "HEAD")
    code, _, err = git(repo, "merge", "--ff-only", upstream)
    if code != 0:
        error(f"Error: fast-forward failed: {err}")
        restore()
        return 1

    if not args.quiet:
        print(f"\nUpdated {branch}: {before[:7]}..{git_ok(repo, 'rev-parse', 'HEAD')[:7]}")
        print("\n".join(f"  {line}" for line in incoming.splitlines()))
        changed = git_ok(repo, "diff", "--stat", f"{before}..HEAD")
        if changed:
            print(f"\n{changed}")

    failed = restore()
    say()  # blank line before the summary, but not when it is the only line
    print(f"Summary: {behind} commit(s) applied to {branch} in {repo}.")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
