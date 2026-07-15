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
progress counter ([12/80] running: repo1, repo2, ...) naming the repos still
in flight is shown on stderr — only when stderr is a terminal, so
piped/redirected runs stay clean — and any failure is echoed immediately
rather than held until the end. Regardless of
how the work is scheduled, the report is deterministic: one section per repo
with the command's combined stdout/stderr — successes first, failures last,
each group in sorted name order, so failures sit right above the summary
instead of scrolling away — followed by a summary line counting successes
and failures (failed repos are listed by name).

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
import signal
import subprocess
import sys
import tempfile
import threading
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


def kill_tree(proc):
    """Terminate `proc` and everything it spawned, gently then firmly."""
    def signal_group(sig):
        if os.name == "posix":
            # start_new_session=True made proc its own process group, so this
            # reaches git's children (ssh, remote helpers) too.
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
    signal_group(signal.SIGTERM)  # let git clean up its lock files
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGKILL)
        proc.wait()


def run_in_repo(root: str, repo: str, git_args, timeout=None):
    """Run `git <git_args>` inside `repo`. Returns (repo, returncode, output).

    stdout and stderr are merged so the report reads like the command would
    have looked in a terminal, just prefixed per repo. A `timeout` in seconds
    kills the repo's whole process tree when exceeded; returncode is None.

    A batch run can never answer a prompt, and with output captured a prompt
    is an *invisible hang*: git asks on /dev/tty, which the user never sees.
    stdin is closed, but credential prompts, editors, and ssh passphrase
    prompts all bypass stdin and open /dev/tty directly — so each is told to
    fail fast instead: GIT_TERMINAL_PROMPT=0 turns credential prompts into
    errors, GIT_EDITOR=false makes any editor launch fail (use --no-edit or
    -m), and ssh runs in BatchMode (skipped if the user routes ssh through
    their own GIT_SSH_COMMAND/GIT_SSH). Agent-loaded keys, passwordless keys,
    and credential helpers still work — only *asking the user* is disabled.

    The injected ssh command also enables keepalives: a stalled-but-still-
    ESTABLISHED connection (seen in the wild against ssh.dev.azure.com) would
    otherwise hang a fetch forever, since ssh's default is to wait
    indefinitely. ConnectTimeout bounds the TCP/handshake phase and
    ServerAlive* abort a session after ~60s of silence.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "false"
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
        env["GIT_SSH_COMMAND"] = ("ssh -oBatchMode=yes -oConnectTimeout=15 "
                                  "-oServerAliveInterval=15 -oServerAliveCountMax=4")
    # Capture into a temp file, not a pipe. After fetch/pull git may spawn
    # *background* maintenance (gc --auto), which inherits our capture fd; a
    # pipe would keep this call blocked until that detached child also exits
    # (the "hangs after the last repo" bug), while a plain file lets us return
    # as soon as git itself does.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as buf:
        proc = subprocess.Popen(
            ["git", "-C", os.path.join(root, repo), *git_args],
            stdout=buf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=(os.name == "posix"),
        )
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            returncode = None
        buf.seek(0)
        output = buf.read()
    if returncode is None and not output.endswith("\n") and output:
        output += "\n"
    if returncode is None:
        output += f"git-batch: no result after {timeout}s; killed.\n"
    return repo, returncode, output


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
    parser.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                        help="kill a repo's command (and everything it spawned) "
                             "after this many seconds and report it as timed out "
                             "(default: no limit)")
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
    # failures are echoed as permanent lines the moment they happen. The line
    # names the repos *still running*, not the last one finished, so if one
    # repo is slow (or stuck) it's the one on screen. The collated report
    # below is untouched by any of this.
    total = len(repos)
    show_progress = sys.stderr.isatty()
    progress_lock = threading.Lock()  # guards `running`, `done`, and stderr
    running = set()
    done = 0

    def render_progress():
        """Redraw the counter line. Caller must hold progress_lock."""
        if not show_progress:
            return
        cols = shutil.get_terminal_size().columns
        line = f"[{done}/{total}]"
        if running:
            line += " running: " + ", ".join(sorted(running, key=str.lower))
        sys.stderr.write("\r" + line[:cols - 1].ljust(cols - 1))
        sys.stderr.flush()

    def clear_progress():
        """Blank the counter line. Caller must hold progress_lock."""
        if show_progress:
            cols = shutil.get_terminal_size().columns
            sys.stderr.write("\r" + " " * (cols - 1) + "\r")
            sys.stderr.flush()

    def task(repo):
        with progress_lock:
            running.add(repo)
            render_progress()
        return run_in_repo(root, repo, git_args, timeout=args.timeout)

    def fail_label(returncode):
        return "timed out" if returncode is None else f"failed (exit {returncode})"

    # A single-worker pool keeps -j 1 strictly sequential (in sorted order)
    # through the same code path as the parallel case.
    results = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(task, r) for r in repos]
        for future in as_completed(futures):
            repo, returncode, output = future.result()
            results[repo] = (returncode, output)
            with progress_lock:
                running.discard(repo)
                done += 1
                if returncode != 0 and show_progress:
                    clear_progress()
                    print(f"{repo}: {fail_label(returncode)}", file=sys.stderr)
                render_progress()
    with progress_lock:
        clear_progress()

    # Report successes first and failures last (each group in sorted repo
    # order, regardless of completion order), so the failures sit right above
    # the summary instead of scrolling away in a long report.
    failed = [repo for repo in repos if results[repo][0] != 0]
    for repo in [repo for repo in repos if results[repo][0] == 0] + failed:
        returncode, output = results[repo]
        ok = returncode == 0
        if args.quiet and ok:
            continue
        status = "ok" if ok else fail_label(returncode)
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
