#!/usr/bin/env python3
"""Cherry-pick a single author's commits from a source branch onto a target.

backport.py finds every commit by a chosen author on <source_branch> (since its
common ancestor with <target_branch>) and replays them, oldest first, onto the
target. It offers an interactive selection step (like 'git rebase -i'), pauses
cleanly on conflicts, and can resume or abort a paused run.

Workflow:
  1. Discover the author's commits between the merge-base and <source_branch>,
     skipping any whose patch is already in the target (so a re-run doesn't
     re-pick work that was backported earlier; see patch_present_in()).
  2. Write them to a state file (.git/.backport) with a metadata header.
  3. Open your editor so you can delete any commits you don't want (unless
     --no-edit). Deleting a line drops that commit; reordering is not supported.
  4. Cherry-pick the remaining commits top to bottom. Each success is recorded
     in the state file, so an interrupted run can resume.
  5. On conflict, stop and tell you how to resolve, then --continue or --abort.

The state file lives in .git/ so it never dirties the working tree.

Examples:
    backport.py feature/new-ui main
    backport.py feature/new-ui main --user jane@example.com
    backport.py feature/new-ui main --create-branch backport/ui --no-edit
    backport.py feature/new-ui main --dry-run
    backport.py feature/new-ui main -s -X theirs
    backport.py --continue
    backport.py --abort

Exit status:
    0   success (commits backported, dry run, or nothing to do)
    1   an error, or a backport paused on conflict (resolve, then --continue)
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import os
import shlex
import shutil
import subprocess
import sys

__version__ = "1.0.0"

STATE_FILENAME = ".backport"
# Completed commit lines are kept (not deleted) but prefixed with this marker so
# they are ignored on resume yet still visible when inspecting the state file.
PICKED_PREFIX = "#picked "
HEADER_KEYS = (
    "STARTING_HEAD", "SOURCE_BRANCH", "TARGET_BRANCH", "TARGET_AUTHOR",
    "SIGNOFF", "MERGE_STRATEGY", "PENDING_INDEX",
)


def git(*args, check=True, capture=True, env=None):
    """Run a git command. Returns CompletedProcess; raises on failure if check."""
    return subprocess.run(
        ["git", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env={**os.environ, **env} if env else None,
    )


def git_out(*args, check=True):
    """Run a git command and return its stripped stdout."""
    return git(*args, check=check).stdout.strip()


def git_lines(*args):
    """Run a git command and return its stdout as a list of non-empty lines."""
    return [line for line in git(*args).stdout.splitlines() if line]


def err(*args):
    """Print to stderr, flushing stdout first so the two streams stay ordered."""
    sys.stdout.flush()
    print(*args, file=sys.stderr)


# --- Git state helpers -------------------------------------------------------

def is_inside_work_tree() -> bool:
    r = git("rev-parse", "--is-inside-work-tree", check=False)
    return r.returncode == 0 and r.stdout.strip() == "true"


def state_file_path() -> str:
    """Absolute-or-relative path to .git/.backport, worktree-aware."""
    return git_out("rev-parse", "--git-path", STATE_FILENAME)


def ref_exists(ref: str) -> bool:
    return git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


def branch_exists(name: str) -> bool:
    return git("rev-parse", "--verify", "--quiet",
               f"refs/heads/{name}", check=False).returncode == 0


def merge_base(a: str, b: str):
    r = git("merge-base", a, b, check=False)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def working_tree_clean() -> bool:
    # The state file sits in .git/, so it never shows up here.
    return git_out("status", "--porcelain") == ""


def has_unmerged() -> bool:
    return bool(git("ls-files", "-u", check=False).stdout.strip())


def cherry_pick_in_progress() -> bool:
    return git("rev-parse", "--verify", "--quiet",
               "CHERRY_PICK_HEAD", check=False).returncode == 0


def staged_changes_present() -> bool:
    # 'git diff --cached --quiet' exits non-zero when the index differs from HEAD.
    return git("diff", "--cached", "--quiet", check=False).returncode != 0


def resolve_author(user_arg):
    if user_arg:
        return user_arg
    email = git("config", "user.email", check=False).stdout.strip()
    if email:
        return email
    name = git("config", "user.name", check=False).stdout.strip()
    return name or None


def patch_present_in(target: str, source: str):
    """Full SHAs of source commits whose patch already exists in target.

    Uses 'git cherry', which compares by patch-id rather than SHA, so commits
    that were previously cherry-picked (and thus have a different SHA on the
    target) are still recognised. This is what stops a re-run from proposing
    commits that have already been backported 1:1. It cannot see commits that
    were squash-merged, since a squashed diff has no matching per-commit
    patch-id; those are caught later by the empty-pick auto-skip.
    """
    result = git("cherry", target, source, check=False)
    present = set()
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "-":  # "- <sha>" = already in target
                present.add(parts[1])
    return present


def discover_commits(base: str, target: str, source: str, author: str):
    """Author's not-yet-backported commits (oldest first) and the count skipped.

    Commits whose patch is already in the target (by patch-id, via git cherry)
    are excluded so a second run doesn't re-pick work that already landed there.
    The search starts at `base`; pass an overriding --base to look past a
    merge-base that a reverted or 'merge -s ours' merge has moved forward (in
    that case git cherry reports nothing present, so the commits resurface and
    any that *are* present get auto-skipped at execution time).
    """
    present = patch_present_in(target, source)
    commits, excluded = [], 0
    # -i makes the --author match case-insensitive, so an address that differs
    # only in capitalisation between tools (Jim@x.com vs jim@x.com) still matches.
    for line in git_lines("log", f"{base}..{source}", "-i", f"--author={author}",
                          "--reverse", "--format=%H %h %aI %s"):
        full, rest = line.split(" ", 1)  # rest = "<short> <date> <subject>"
        if full in present:
            excluded += 1
            continue
        commits.append(rest)
    return commits, excluded


# --- State file (.backport) --------------------------------------------------

def build_state_lines(starting_head, source, target, author, signoff,
                      strategy, commits):
    header = [
        "# BACKPORT STATE - DO NOT EDIT THESE VARIABLES MANUALLY",
        f"# STARTING_HEAD: {starting_head}",
        f"# SOURCE_BRANCH: {source}",
        f"# TARGET_BRANCH: {target}",
        f"# TARGET_AUTHOR: {author}",
        f"# SIGNOFF: {signoff}",
        f"# MERGE_STRATEGY: {strategy}",
        "# PENDING_INDEX: 0",
        "#",
        "# INSTRUCTIONS:",
        "# Delete lines below to prevent those commits from being cherry-picked.",
        "# Reordering lines is not supported. Save and close this file to begin.",
        "",
    ]
    return header + list(commits)


def read_state(path):
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def write_state(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def parse_header(lines):
    """Pull the '# KEY: value' metadata variables out of the state file."""
    values = {}
    for line in lines:
        s = line.strip()
        if not s.startswith("#"):
            continue
        body = s[1:].strip()
        if ":" not in body:
            continue
        key, _, val = body.partition(":")
        key = key.strip()
        if key in HEADER_KEYS:
            values[key] = val.strip()
    return values


def pending_commits(lines):
    """(index, sha) for each not-yet-picked commit, in file order."""
    out = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append((i, s.split()[0]))
    return out


def count_done(lines):
    return sum(1 for line in lines if line.startswith(PICKED_PREFIX))


def mark_done(lines, idx):
    lines[idx] = PICKED_PREFIX + lines[idx]


def set_pending_index(lines, n):
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and "PENDING_INDEX:" in line:
            lines[i] = f"# PENDING_INDEX: {n}"
            return


# --- Editor ------------------------------------------------------------------

def launch_editor(path) -> bool:
    """Open the user's git editor on the state file. False if none works."""
    configured = git("var", "GIT_EDITOR", check=False).stdout.strip()
    candidates = [configured, os.environ.get("VISUAL", ""),
                  os.environ.get("EDITOR", ""), "nano", "vi"]
    for cand in candidates:
        if not cand:
            continue
        try:
            argv0 = shlex.split(cand, posix=(os.name != "nt"))[0]
        except ValueError:
            argv0 = cand
        if shutil.which(argv0) is None and not os.path.exists(argv0):
            continue
        try:
            subprocess.run(f'{cand} "{path}"', shell=True)
            return True
        except OSError:
            continue
    return False


# --- Cherry-pick loop --------------------------------------------------------

def print_conflict(sha, result):
    print()
    print(f"Conflict while cherry-picking {sha}.")
    detail = (result.stderr or "").strip()
    if detail:
        print(detail)
    print()
    print("Resolve the conflicts and stage the result:")
    print("    git add <paths>")
    print("    # optionally commit it yourself: git commit")
    print()
    print("Then resume:")
    print("    python backport.py --continue")
    print()
    print("Or cancel and rewind everything:")
    print("    python backport.py --abort")


def run_execution_loop(path, lines, header):
    signoff = header.get("SIGNOFF", "False") == "True"
    strategy = header.get("MERGE_STRATEGY", "") or ""
    target = header.get("TARGET_BRANCH", "target")

    while True:
        pend = pending_commits(lines)
        if not pend:
            os.remove(path)
            print(f"\nBackport complete: {count_done(lines)} commit(s) "
                  f"cherry-picked onto {target}.")
            return 0

        idx, sha = pend[0]
        print(f"Cherry-picking {sha} ...")
        cmd = ["cherry-pick", "-x"]  # -x: record "(cherry picked from commit ...)"
        if signoff:
            cmd.append("-s")
        if strategy:
            cmd += ["-X", strategy]
        cmd.append(sha)
        result = git(*cmd, check=False)

        if result.returncode == 0:
            mark_done(lines, idx)
            set_pending_index(lines, count_done(lines))
            write_state(path, lines)
            continue

        if has_unmerged():
            set_pending_index(lines, count_done(lines))
            write_state(path, lines)
            print_conflict(sha, result)
            return 1

        if cherry_pick_in_progress():
            # No conflict but the pick is empty: its changes are already in the
            # target. Auto-skip so we don't create an empty commit.
            print(f"  {sha} is already present in {target}; skipping.")
            git("cherry-pick", "--skip", check=False)
            mark_done(lines, idx)
            set_pending_index(lines, count_done(lines))
            write_state(path, lines)
            continue

        err(f"Error: cherry-pick of {sha} failed.")
        detail = (result.stderr or "").strip()
        if detail:
            err(detail)
        err("Fix the problem manually, or run 'python backport.py --abort' "
            "to rewind.")
        return 1


# --- Subcommands -------------------------------------------------------------

def do_continue(path) -> int:
    lines = read_state(path)
    header = parse_header(lines)

    if has_unmerged():
        err("Error: unmerged paths remain. Resolve the conflicts and "
            "'git add' them, then re-run 'python backport.py --continue'.")
        return 1

    pend = pending_commits(lines)
    if not pend:
        os.remove(path)
        print("Nothing left to continue; backport already complete.")
        return 0

    idx, sha = pend[0]

    if cherry_pick_in_progress():
        if staged_changes_present():
            # Finish via cherry-pick --continue (not 'git commit') so the saved
            # -x/-s/-X options are reapplied. Force a no-op editor to stay
            # non-interactive while keeping the original commit message.
            r = git("cherry-pick", "--continue", check=False, capture=False,
                    env={"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"})
            if r.returncode != 0:
                err("Error: failed to complete the cherry-pick.")
                return 1
        else:
            # Resolution left no changes: the commit is redundant. Skip it.
            git("cherry-pick", "--skip", check=False)
    # else: the user already committed the resolution manually; nothing to do.

    mark_done(lines, idx)
    set_pending_index(lines, count_done(lines))
    write_state(path, lines)
    print(f"Resolved {sha}; resuming.")
    return run_execution_loop(path, lines, header)


def do_abort(path) -> int:
    lines = read_state(path)
    header = parse_header(lines)
    starting = header.get("STARTING_HEAD")
    if not starting:
        err("Error: state file has no STARTING_HEAD; refusing to reset. "
            f"Remove {path} and recover manually.")
        return 1

    if cherry_pick_in_progress():
        git("cherry-pick", "--abort", check=False)
    reset = git("reset", "--hard", starting, check=False)
    if reset.returncode != 0:
        err(f"Error: 'git reset --hard {starting}' failed.")
        detail = (reset.stderr or "").strip()
        if detail:
            err(detail)
        return 1

    os.remove(path)
    print(f"Backport aborted; repository rewound to {starting[:9]}.")
    return 0


def fully_merged_note(args):
    """Hint shown when source looks fully merged but content may be absent."""
    if args.base:
        return None
    if git("merge-base", "--is-ancestor", args.source, args.target,
           check=False).returncode != 0:
        return None
    return (f"Note: '{args.source}' is already fully merged into "
            f"'{args.target}'. If that merge was reverted or made with "
            f"'git merge -s ours', the changes may not actually be present; "
            f"pass --base <fork-point> to re-scan by patch content.")


def print_dry_run(args, base, author, commits, excluded):
    print("DRY RUN - no changes will be made.\n")
    print(f"Source branch  : {args.source}")
    print(f"Target branch  : {args.target}")
    print(f"Author filter  : {author}")
    print(f"{'Base (--base)' if args.base else 'Merge base   '}  : {base[:9]}")
    if args.create_branch:
        print(f"Branch setup   : would run 'git checkout -b "
              f"{args.create_branch} {args.target}'")
    else:
        print(f"Branch setup   : would cherry-pick onto current HEAD "
              f"(must contain {args.target})")
    if args.signoff:
        print("Signoff        : yes (-s)")
    if args.strategy:
        print(f"Merge strategy : -X {args.strategy}")
    if excluded:
        print(f"Already present : {excluded} commit(s) excluded "
              f"(patch already in {args.target})")
    print()
    if not commits:
        print(f"No new commits by '{author}' to backport from {args.source}.")
        note = fully_merged_note(args)
        if note:
            print(note)
        return
    print(f"{len(commits)} commit(s) would be backported (oldest first):")
    for c in commits:
        print(f"  {c}")


def start_session(args) -> int:
    state_path = state_file_path()
    if not args.dry_run and os.path.exists(state_path):
        err("Error: a backport is already in progress (.backport exists). "
            "Use --continue or --abort.")
        return 1

    for ref in (args.source, args.target):
        if not ref_exists(ref):
            err(f"Error: '{ref}' is not a valid branch or revision.")
            return 1

    if args.base:
        if not ref_exists(args.base):
            err(f"Error: --base '{args.base}' is not a valid revision.")
            return 1
        if git("merge-base", "--is-ancestor", args.base, args.source,
               check=False).returncode != 0:
            err(f"Error: --base '{args.base}' is not an ancestor of "
                f"'{args.source}'.")
            return 1
        base = git_out("rev-parse", args.base)
    else:
        base = merge_base(args.source, args.target)
        if base is None:
            err(f"Error: no common ancestor between '{args.source}' and "
                f"'{args.target}'.")
            return 1

    author = resolve_author(args.user)
    if not author:
        err("Error: could not determine an author. Pass --user, or set "
            "'git config user.email'.")
        return 1

    commits, excluded = discover_commits(base, args.target, args.source, author)

    if args.dry_run:
        print_dry_run(args, base, author, commits, excluded)
        return 0

    if not working_tree_clean():
        err("Error: working tree is not clean. Commit or stash your changes "
            "first.")
        return 1

    if excluded:
        print(f"Excluded {excluded} commit(s) already present in "
              f"'{args.target}'.")

    if not commits:
        print(f"No new commits by '{author}' to backport from '{args.source}'.")
        note = fully_merged_note(args)
        if note:
            print(note)
        return 0

    # Branch setup.
    if args.create_branch:
        if branch_exists(args.create_branch):
            err(f"Error: branch '{args.create_branch}' already exists.")
            return 1
        r = git("checkout", "-b", args.create_branch, args.target,
                check=False, capture=False)
        if r.returncode != 0:
            err(f"Error: failed to create branch '{args.create_branch}'.")
            return 1
    else:
        # The current branch must already contain the target tip, otherwise the
        # cherry-picks would land on an unrelated base.
        if git("merge-base", "--is-ancestor", args.target, "HEAD",
               check=False).returncode != 0:
            err(f"Error: the current branch does not contain '{args.target}'. "
                f"Check out '{args.target}' (or a descendant of it), or pass "
                "--create-branch.")
            return 1

    starting_head = git_out("rev-parse", "HEAD")
    lines = build_state_lines(starting_head, args.source, args.target, author,
                              bool(args.signoff), args.strategy or "", commits)
    write_state(state_path, lines)

    if not args.no_edit:
        if not launch_editor(state_path):
            os.remove(state_path)
            err("Error: could not open an editor. Set one with "
                "'git config core.editor <command>', or re-run with --no-edit.")
            return 1
        lines = read_state(state_path)

    if not pending_commits(lines):
        os.remove(state_path)
        print("No commits selected. Nothing to do.")
        return 0

    header = parse_header(lines)
    return run_execution_loop(state_path, lines, header)


# --- CLI ---------------------------------------------------------------------

def validate_args(parser, args):
    if args.cont and args.abort:
        parser.error("--continue and --abort cannot be used together")
    if args.cont or args.abort:
        extras = []
        if args.source or args.target:
            extras.append("branch arguments")
        if args.user:
            extras.append("--user")
        if args.create_branch:
            extras.append("--create-branch")
        if args.dry_run:
            extras.append("--dry-run")
        if args.no_edit:
            extras.append("--no-edit")
        if args.signoff:
            extras.append("--signoff")
        if args.strategy:
            extras.append("-X")
        if args.base:
            extras.append("--base")
        if extras:
            mode = "--continue" if args.cont else "--abort"
            parser.error(f"{mode} cannot be combined with: {', '.join(extras)}")
    elif not args.source or not args.target:
        parser.error("source and target branches are required "
                     "(or use --continue / --abort)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="backport.py",
        description="Cherry-pick one author's commits from a source branch "
                    "onto a target branch, with interactive selection and "
                    "conflict pausing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "workflow:\n"
            "  Commits by the author (default: your git user) between the\n"
            "  merge-base and <source> are written to .git/.backport. Your\n"
            "  editor opens so you can delete unwanted lines (skip with\n"
            "  --no-edit). Remaining commits are cherry-picked oldest first.\n"
            "  On conflict the run pauses; resolve, then --continue or --abort.\n"
            "\n"
            "examples:\n"
            "  backport.py feature/new-ui main\n"
            "  backport.py feature/new-ui main --user jane@example.com\n"
            "  backport.py feature/new-ui main --create-branch backport/ui\n"
            "  backport.py feature/new-ui main -s -X theirs --no-edit\n"
            "  backport.py feature/new-ui main --base <fork-point>  "
            "# after a reverted/-s ours merge\n"
            "  backport.py --continue\n"
            "  backport.py --abort\n"
        ),
    )
    parser.add_argument("source", nargs="?", metavar="SOURCE_BRANCH",
                        help="branch to take commits from")
    parser.add_argument("target", nargs="?", metavar="TARGET_BRANCH",
                        help="branch (in current HEAD's history) to apply onto")
    parser.add_argument("--user", metavar="NAME_OR_EMAIL",
                        help="filter commits by this author, case-insensitively "
                             "(default: your git user.email or user.name)")
    parser.add_argument("--create-branch", metavar="NAME",
                        help="create and check out NAME from TARGET_BRANCH "
                             "before backporting")
    parser.add_argument("--base", metavar="REF",
                        help="override the auto-detected merge-base used to "
                             "find commits; pass the true fork point if a merge "
                             "was reverted or made with 'git merge -s ours' "
                             "(already-present changes are still skipped by "
                             "patch content)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the commits that would be backported, then exit")
    parser.add_argument("--no-edit", action="store_true",
                        help="skip the interactive editor and pick all commits")
    parser.add_argument("-s", "--signoff", action="store_true",
                        help="pass -s to git cherry-pick (add Signed-off-by)")
    parser.add_argument("-X", dest="strategy", metavar="STRATEGY",
                        help="pass -X STRATEGY to git cherry-pick "
                             "(e.g. theirs, ours)")
    parser.add_argument("--continue", dest="cont", action="store_true",
                        help="resume a paused backport after resolving a conflict")
    parser.add_argument("--abort", action="store_true",
                        help="cancel a backport and rewind to the starting commit")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    validate_args(parser, args)

    if not is_inside_work_tree():
        err("Error: not inside a Git repository.")
        return 1

    if args.cont or args.abort:
        path = state_file_path()
        if not os.path.exists(path):
            err("Error: no backport in progress (no .backport state file found).")
            return 1
        return do_abort(path) if args.abort else do_continue(path)

    return start_session(args)


if __name__ == "__main__":
    raise SystemExit(main())
