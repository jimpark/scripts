#!/usr/bin/env python3
"""ado-open.py — Open an Azure DevOps work item or pull request by its number.

Give it a bug or PBI number, or a pull request number, and it looks the item up
with the `az` CLI, prints its title and URL, and opens the URL in your browser:

    ado-open '#645265'        # work item (bug, PBI, task, ...)
    ado-open '!176642'        # pull request
    ado-open wi 645265        # same as #645265, no quoting needed
    ado-open pr 176642        # same as !176642, no quoting needed
    ado-open 645265           # bare number: tries both

WHY QUOTE # AND !
-----------------
In bash and PowerShell, `#` after a space starts a comment, so an unquoted
`ado-open #1234` passes no argument at all. Interactive bash also expands `!1234`
from its history. Quote those forms, or use the word forms (`wi`, `bug`, `pbi`,
`pr`), which need no quoting in any shell. cmd.exe passes both through as-is.

A BARE NUMBER
-------------
Work item and pull request IDs share one number range, so a bare number is
looked up as both. If exactly one exists, it opens. If both exist, both are
listed and neither opens; rerun with a prefix to pick one.

WHERE THE URL COMES FROM
------------------------
The project URL lives in `ado-open.ini` beside this script. The file is
gitignored; the first run creates it with the default project URL:

    [ado]
    base_url = https://dev.azure.com/whqmeps/MEPS

The organization is the part before the project. `az` queries that organization,
and the item's own project replaces the configured one in the URL, so an item
from another project in the same organization still resolves.

Usage:
    ado-open.py [-n] ID [ID ...]

Exit status:
    0   every ID resolved (and opened, unless -n)
    1   an ID was not found, was ambiguous, or `az` failed
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

CONFIG_PATH = Path(__file__).resolve().parent / "ado-open.ini"
DEFAULT_BASE_URL = "https://dev.azure.com/whqmeps/MEPS"
CONFIG_TEMPLATE = """\
# Configuration for ado-open. This file is gitignored; edit it freely.
#
# base_url is the Azure DevOps project URL: https://dev.azure.com/<org>/<project>
[ado]
base_url = {0}
"""

WORK_ITEM = "wi"
PULL_REQUEST = "pr"
EITHER = "either"

# Words accepted before a number, and the kind each one selects.
KIND_WORDS = {
    "wi": WORK_ITEM, "bug": WORK_ITEM, "pbi": WORK_ITEM,
    "pr": PULL_REQUEST,
}
PREFIXES = {"#": WORK_ITEM, "!": PULL_REQUEST}
ID_RE = re.compile(r"^([#!]?)(\d+)$")

# TF401232: work item does not exist. TF401180: pull request not found.
NOT_FOUND_CODES = ("TF401232", "TF401180")


class UsageError(Exception):
    """Raised when the ID arguments can't be parsed."""


class AzError(Exception):
    """Raised when `az` fails for a reason other than "not found"."""


# ── Configuration ─────────────────────────────────────────────────────────────

def load_base_url():
    """Return the configured project URL, creating the config file if missing."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(CONFIG_TEMPLATE.format(DEFAULT_BASE_URL),
                               encoding="utf-8")
        sys.stderr.write("ado-open: created {0} with base_url = {1}\n"
                         .format(CONFIG_PATH, DEFAULT_BASE_URL))
    parser = configparser.ConfigParser()
    try:
        parser.read(CONFIG_PATH, encoding="utf-8")
    except configparser.Error as exc:
        raise AzError("cannot parse {0}: {1}".format(CONFIG_PATH, exc))
    base_url = parser.get("ado", "base_url", fallback="").strip().rstrip("/")
    if not base_url:
        raise AzError("{0} has no base_url in its [ado] section"
                      .format(CONFIG_PATH))
    return base_url


def split_base_url(base_url):
    """Split https://dev.azure.com/<org>/<project> into (org URL, project)."""
    org_url, _, project = base_url.rpartition("/")
    if not org_url or not project or "://" not in org_url:
        raise AzError("base_url must look like https://dev.azure.com/<org>/<project>,"
                      " got {0!r}".format(base_url))
    return org_url, urllib.parse.unquote(project)


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_targets(tokens):
    """Turn the ID arguments into a list of (kind, number) pairs."""
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
        prefix, number = match.groups()
        if pending_kind is not None and prefix:
            raise UsageError("{0!r} already has a prefix; drop the word before it"
                             .format(token))
        kind = pending_kind or PREFIXES.get(prefix, EITHER)
        targets.append((kind, int(number)))
        pending_kind = None
    if pending_kind is not None:
        raise UsageError("{0!r} needs a number after it".format(tokens[-1]))
    if not targets:
        raise UsageError("no IDs given")
    return targets


# ── az lookups ────────────────────────────────────────────────────────────────

def run_az(az, args):
    """Run `az` and return its parsed JSON, or None if the item does not exist."""
    proc = subprocess.run([az] + args + ["-o", "json"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode == 0:
        return json.loads(proc.stdout)
    if any(code in proc.stderr for code in NOT_FOUND_CODES):
        return None
    message = proc.stderr.strip() or "az exited with status {0}".format(
        proc.returncode)
    raise AzError(message.removeprefix("ERROR: "))


def lookup_work_item(az, org_url, number):
    """Return a result dict for a work item, or None if it does not exist."""
    data = run_az(az, ["boards", "work-item", "show", "--id", str(number),
                       "--org", org_url])
    if data is None:
        return None
    fields = data.get("fields", {})
    project = urllib.parse.quote(fields.get("System.TeamProject", ""))
    return {
        "label": "{0} {1}".format(fields.get("System.WorkItemType", "Work item"),
                                  number),
        "title": fields.get("System.Title", ""),
        "state": fields.get("System.State", ""),
        "url": "{0}/{1}/_workitems/edit/{2}".format(org_url, project, number),
    }


def lookup_pull_request(az, org_url, number):
    """Return a result dict for a pull request, or None if it does not exist."""
    data = run_az(az, ["repos", "pr", "show", "--id", str(number),
                       "--org", org_url])
    if data is None:
        return None
    repo = data.get("repository", {})
    project = urllib.parse.quote(repo.get("project", {}).get("name", ""))
    return {
        "label": "Pull request {0} ({1})".format(number, repo.get("name", "")),
        "title": data.get("title", ""),
        "state": data.get("status", ""),
        "url": "{0}/{1}/_git/{2}/pullrequest/{3}".format(
            org_url, project, urllib.parse.quote(repo.get("name", "")), number),
    }


LOOKUPS = {WORK_ITEM: lookup_work_item, PULL_REQUEST: lookup_pull_request}


def resolve(az, org_url, kind, number):
    """Return the list of items that match one target (empty if none)."""
    kinds = (WORK_ITEM, PULL_REQUEST) if kind == EITHER else (kind,)
    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        found = list(pool.map(lambda k: LOOKUPS[k](az, org_url, number), kinds))
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


def target_name(kind, number):
    if kind == WORK_ITEM:
        return "work item {0}".format(number)
    if kind == PULL_REQUEST:
        return "pull request {0}".format(number)
    return "{0}".format(number)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="ado-open",
        description="Look up Azure DevOps work items and pull requests by "
                    "number with the az CLI, print their URLs, and open them "
                    "in the browser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "ID forms:\n"
            "  #1234          work item (bug, PBI, task, ...)\n"
            "  !1234          pull request\n"
            "  wi 1234        work item; 'bug' and 'pbi' are aliases for 'wi'\n"
            "  pr 1234        pull request\n"
            "  1234           either; opens the one that exists, and lists\n"
            "                 both without opening if both exist\n"
            "\n"
            "quoting:\n"
            "  bash and PowerShell treat #1234 as a comment, and interactive\n"
            "  bash expands !1234 from history. Quote them ('#1234', '!1234')\n"
            "  or use the word forms. cmd.exe needs no quotes.\n"
            "\n"
            "examples:\n"
            "  ado-open '#645265'           open a work item\n"
            "  ado-open pr 176642           open a pull request\n"
            "  ado-open -n bug 645265       print the URL, don't open it\n"
            "  ado-open '#645265' '!176642' open several at once\n"
            "\n"
            "configuration:\n"
            "  The project URL is base_url in the [ado] section of\n"
            "  " + str(CONFIG_PATH) + "\n"
            "  The first run creates it with " + DEFAULT_BASE_URL + ".\n"
            "\n"
            "When stdout is not a terminal, only the URLs are printed, one per\n"
            "line, so 'ado-open -n !1234 | clip' copies just the URL.\n"
            "\n"
            "Requires the az CLI with the azure-devops extension, logged in\n"
            "(az login).\n"
        ),
    )
    parser.add_argument("ids", nargs="+", metavar="ID",
                        help="an ID in one of the forms below")
    parser.add_argument("-n", "--print", dest="print_only", action="store_true",
                        help="print the URLs without opening the browser")
    parser.add_argument("-V", "--version", action="version",
                        version="%(prog)s " + __version__)
    args = parser.parse_args(argv)

    try:
        targets = parse_targets(args.ids)
    except UsageError as exc:
        parser.error(str(exc))

    az = shutil.which("az")
    if az is None:
        sys.stderr.write("ado-open: the az CLI is not on PATH. Install it and "
                         "run 'az extension add --name azure-devops'.\n")
        return 1

    try:
        org_url, _ = split_base_url(load_base_url())
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            results = list(pool.map(
                lambda t: resolve(az, org_url, t[0], t[1]), targets))
    except AzError as exc:
        sys.stderr.write("ado-open: " + str(exc) + "\n")
        return 1

    interactive = sys.stdout.isatty()
    color = use_color(sys.stdout)
    status = 0
    for (kind, number), found in zip(targets, results):
        if not found:
            sys.stderr.write("ado-open: {0} not found\n"
                             .format(target_name(kind, number)))
            status = 1
            continue
        if len(found) > 1:
            sys.stderr.write("ado-open: {0} is both a work item and a pull "
                             "request; rerun with '#{0}' or '!{0}':\n"
                             .format(number))
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
