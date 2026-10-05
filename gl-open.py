#!/usr/bin/env python3
"""gl-open.py — Open a GitLab issue or merge request by its number.

Give it an issue or merge request number, and it looks the item up with the
`glab` CLI, prints its title and URL, and opens the URL in your browser:

    gl-open '#42'                     # issue in the current project
    gl-open '!17'                     # merge request in the current project
    gl-open issue 42                  # same as #42, no quoting needed
    gl-open mr 17                     # same as !17, no quoting needed
    gl-open 42                        # bare number: tries both
    gl-open 'group/project!17'        # GitLab reference syntax, any project
    gl-open -p group/project mr 17    # same, with the project as an option

WHICH PROJECT
-------------
GitLab numbers issues and merge requests per project, so every number needs a
project. The first of these that applies supplies it:

    1. The project in the reference itself: group/project#42.
    2. -p/--project.
    3. The current directory's git remote, if it points at the configured host.
       `origin` wins; otherwise the first remote on that host.
    4. `project` in the config file.

WHY QUOTE # AND !
-----------------
In bash and PowerShell, `#` after a space starts a comment, so an unquoted
`gl-open #42` passes no argument at all. Interactive bash also expands `!17`
from its history. Quote those forms, or use the word forms (`issue`, `mr`),
which need no quoting in any shell. cmd.exe passes both through as-is.

A BARE NUMBER
-------------
Issues and merge requests have separate number sequences in each project, so a
bare number often matches both. If exactly one exists, it opens. If both exist,
both are listed and neither opens; rerun with a prefix to pick one.

WHERE THE HOST COMES FROM
-------------------------
The GitLab host lives in `gl-open.ini` beside this script. The file is
gitignored; the first run creates it with the default host:

    [gitlab]
    host = gitlab.whqmeps.org
    # project = group/project

Usage:
    gl-open.py [-n] [-p PROJECT] ID [ID ...]

Exit status:
    0   every ID resolved (and opened, unless -n)
    1   an ID was not found, was ambiguous, had no project, or `glab` failed
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

__version__ = "1.0.0"

CONFIG_PATH = Path(__file__).resolve().parent / "gl-open.ini"
DEFAULT_HOST = "gitlab.whqmeps.org"
CONFIG_TEMPLATE = """\
# Configuration for gl-open. This file is gitignored; edit it freely.
#
# host is the GitLab server that glab queries. project is the default
# group/project, used when neither the ID, -p, nor the current directory's git
# remote names one.
[gitlab]
host = {0}
# project = group/project
"""

ISSUE = "issue"
MERGE_REQUEST = "mr"
EITHER = "either"

# Words accepted before a number, and the kind each one selects.
KIND_WORDS = {"issue": ISSUE, "mr": MERGE_REQUEST}
PREFIXES = {"#": ISSUE, "!": MERGE_REQUEST}
# An optional group/project, an optional # or ! prefix, then the number.
ID_RE = re.compile(r"^(?:([\w.-]+(?:/[\w.-]+)+)(?=[#!]))?([#!]?)(\d+)$")


class UsageError(Exception):
    """Raised when the ID arguments can't be parsed."""


class GlabError(Exception):
    """Raised when `glab` fails for a reason other than "not found"."""


# ── Configuration ─────────────────────────────────────────────────────────────

def load_config():
    """Return (host, default project or None), creating the config if missing."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(CONFIG_TEMPLATE.format(DEFAULT_HOST),
                               encoding="utf-8")
        sys.stderr.write("gl-open: created {0} with host = {1}\n"
                         .format(CONFIG_PATH, DEFAULT_HOST))
    parser = configparser.ConfigParser()
    try:
        parser.read(CONFIG_PATH, encoding="utf-8")
    except configparser.Error as exc:
        raise GlabError("cannot parse {0}: {1}".format(CONFIG_PATH, exc))
    host = parser.get("gitlab", "host", fallback="").strip()
    # Accept a URL as well as a bare host name.
    host = urllib.parse.urlsplit(host).netloc or host.rstrip("/")
    if not host:
        raise GlabError("{0} has no host in its [gitlab] section"
                        .format(CONFIG_PATH))
    project = parser.get("gitlab", "project", fallback="").strip().strip("/")
    return host, project or None


# ── Project from the git remote ───────────────────────────────────────────────

def remote_project(url, host):
    """Return group/project if a remote URL points at host, else None."""
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        if parts.hostname != host.split(":")[0]:
            return None
        path = parts.path
    else:
        # scp-like syntax: git@host:group/project.git
        login, sep, path = url.partition(":")
        if not sep or login.rpartition("@")[2] != host.split(":")[0]:
            return None
    path = path.strip("/").removesuffix(".git")
    return path if "/" in path else None


def cwd_project(host):
    """Return the project of the current directory's GitLab remote, or None."""
    git = shutil.which("git")
    if git is None:
        return None
    proc = subprocess.run([git, "remote"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    remotes = proc.stdout.split()
    if "origin" in remotes:
        remotes.remove("origin")
        remotes.insert(0, "origin")
    for remote in remotes:
        proc = subprocess.run([git, "remote", "get-url", remote],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            project = remote_project(proc.stdout.strip(), host)
            if project:
                return project
    return None


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_targets(tokens):
    """Turn the ID arguments into a list of (project or None, kind, number)."""
    targets = []
    pending_kind = None
    for token in tokens:
        word = token.lower()
        if pending_kind is None and word in KIND_WORDS:
            pending_kind = KIND_WORDS[word]
            continue
        match = ID_RE.match(token)
        if not match:
            raise UsageError("not an ID: {0!r}".format(token))
        project, prefix, number = match.groups()
        if pending_kind is not None and prefix:
            raise UsageError("{0!r} already has a prefix; drop the word before it"
                             .format(token))
        kind = pending_kind or PREFIXES.get(prefix, EITHER)
        targets.append((project, kind, int(number)))
        pending_kind = None
    if pending_kind is not None:
        raise UsageError("{0!r} needs a number after it".format(tokens[-1]))
    if not targets:
        raise UsageError("no IDs given")
    return targets


# ── glab lookups ──────────────────────────────────────────────────────────────

def run_glab(glab, host, path):
    """Run `glab api` and return its parsed JSON, or None if the item is missing.

    A missing project raises GlabError, so a mistyped project isn't reported as
    a missing item.
    """
    proc = subprocess.run([glab, "api", "--hostname", host, path],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode == 0:
        return json.loads(proc.stdout)
    output = proc.stdout + proc.stderr
    if "404 Project Not Found" in output:
        raise GlabError("project not found on {0}: {1}".format(
            host, urllib.parse.unquote(path.split("/")[1])))
    if "404" in output:
        return None
    message = proc.stderr.strip() or proc.stdout.strip() or (
        "glab exited with status {0}".format(proc.returncode))
    raise GlabError(message.removeprefix("glab: "))


def lookup(glab, host, project, kind, number):
    """Return a result dict for an issue or MR, or None if it does not exist."""
    endpoint = "issues" if kind == ISSUE else "merge_requests"
    data = run_glab(glab, host, "projects/{0}/{1}/{2}".format(
        urllib.parse.quote(project, safe=""), endpoint, number))
    if data is None:
        return None
    reference = data.get("references", {}).get("full") or "{0}{1}{2}".format(
        project, "#" if kind == ISSUE else "!", number)
    state = data.get("state", "")
    if data.get("draft"):
        state = "draft, " + state
    return {
        "label": "{0} {1}".format(
            "Issue" if kind == ISSUE else "Merge request", reference),
        "title": data.get("title", ""),
        "state": state,
        "url": data.get("web_url", ""),
    }


def resolve(glab, host, project, kind, number):
    """Return the list of items that match one target (empty if none)."""
    kinds = (ISSUE, MERGE_REQUEST) if kind == EITHER else (kind,)
    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        found = list(pool.map(
            lambda k: lookup(glab, host, project, k, number), kinds))
    return [item for item in found if item is not None]


# ── Output ────────────────────────────────────────────────────────────────────

def use_color(stream):
    return stream.isatty() and "NO_COLOR" not in os.environ


def describe(item, color):
    """One line: bold label, title, and state."""
    label = item["label"]
    if color:
        label = "\033[1m" + label + "\033[0m"
    state = " [{0}]".format(item["state"]) if item["state"] else ""
    return "{0}: {1}{2}".format(label, item["title"], state)


def target_name(project, kind, number):
    if kind == ISSUE:
        return "issue {0}#{1}".format(project, number)
    if kind == MERGE_REQUEST:
        return "merge request {0}!{1}".format(project, number)
    return "{0} {1}".format(project, number)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="gl-open",
        description="Look up GitLab issues and merge requests by number with "
                    "the glab CLI, print their URLs, and open them in the "
                    "browser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "ID forms:\n"
            "  #42                  issue\n"
            "  !17                  merge request\n"
            "  issue 42             issue\n"
            "  mr 17                merge request\n"
            "  42                   either; opens the one that exists, and\n"
            "                       lists both without opening if both exist\n"
            "  group/project#42     issue in that project\n"
            "  group/project!17     merge request in that project\n"
            "\n"
            "project:\n"
            "  An ID without a project uses -p, else the current directory's\n"
            "  git remote on the configured host (origin first), else the\n"
            "  project in the config file.\n"
            "\n"
            "quoting:\n"
            "  bash and PowerShell treat #42 as a comment, and interactive\n"
            "  bash expands !17 from history. Quote them ('#42', '!17') or\n"
            "  use the word forms. cmd.exe needs no quotes.\n"
            "\n"
            "examples:\n"
            "  gl-open '#42'                     open an issue\n"
            "  gl-open mr 17                     open a merge request\n"
            "  gl-open -n issue 42               print the URL, don't open it\n"
            "  gl-open 'meps-foundation/mfn-idl!1'\n"
            "                                    open an MR in another project\n"
            "  gl-open '#42' '!17'               open several at once\n"
            "\n"
            "configuration:\n"
            "  The GitLab host is host in the [gitlab] section of\n"
            "  " + str(CONFIG_PATH) + "\n"
            "  The first run creates it with " + DEFAULT_HOST + ".\n"
            "\n"
            "When stdout is not a terminal, only the URLs are printed, one per\n"
            "line, so 'gl-open -n !17 | pbcopy' copies just the URL.\n"
            "\n"
            "Requires the glab CLI, logged in to the host\n"
            "(glab auth login --hostname <host>).\n"
        ),
    )
    parser.add_argument("ids", nargs="+", metavar="ID",
                        help="an ID in one of the forms below")
    parser.add_argument("-p", "--project", metavar="PROJECT",
                        help="group/project for IDs that don't name one")
    parser.add_argument("-n", "--print", dest="print_only", action="store_true",
                        help="print the URLs without opening the browser")
    parser.add_argument("-V", "--version", action="version",
                        version="%(prog)s " + __version__)
    args = parser.parse_args(argv)

    try:
        targets = parse_targets(args.ids)
    except UsageError as exc:
        parser.error(str(exc))

    glab = shutil.which("glab")
    if glab is None:
        sys.stderr.write("gl-open: the glab CLI is not on PATH. Install it and "
                         "run 'glab auth login'.\n")
        return 1

    try:
        host, config_project = load_config()
    except GlabError as exc:
        sys.stderr.write("gl-open: " + str(exc) + "\n")
        return 1

    default_project = None
    if any(project is None for project, _, _ in targets):
        default_project = (args.project and args.project.strip("/")) \
            or cwd_project(host) or config_project
        if default_project is None:
            sys.stderr.write(
                "gl-open: no project for {0}. Pass -p group/project, write "
                "group/project#N, run from a checkout of a {1} project, or set "
                "project in {2}.\n".format(
                    " ".join(args.ids), host, CONFIG_PATH))
            return 1
    targets = [(project or default_project, kind, number)
               for project, kind, number in targets]

    try:
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            results = list(pool.map(
                lambda t: resolve(glab, host, *t), targets))
    except GlabError as exc:
        sys.stderr.write("gl-open: " + str(exc) + "\n")
        return 1

    interactive = sys.stdout.isatty()
    color = use_color(sys.stdout)
    status = 0
    for (project, kind, number), found in zip(targets, results):
        if not found:
            sys.stderr.write("gl-open: {0} not found\n"
                             .format(target_name(project, kind, number)))
            status = 1
            continue
        if len(found) > 1:
            sys.stderr.write("gl-open: {0} {1} is both an issue and a merge "
                             "request; rerun with '#{1}' or '!{1}':\n"
                             .format(project, number))
            for item in found:
                sys.stderr.write("  " + describe(item, use_color(sys.stderr))
                                 + "\n  " + item["url"] + "\n")
            status = 1
            continue
        item = found[0]
        if interactive:
            print(describe(item, color))
            print("  " + item["url"])
        else:
            print(item["url"])
        if not args.print_only:
            webbrowser.open(item["url"])
    return status


if __name__ == "__main__":
    sys.exit(main())
