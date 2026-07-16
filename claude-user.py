#!/usr/bin/env python3
"""Launch `claude` against a named configuration directory, one per account.

Each "account" is a directory named `~/.claude-<name>`; the name after the
dash is what you pass on the command line. The script points
CLAUDE_CONFIG_DIR at that directory and hands off to `claude`, so a personal
login and an enterprise login can coexist without either one clobbering the
other:

    claude-user.py personal          ->  CLAUDE_CONFIG_DIR=~/.claude-personal claude

With no name, the accounts found under the home directory are listed and one
can be picked interactively. Everything after the name is forwarded to
`claude` untouched:

    claude-user.py personal --resume
    claude-user.py work -p "explain this repo"

The plain default directory (~/.claude) is deliberately not listed: it is
what bare `claude` already uses, so it needs no wrapper.

Why the config directory is enough, on every platform:

CLAUDE_CONFIG_DIR moves the whole config tree — settings, projects, history.
Credentials follow it too, but by different routes. On Linux and Windows they
live in a `.credentials.json` inside the directory, so moving the directory
moves them. On macOS they live in the login Keychain, which the directory
plainly cannot contain; the isolation there works because claude derives the
Keychain item's service name from a hash of the config directory path, so
each directory reads and writes its own item. This was verified against
claude 2.1.211 by running it with an empty config directory on a logged-in
macOS machine: it reported "Not logged in", which only happens if the
Keychain lookup missed. Note that the official documentation currently
claims the opposite — that macOS credentials are shared across config
directories — so trust the observed behaviour, not the docs, and re-check if
a future version regresses.

That hash makes the *spelling* of the path load-bearing on macOS: two
spellings of the same directory hash differently and would each get their own
Keychain item, presenting as a surprise logout. Hence the path handed to
claude is always canonicalised the same way (see profile_dir), and the same
name always produces the same string.

Examples:
    claude-user.py                   pick an account from a list
    claude-user.py personal          launch claude as the personal account
    claude-user.py personal --resume forward extra arguments to claude
    claude-user.py --list            list accounts and exit
    claude-user.py --create work     make ~/.claude-work without being asked

Naming an account that does not exist offers to create it, so a new account
is just a matter of launching it; claude then prompts for /login. The offer
needs a terminal to answer it — non-interactive runs get an error instead, on
the grounds that a script naming a missing account has a typo, not a new
account. --create says yes in advance, for setup scripts.

Exit status:
    0   claude ran (its own exit status is passed through), or --list succeeded
    1   no accounts found, unknown account, claude not on PATH, or the pick
        was cancelled
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import os
import shutil
import subprocess
import sys

__version__ = "1.0.0"

PREFIX = ".claude-"


def profile_dir(name: str) -> str:
    """Return the absolute config directory for the account `name`.

    Canonical and deterministic: the same name must always yield the exact
    same string, because on macOS the Keychain item holding the account's
    credentials is keyed by a hash of this path (see the module docstring).
    """
    return os.path.abspath(os.path.join(os.path.expanduser("~"), PREFIX + name))


def find_profiles():
    """Return the sorted account names found as ~/.claude-<name> directories.

    Only directories count, so a stray file such as ~/.claude-notes.txt is
    ignored. Symlinks to directories are accepted — that is a reasonable way
    to park an account on another volume — but the link's own path is what
    gets used, keeping the name-to-path mapping stable.
    """
    home = os.path.expanduser("~")
    try:
        entries = os.scandir(home)
    except OSError as exc:
        raise SystemExit(f"Error: cannot list '{home}': {exc}")

    with entries:
        names = [
            entry.name[len(PREFIX):]
            for entry in entries
            if entry.name.startswith(PREFIX)
            and entry.name != PREFIX
            and entry.is_dir(follow_symlinks=True)
        ]
    return sorted(names, key=str.lower)


def choose_profile(names):
    """Prompt for one of `names`; return the pick, or None if cancelled.

    Accepts either the number shown or the account name itself. A plain
    numbered list rather than a curses UI, so it behaves the same everywhere
    and degrades gracefully in odd terminals.
    """
    if not sys.stdin.isatty():
        raise SystemExit(
            "Error: no account given and stdin is not a terminal.\n"
            "Pass an account name, or run --list to see what is available."
        )

    print("Accounts:")
    width = len(str(len(names)))
    for index, name in enumerate(names, 1):
        print(f"  {index:>{width}}. {name}  ({profile_dir(name)})")

    while True:
        try:
            reply = input(f"Choose [1-{len(names)}, or a name] (Enter to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not reply:
            return None
        if reply.isdigit() and 1 <= int(reply) <= len(names):
            return names[int(reply) - 1]
        if reply in names:
            return reply
        print(f"Not an account: {reply}")


def confirm_create(name, directory):
    """Ask whether to create a missing account; return True to go ahead.

    Only reachable from a terminal — a script that names an account that does
    not exist has made a mistake, and inventing a fresh empty account (with
    the /login prompt that follows) is the wrong way to answer that.
    """
    if not sys.stdin.isatty():
        return False
    prompt = f"No account '{name}' yet. Create {directory}? [y/N]: "
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def launch(name, claude_args):
    """Run claude with CLAUDE_CONFIG_DIR set to `name`'s directory.

    On POSIX this replaces the current process, so claude owns the terminal
    outright and signals, job control and the exit status need no forwarding.
    Windows has no exec that preserves the console this way, so there the
    child is run to completion and its status relayed.
    """
    exe = shutil.which("claude")
    if exe is None:
        print(
            "Error: 'claude' is not on PATH. Install Claude Code, or add it to PATH.",
            file=sys.stderr,
        )
        return 1

    env = dict(os.environ, CLAUDE_CONFIG_DIR=profile_dir(name))
    argv = [exe, *claude_args]
    if os.name == "nt":
        return subprocess.run(argv, env=env).returncode
    # execve does not flush our buffers, and anything still sitting in them is
    # lost with the process image — which is invisible on a terminal (line
    # buffered) but eats earlier output when redirected to a pipe or file.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(exe, argv, env)


def main():
    parser = argparse.ArgumentParser(
        description="Launch claude against a per-account configuration directory.",
        epilog="Options before the account name are this script's; the name and "
               "everything after it go to claude verbatim, so 'claude-user work "
               "--list' lists claude's sessions rather than accounts. Forwarding "
               "therefore needs a name: use '--' to pass one that looks like an "
               "option.",
    )
    parser.add_argument(
        "name",
        nargs="?",
        help=f"account to use, i.e. the <name> in ~/{PREFIX}<name>; "
             "omit to pick from a list",
    )
    parser.add_argument(
        "claude_args",
        nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="list the accounts and exit",
    )
    parser.add_argument(
        "-c", "--create",
        action="store_true",
        help="create the account's directory if it does not exist yet",
    )
    parser.add_argument("-V", "--version", action="version", version=__version__)
    args = parser.parse_args()

    names = find_profiles()

    if args.list:
        if not names:
            print(f"No accounts yet. Create one with: {parser.prog} --create <name>")
            return 1
        for name in names:
            print(f"{name}\t{profile_dir(name)}")
        return 0

    name = args.name
    if name is None:
        if not names:
            print(
                f"No accounts found matching ~/{PREFIX}*.\n"
                f"Create one with: {parser.prog} --create <name>",
                file=sys.stderr,
            )
            return 1
        name = choose_profile(names)
        if name is None:
            print("Cancelled.", file=sys.stderr)
            return 1

    if name not in names:
        directory = profile_dir(name)
        if not args.create and not confirm_create(name, directory):
            known = ", ".join(names) if names else "none yet"
            print(
                f"Error: no account '{name}' ({directory} does not exist).\n"
                f"Known accounts: {known}\n"
                f"Create it with: {parser.prog} --create {name}",
                file=sys.stderr,
            )
            return 1
        try:
            # mode 0700: the directory holds session history and, off macOS,
            # the account's credentials.
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError as exc:
            print(f"Error: cannot create '{directory}': {exc}", file=sys.stderr)
            return 1
        print(f"Created {directory} — claude will ask you to /login.")

    return launch(name, args.claude_args)


if __name__ == "__main__":
    raise SystemExit(main())
