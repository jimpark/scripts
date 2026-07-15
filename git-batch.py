#!/usr/bin/env python3
"""Run one git command across every git repo in the current directory.

Scans the immediate subdirectories of the current directory (no recursion)
for git repositories — anything with a `.git` entry, so normal clones,
worktree checkouts, and submodule checkouts all count — then runs the given
git command in each one and collates the results. Symlinked directories are
followed, deduped by resolved path so a repo reachable under two names only
runs once (the real directory's name is preferred over a symlink's).

Everything after the script name is passed to git verbatim, so any git
command works:

    git-batch.py status -sb
    git-batch.py fetch --prune
    git-batch.py pull --ff-only
    git-batch.py log -1 --oneline

Repos are processed in parallel (handy for network commands like fetch and
pull); pass -j 1 for strictly sequential runs. While repos run, a live
progress counter ([12/80] <last repo finished>) is shown on stderr — only
when stderr is a terminal, so piped/redirected runs stay clean — and any
failure is echoed immediately rather than held until the end. Regardless of
how the work is scheduled, the report is deterministic: one section per repo
in sorted name order, each with the command's combined stdout/stderr,
followed by a summary line counting successes and failures (failed repos are
listed by name).

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
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

__version__ = "1.0.0"


def find_repos(root: str):
    """Return the sorted names of immediate subdirectories that are git repos.

    A directory counts as a repo if it contains a `.git` entry — a directory
    for normal clones, a file for worktrees and submodules. Symlinks to
    directories are followed, but repos are deduped by resolved path so the
    same repo never runs twice; when a real directory and a symlink point at
    the same repo, the real directory's name wins.
    """
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError as exc:
        raise SystemExit(f"Error: cannot list '{root}': {exc}")

    def is_repo(entry):
        # follow_symlinks=True also rejects broken symlinks (is_dir is False).
        return entry.is_dir(follow_symlinks=True) and \
            os.path.exists(os.path.join(entry.path, ".git"))

    # Two passes so a real directory claims its resolved path before any
    # symlink alias of it is considered.
    repos, seen = [], set()
    for pass_symlinks in (False, True):
        for entry in entries:
            if entry.is_symlink() != pass_symlinks or not is_repo(entry):
                continue
            real = os.path.realpath(entry.path)
            if real not in seen:
                seen.add(real)
                repos.append(entry.name)
    return sorted(repos, key=str.lower)


def run_in_repo(root: str, repo: str, git_args):
    """Run `git <git_args>` inside `repo`. Returns (repo, returncode, output).

    stdout and stderr are merged so the report reads like the command would
    have looked in a terminal, just prefixed per repo.

    A batch run can never answer a prompt, and with output captured a prompt
    is an *invisible hang*: git asks on /dev/tty, which the user never sees.
    stdin is closed, but credential prompts, editors, and ssh passphrase
    prompts all bypass stdin and open /dev/tty directly — so each is told to
    fail fast instead: GIT_TERMINAL_PROMPT=0 turns credential prompts into
    errors, GIT_EDITOR=false makes any editor launch fail (use --no-edit or
    -m), and ssh runs in BatchMode (skipped if the user routes ssh through
    their own GIT_SSH_COMMAND/GIT_SSH). Agent-loaded keys, passwordless keys,
    and credential helpers still work — only *asking the user* is disabled.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "false"
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
        env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
    result = subprocess.run(
        ["git", "-C", os.path.join(root, repo), *git_args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
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

    # Live progress on stderr while repos run, so long commands (pulling 80
    # repos...) don't look hung. A single self-overwriting counter line — no
    # ANSI, just \r and space-padding — shown only when stderr is a terminal;
    # failures are echoed as permanent lines the moment they happen. The
    # collated report below is untouched by any of this.
    total = len(repos)
    show_progress = sys.stderr.isatty()

    def clear_progress():
        if show_progress:
            cols = shutil.get_terminal_size().columns
            sys.stderr.write("\r" + " " * (cols - 1) + "\r")
            sys.stderr.flush()

    def progress(done, repo, returncode):
        if not show_progress:
            return
        cols = shutil.get_terminal_size().columns
        if returncode != 0:
            clear_progress()
            print(f"{repo}: failed (exit {returncode})", file=sys.stderr)
        line = f"[{done}/{total}] {repo}"
        sys.stderr.write("\r" + line[:cols - 1].ljust(cols - 1))
        sys.stderr.flush()

    results = {}
    if jobs == 1:
        for done, repo in enumerate(repos, 1):
            _, returncode, output = run_in_repo(root, repo, git_args)
            results[repo] = (returncode, output)
            progress(done, repo, returncode)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(run_in_repo, root, r, git_args) for r in repos]
            for done, future in enumerate(as_completed(futures), 1):
                repo, returncode, output = future.result()
                results[repo] = (returncode, output)
                progress(done, repo, returncode)
    clear_progress()

    # Report in sorted repo order regardless of completion order.
    failed = []
    for repo in repos:
        returncode, output = results[repo]
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
