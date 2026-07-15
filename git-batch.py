#!/usr/bin/env python3
"""Run one git command across every git repo in the current directory.

Scans the immediate subdirectories of the current directory (no recursion)
for git repositories — anything with a `.git` entry, so normal clones,
worktree checkouts, and submodule checkouts all count — then runs the given
git command in each one and collates the results.

Everything after the script name is passed to git verbatim, so any git
command works:

    git-batch.py status -sb
    git-batch.py fetch --prune
    git-batch.py pull --ff-only
    git-batch.py log -1 --oneline

Repos are processed in parallel (handy for network commands like fetch and
pull); pass -j 1 for strictly sequential runs. Regardless of how the work is
scheduled, the report is deterministic: one section per repo in sorted name
order, each with the command's combined stdout/stderr, followed by a summary
line counting successes and failures (failed repos are listed by name).

Use --quiet to keep the report short: only repos whose command failed get a
section; everything else is folded into the summary.

Since the command is passed through as-is, there is no confirmation and no
safety net — `git-batch.py reset --hard` really will reset every repo.

Examples:
    git-batch.py status -sb
    git-batch.py -q fetch --prune
    git-batch.py -j 1 pull --ff-only
    git-batch.py -C ~/projects branch --show-current

Exit status:
    0   the command succeeded in every repo (or there was nothing to do)
    1   no repos found, or the command failed in at least one repo
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

__version__ = "1.0.0"


def find_repos(root: str):
    """Return the sorted names of immediate subdirectories that are git repos.

    A directory counts as a repo if it contains a `.git` entry — a directory
    for normal clones, a file for worktrees and submodules.
    """
    repos = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError as exc:
        raise SystemExit(f"Error: cannot list '{root}': {exc}")
    for entry in entries:
        if entry.is_dir(follow_symlinks=False) and \
                os.path.exists(os.path.join(entry.path, ".git")):
            repos.append(entry.name)
    return repos


def run_in_repo(root: str, repo: str, git_args):
    """Run `git <git_args>` inside `repo`. Returns (repo, returncode, output).

    stdout and stderr are merged so the report reads like the command would
    have looked in a terminal, just prefixed per repo.
    """
    result = subprocess.run(
        ["git", "-C", os.path.join(root, repo), *git_args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    return repo, result.returncode, result.stdout


def err(*args):
    """Print to stderr, flushing stdout first so the two streams stay ordered."""
    sys.stdout.flush()
    print(*args, file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="git-batch.py",
        description="Run one git command across every git repo in the current directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "notes:\n"
            "  Everything after the options is passed to git verbatim, so any\n"
            "  git command works. Use '--' before git args that start with a\n"
            "  dash if argparse claims them (e.g. git-batch.py -- -c core.pager=cat log -1).\n"
            "  There is no confirmation: a destructive command runs in every repo.\n"
            "\n"
            "examples:\n"
            "  git-batch.py status -sb\n"
            "  git-batch.py -q fetch --prune\n"
            "  git-batch.py -j 1 pull --ff-only\n"
        ),
    )
    parser.add_argument("git_args", nargs=argparse.REMAINDER, metavar="COMMAND",
                        help="the git command and its arguments (passed to git verbatim)")
    parser.add_argument("-C", dest="directory", default=".", metavar="DIR",
                        help="scan this directory instead of the current one")
    parser.add_argument("-j", "--jobs", type=int, default=None, metavar="N",
                        help="repos to process in parallel (default: scales with "
                             "CPU count; 1 disables parallelism)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only print sections for repos where the command failed")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    # REMAINDER keeps a leading '--' separator; drop it so git never sees it.
    git_args = args.git_args
    if git_args and git_args[0] == "--":
        git_args = git_args[1:]
    if not git_args:
        parser.error("no git command given (e.g. git-batch.py status -sb)")

    root = args.directory
    repos = find_repos(root)
    if not repos:
        err(f"Error: no git repositories found in '{os.path.abspath(root)}'.")
        return 1

    command = "git " + " ".join(git_args)
    print(f"Running '{command}' in {len(repos)} repo(s)...\n")

    jobs = args.jobs if args.jobs and args.jobs > 0 else min(32, (os.cpu_count() or 4) * 4)
    jobs = min(jobs, len(repos))
    if jobs == 1:
        results = [run_in_repo(root, repo, git_args) for repo in repos]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(lambda r: run_in_repo(root, r, git_args), repos))

    # `results` is already in sorted repo order (map preserves input order).
    failed = []
    for repo, returncode, output in results:
        ok = returncode == 0
        if not ok:
            failed.append(repo)
        if args.quiet and ok:
            continue
        status = "ok" if ok else f"failed (exit {returncode})"
        print(f"=== {repo}  [{status}]")
        body = output.rstrip("\n")
        print(body if body else "(no output)")
        print()

    succeeded = len(repos) - len(failed)
    print(f"Summary: {succeeded} succeeded, {len(failed)} failed "
          f"(of {len(repos)} repo(s)).")
    if failed:
        for repo in failed:
            print(f"  - {repo}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
