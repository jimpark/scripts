#!/usr/bin/env python3
"""Remove Git worktrees whose branches are already merged.

Scans a folder (the current directory by default) for Git repositories: the
folder itself, if it is one, and each immediate subfolder that is one. Repos
reached twice, for example through a worktree checkout or a symlink, are
scanned once. For every repo it fetches the remote, lists the linked worktrees
(the main worktree is never touched), and decides whether each worktree's
branch is merged. A branch counts as merged if either check says so:

  * Git: the branch is in the remote's default branch (origin/HEAD), either
    as an ancestor or as an equivalent patch, so squash- and rebase-merged
    PRs count. This is the same check git-prune.py uses.
  * Server: Azure DevOps (az), GitLab (glab), or GitHub (gh) reports a
    completed PR for the branch, and that PR's last source commit contains
    the local branch tip. A branch with commits pushed or made after the PR
    merged is not counted.

A merged worktree is kept, and listed with the reason, if it has uncommitted
or untracked changes (pass --force to remove it anyway), is locked, holds the
current directory, or has a branch that never had commits of its own. A
missing server CLI, or a failed lookup, falls back to the Git check alone.

Without --dry-run the script lists what it will remove and the disk space it
will free, asks for confirmation, then runs 'git worktree remove' and
'git branch -D' for each one. With --dry-run it lists the same plan and stops.

Examples:
    git-worktree-prune.py --dry-run
    git-worktree-prune.py ~/MEPS/master/foundation-workspace
    git-worktree-prune.py --no-server
    git-worktree-prune.py --force --yes

Exit status:
    0   success (worktrees removed, nothing to remove, or a dry run)
    1   no repos found, or one or more fetches or removals failed
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

__version__ = "1.0.0"

REMOTE = "origin"
FALLBACK_TARGETS = ("main", "master", "develop")


# ── git helpers ───────────────────────────────────────────────────────────────

def git(repo, *args):
    """Run git in `repo` and return the CompletedProcess (never raises)."""
    return subprocess.run(["git", "-C", repo, *args], text=True,
                          encoding="utf-8", errors="replace",
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def git_out(repo, *args):
    """Return git's stripped stdout, or None if the command failed."""
    result = git(repo, *args)
    return result.stdout.strip() if result.returncode == 0 else None


def is_merged_into(repo, branch, target):
    """Is `branch`'s work already in `target`? Copied from git-prune.py.

    Ancestor covers fast-forward and merge commits. For squash and rebase
    merges, a commit is built from the branch's tree on top of its merge-base,
    and 'git cherry' reports whether an equivalent patch is in target.
    """
    if git(repo, "merge-base", "--is-ancestor", branch, target).returncode == 0:
        return True
    base = git_out(repo, "merge-base", target, branch)
    tree = git_out(repo, "rev-parse", f"{branch}^{{tree}}")
    if not base or not tree:
        return False
    fake = git_out(repo, "commit-tree", tree, "-p", base, "-m", "_")
    if not fake:
        return False
    cherry = git(repo, "cherry", target, fake)
    return cherry.returncode == 0 and cherry.stdout.lstrip().startswith("-")


def never_committed(repo, branch, target):
    """True if the branch looks like it never had commits of its own.

    The ancestor check calls a fresh branch "merged" only because it never
    diverged from the target. Two signs of that:

      * the reflog's oldest entry (the creation point) is still the tip;
      * with no reflog, the tip is an ancestor of the target and the branch
        has no upstream. A branch merged through a PR was pushed, so it has
        one; the PR check covers a branch pushed without -u.
    """
    tip = git_out(repo, "rev-parse", f"refs/heads/{branch}")
    if not tip:
        return False
    shas = git_out(repo, "log", "-g", "--format=%H", f"refs/heads/{branch}")
    if shas:
        return shas.splitlines()[-1] == tip
    if git_out(repo, "config", f"branch.{branch}.merge"):
        return False
    return bool(target) and git(repo, "merge-base", "--is-ancestor", tip,
                                target).returncode == 0


def contains(repo, newer, older):
    """True if commit `older` is `newer` or one of its ancestors."""
    if newer == older:
        return True
    return git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0


def default_target(repo):
    """Return the remote default branch ref (e.g. 'origin/master'), or None."""
    head = git_out(repo, "symbolic-ref", "--short", f"refs/remotes/{REMOTE}/HEAD")
    if head:
        return head
    for name in FALLBACK_TARGETS:
        ref = f"{REMOTE}/{name}"
        if git(repo, "rev-parse", "--verify", "--quiet",
               f"refs/remotes/{ref}").returncode == 0:
            return ref
    return None


# ── Server lookups ────────────────────────────────────────────────────────────

class ServerError(Exception):
    """A PR lookup failed; the Git check still applies."""


def run_json(cmd):
    # Resolve the full path: on Windows az is az.cmd, which CreateProcess
    # will not find from a bare "az" but will run when given its path.
    exe = shutil.which(cmd[0])
    if not exe:
        raise ServerError(f"'{cmd[0]}' not found")
    try:
        proc = subprocess.run([exe, *cmd[1:]], text=True, encoding="utf-8",
                              errors="replace", stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    except OSError as exc:
        raise ServerError(f"cannot run '{cmd[0]}': {exc}")
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or \
            f"{cmd[0]} exited with status {proc.returncode}"
        raise ServerError(message.splitlines()[0])
    return json.loads(proc.stdout or "[]")


def parse_remote(url):
    """Return (kind, info) for a remote URL, or (None, None) if unrecognised.

    kind is 'ado', 'gitlab', or 'github'. Hosts other than Azure DevOps and
    github.com are assumed to be GitLab.
    """
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        host, path = parts.hostname or "", parts.path
    else:
        # scp-like syntax: git@host:path
        login, sep, path = url.partition(":")
        if not sep:
            return None, None
        host = login.rpartition("@")[2]
    path = urllib.parse.unquote(path.strip("/").removesuffix(".git"))
    segments = path.split("/")

    if host == "ssh.dev.azure.com" and len(segments) == 4 and segments[0] == "v3":
        _, org, project, repo = segments
        return "ado", (f"https://dev.azure.com/{org}", project, repo)
    if host == "dev.azure.com" and len(segments) == 4 and segments[2] == "_git":
        org, project, _, repo = segments
        return "ado", (f"https://dev.azure.com/{org}", project, repo)
    if host.endswith(".visualstudio.com") and len(segments) == 3 \
            and segments[1] == "_git":
        project, _, repo = segments
        org = host.split(".")[0]
        return "ado", (f"https://dev.azure.com/{org}", project, repo)
    if host == "github.com" and len(segments) == 2:
        return "github", path
    if host and len(segments) >= 2:
        return "gitlab", (host, path)
    return None, None


def merged_prs(kind, info, branch):
    """Return [(label, source sha)] for merged PRs whose source is `branch`."""
    if kind == "ado":
        org, project, repo = info
        data = run_json(["az", "repos", "pr", "list", "--organization", org,
                         "--project", project, "--repository", repo,
                         "--source-branch", branch, "--status", "completed",
                         "-o", "json"])
        return [(f"PR {pr['pullRequestId']}",
                 (pr.get("lastMergeSourceCommit") or {}).get("commitId"))
                for pr in data]
    if kind == "gitlab":
        host, path = info
        query = urllib.parse.urlencode({"source_branch": branch,
                                        "state": "merged"})
        data = run_json(["glab", "api", "--hostname", host,
                         f"projects/{urllib.parse.quote(path, safe='')}"
                         f"/merge_requests?{query}"])
        return [(f"MR !{mr['iid']}", mr.get("sha")) for mr in data]
    if kind == "github":
        data = run_json(["gh", "pr", "list", "--repo", info, "--head", branch,
                         "--state", "merged", "--json", "number,headRefOid"])
        return [(f"PR #{pr['number']}", pr.get("headRefOid")) for pr in data]
    return []


CLI_FOR = {"ado": "az", "gitlab": "glab", "github": "gh"}


# ── Model ─────────────────────────────────────────────────────────────────────

@dataclass
class Worktree:
    path: str
    branch: str = None      # short name; None when detached
    head: str = None
    locked: bool = False
    prunable: bool = False
    merged_by: list = field(default_factory=list)  # reasons it counts as merged
    keep_reason: str = None  # set when it is merged but kept
    size: int = 0


@dataclass
class Repo:
    path: str
    name: str
    worktrees: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    fetch_failed: bool = False


def find_repos(root):
    """Return Repo objects for `root` and its immediate subfolders.

    Deduped by Git's common directory, so a worktree checkout or a symlink
    resolves to the repo that owns it. The repo's main worktree path is used.
    """
    candidates = [root]
    try:
        candidates += sorted((e.path for e in os.scandir(root)
                              if e.is_dir(follow_symlinks=True)),
                             key=lambda p: os.path.basename(p).lower())
    except OSError as exc:
        raise SystemExit(f"Error: cannot list '{root}': {exc}")

    repos, seen = [], set()
    for path in candidates:
        if not os.path.exists(os.path.join(path, ".git")):
            continue
        common = git_out(path, "rev-parse", "--path-format=absolute",
                         "--git-common-dir")
        if not common:
            continue
        common = os.path.realpath(common)
        if common in seen:
            continue
        seen.add(common)
        # The main worktree is the first entry of 'git worktree list'.
        listing = git_out(path, "worktree", "list", "--porcelain") or ""
        first = listing.splitlines()[0] if listing else ""
        main = first.removeprefix("worktree ") if first else path
        rel = os.path.relpath(main, root)
        repos.append(Repo(path=main, name=os.path.basename(root) if rel == "."
                          else rel))
    return repos


def list_worktrees(repo):
    """Return the linked worktrees of a repo (the main worktree excluded)."""
    out = git_out(repo.path, "worktree", "list", "--porcelain") or ""
    blocks = [b for b in out.split("\n\n") if b.strip()]
    worktrees = []
    for block in blocks[1:]:
        wt = None
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                wt = Worktree(path=value)
            elif key == "HEAD":
                wt.head = value
            elif key == "branch":
                wt.branch = value.removeprefix("refs/heads/")
            elif key == "locked":
                wt.locked = True
            elif key == "prunable":
                wt.prunable = True
        if wt:
            worktrees.append(wt)
    return worktrees


def dir_size(path):
    """Total disk usage of a directory tree in bytes, counting hardlinks once."""
    total, seen = 0, set()
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if st.st_nlink > 1:
                if (st.st_dev, st.st_ino) in seen:
                    continue
                seen.add((st.st_dev, st.st_ino))
            blocks = getattr(st, "st_blocks", None)
            total += blocks * 512 if blocks is not None else st.st_size
    return total


def is_within(path, parent):
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent + os.sep)


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyse(repo, args, available_clis):
    """Fill in repo.worktrees with merge results and keep reasons."""
    repo.worktrees = list_worktrees(repo)
    if not repo.worktrees:
        return

    if not args.no_fetch:
        fetch = git(repo.path, "fetch", "--prune", "--quiet", REMOTE)
        if fetch.returncode != 0:
            repo.fetch_failed = True
            detail = fetch.stderr.strip().splitlines()
            repo.warnings.append(f"fetch from '{REMOTE}' failed"
                                 + (f": {detail[-1]}" if detail else ""))

    target = default_target(repo.path)
    if target is None:
        repo.warnings.append(f"no default branch found on '{REMOTE}'; "
                             "Git merge check skipped")

    kind = info = None
    if not args.no_server:
        url = git_out(repo.path, "remote", "get-url", REMOTE)
        if url:
            kind, info = parse_remote(url)
        if kind and CLI_FOR[kind] not in available_clis:
            repo.warnings.append(f"'{CLI_FOR[kind]}' not found; "
                                 "PR check skipped")
            kind = None

    # PR lookups take about a second each, so run them in parallel up front.
    prs = {}
    if kind:
        branches = sorted({wt.branch for wt in repo.worktrees
                           if wt.branch and not wt.prunable})

        def lookup(branch):
            try:
                return branch, merged_prs(kind, info, branch)
            except (ServerError, json.JSONDecodeError, KeyError) as exc:
                return branch, exc

        with ThreadPoolExecutor(max_workers=4) as pool:
            for branch, result in pool.map(lookup, branches):
                if isinstance(result, Exception):
                    repo.warnings.append(f"PR lookup for '{branch}' failed "
                                         f"({result}); using the Git check")
                else:
                    prs[branch] = result

    cwd = os.getcwd()
    for wt in repo.worktrees:
        if wt.prunable:
            wt.keep_reason = "directory missing; run 'git worktree prune'"
            continue
        if not wt.branch:
            wt.keep_reason = "detached HEAD"
            continue

        if target and is_merged_into(repo.path, wt.branch, target):
            wt.merged_by.append(f"in {target}")
        for label, sha in prs.get(wt.branch, []):
            if sha and contains(repo.path, sha, wt.head):
                wt.merged_by.append(f"{label} merged")
                break
            if sha:
                wt.keep_reason = (f"{label} merged, but the branch "
                                  "has later commits")

        if not wt.merged_by:
            continue
        # A later-commits note only matters when nothing else proved a merge.
        wt.keep_reason = None
        has_pr = any(reason.endswith(" merged") for reason in wt.merged_by)
        if not has_pr and never_committed(repo.path, wt.branch, target):
            wt.keep_reason = "no commits of its own"
        elif wt.locked:
            wt.keep_reason = "locked"
        elif is_within(cwd, wt.path):
            wt.keep_reason = "holds the current directory"
        elif not args.force and git_out(wt.path, "status", "--porcelain"):
            wt.keep_reason = "uncommitted or untracked changes"


def removable(wt):
    return wt.merged_by and not wt.keep_reason


# ── Output ────────────────────────────────────────────────────────────────────

def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def print_plan(repos):
    for repo in repos:
        merged = [wt for wt in repo.worktrees if wt.merged_by or wt.keep_reason]
        if not merged and not repo.warnings:
            continue
        print(f"\n{repo.name}")
        for warning in repo.warnings:
            print(f"  warning: {warning}")
        width = max((len(os.path.relpath(wt.path, repo.path))
                     for wt in merged), default=0)
        for wt in merged:
            rel = os.path.relpath(wt.path, repo.path).ljust(width)
            if removable(wt):
                print(f"  remove  {rel}  {human_size(wt.size):>9}  "
                      f"{', '.join(wt.merged_by)}")
            else:
                merged_note = f" (merged: {', '.join(wt.merged_by)})" \
                    if wt.merged_by and wt.keep_reason != "no commits of its own" else ""
                print(f"  keep    {rel}  {'':>9}  {wt.keep_reason}{merged_note}")


def confirm(prompt):
    try:
        return input(prompt).strip().lower() == "y"
    except EOFError:
        return False


def err(*args):
    sys.stdout.flush()
    print(*args, file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="git-worktree-prune.py",
        description="Remove Git worktrees whose branches are already merged.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "notes:\n"
            "  A branch is merged if it is in origin's default branch\n"
            "  (squash and rebase merges included), or if az, glab, or gh\n"
            "  reports a completed PR that contains the branch tip. Each\n"
            "  removed worktree's local branch is deleted too.\n"
        ),
    )
    parser.add_argument("folder", nargs="?", default=".",
                        help="folder to scan (default: current directory)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="list what would be removed, then stop")
    parser.add_argument("--force", action="store_true",
                        help="also remove merged worktrees that have "
                             "uncommitted or untracked changes")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    parser.add_argument("--no-fetch", action="store_true",
                        help="do not fetch before checking merges")
    parser.add_argument("--no-server", action="store_true",
                        help="skip the az/glab/gh PR lookups; Git check only")
    parser.add_argument("-j", "--jobs", type=int, default=8, metavar="N",
                        help="repos to check in parallel (default: 8)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(args.folder))
    if not os.path.isdir(root):
        err(f"Error: '{args.folder}' is not a directory.")
        return 1

    repos = find_repos(root)
    if not repos:
        err(f"Error: no Git repositories in '{root}'.")
        return 1

    available = {cli for cli in CLI_FOR.values() if shutil.which(cli)}
    print(f"Checking worktrees in {len(repos)} repo(s) under {root}...")
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        list(pool.map(lambda r: analyse(r, args, available), repos))

    to_remove = [(repo, wt) for repo in repos for wt in repo.worktrees
                 if removable(wt)]
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        sizes = pool.map(lambda pair: dir_size(pair[1].path), to_remove)
        for (_, wt), size in zip(to_remove, sizes):
            wt.size = size

    print_plan(repos)
    fetch_failed = any(repo.fetch_failed for repo in repos)

    if not to_remove:
        print("\nNo merged worktrees to remove.")
        return 1 if fetch_failed else 0

    total = sum(wt.size for _, wt in to_remove)
    print(f"\n{len(to_remove)} worktree(s) to remove, "
          f"{human_size(total)} to reclaim.")

    if args.dry_run:
        print("Dry run: nothing removed.")
        return 1 if fetch_failed else 0

    if not args.yes and not confirm(
            "Remove these worktrees and delete their branches? (y/n): "):
        print("Operation cancelled.")
        return 0

    failed, reclaimed = 0, 0
    for repo, wt in to_remove:
        print(f"Removing {wt.path}")
        # --force: the changes check already ran (or --force skipped it), and
        # without it git refuses any worktree that contains submodules.
        result = git(repo.path, "worktree", "remove", "--force", wt.path)
        if result.returncode != 0:
            failed += 1
            err(f"  Failed to remove worktree: {result.stderr.strip()}")
            continue
        reclaimed += wt.size
        result = git(repo.path, "branch", "-D", wt.branch)
        if result.returncode != 0:
            failed += 1
            err(f"  Failed to delete branch '{wt.branch}': "
                f"{result.stderr.strip()}")

    print(f"\nReclaimed {human_size(reclaimed)}.")
    if failed:
        print(f"Finished with {failed} failure(s).")
        return 1
    return 1 if fetch_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
