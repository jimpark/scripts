#!/usr/bin/env python3
"""mfn-cascade.py — Trace a Foundation library release down its GitLab build tree.

Give it a project and the version that carries a change, and it follows the
project's downstream projects (mfn-lib-tcu, mfn-lib-core, mfn-lib-core-cpp,
mfn-lib-core-cs, ...) to report, for each one, the first version built on top
of that change and the state of its pipeline:

    mfn-cascade toolchain 1.46.1
    mfn-cascade tcu 1.48.1
    mfn-cascade mfn-lib-core@3.20.5

HOW IT WORKS
------------
Each project's `.project-manager.yaml` lists its downstream projects and the
branch to follow in each. Each project's `mmpackage.json` records its own
version and the version it requires of its upstream package. When an upstream
release lands, the cascade commits "Require dependency 'pkg@x.y.z'" and then
"Bump version to 'a.b.c'" on the downstream branch.

For each downstream project the script reads `mmpackage.json` at every commit
that changed it since the upstream version was set. The first commit whose
upstream requirement reaches the traced version is where the change arrived.
The first version set at or after that commit is the version that carries it.
That commit's pipeline (and its child pipeline) gives the build status.

Usage:
    mfn-cascade.py [options] PROJECT VERSION
    mfn-cascade.py [options] PROJECT@VERSION

Exit status:
    0   the tree was traced
    1   the starting version was not found, or a glab call failed
    2   usage error (bad or missing arguments; handled by argparse)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

__version__ = "1.0.0"

DEFAULT_HOST = "gitlab.whqmeps.org"
DEFAULT_GROUP = "meps-foundation"
MMPACKAGE = "mmpackage.json"
PROJECT_MANAGER = ".project-manager.yaml"
PUBLISH_JOB = "publish-mm-packages"
VERSION_SET_RE = re.compile(r"(?:Bump version to|for release to) '([^']+)'")

# Result states for one project in the tree.
FIXED = "fixed"
PENDING_BUMP = "pending-bump"
NOT_YET = "not-yet"
WAITING = "waiting"
UNPINNED = "unpinned"
NO_PACKAGE = "no-package"


class GlabError(RuntimeError):
    pass


class GitLab:
    def __init__(self, host):
        self.host = host
        self._mm_cache = {}

    def get(self, path, paginate=False, missing_ok=False):
        args = ["glab", "api", "--hostname", self.host, path]
        if paginate:
            args += ["--paginate", "--output", "ndjson"]
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip()
            if missing_ok and "404" in message:
                return None
            raise GlabError(f"glab api {path}: {message}")
        if paginate:
            return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        return proc.stdout

    def get_json(self, path, missing_ok=False):
        text = self.get(path, missing_ok=missing_ok)
        return None if text is None else json.loads(text)

    @staticmethod
    def project_path(project):
        return "projects/" + urllib.parse.quote(project, safe="")

    def raw_file(self, project, path, ref):
        return self.get(
            f"{self.project_path(project)}/repository/files/"
            f"{urllib.parse.quote(path, safe='')}/raw?ref={urllib.parse.quote(ref, safe='')}",
            missing_ok=True,
        )

    def mmpackage(self, project, ref):
        key = (project, ref)
        if key not in self._mm_cache:
            text = self.raw_file(project, MMPACKAGE, ref)
            self._mm_cache[key] = parse_mmpackage(text) if text is not None else None
        return self._mm_cache[key]

    def branch_exists(self, project, branch):
        quoted = urllib.parse.quote(branch, safe="")
        return self.get(f"{self.project_path(project)}/repository/branches/{quoted}",
                        missing_ok=True) is not None

    def head_commit(self, project, branch):
        quoted = urllib.parse.quote(branch, safe="")
        data = self.get_json(f"{self.project_path(project)}/repository/branches/{quoted}")
        return data["commit"]

    def mmpackage_commits(self, project, branch, since=None):
        """Commits on branch that changed mmpackage.json, oldest first."""
        query = {"ref_name": branch, "path": MMPACKAGE, "per_page": 100}
        if since:
            query["since"] = since
        commits = self.get(
            f"{self.project_path(project)}/repository/commits?{urllib.parse.urlencode(query)}",
            paginate=True,
        )
        return list(reversed(commits))

    def downstreams(self, project, ref):
        text = self.raw_file(project, PROJECT_MANAGER, ref)
        return parse_downstreams(text) if text else []

    def pipeline_summary(self, project, sha):
        pipelines = self.get_json(f"{self.project_path(project)}/pipelines?sha={sha}&per_page=5")
        if not pipelines:
            return None
        # The push pipeline builds and publishes the version. A later pipeline on the same
        # commit, started by an upstream cascade, only commits the next dependency update.
        pipeline = next((p for p in pipelines if p["source"] == "push"), pipelines[0])
        jobs = self._pipeline_jobs(project, pipeline["id"], pipeline["project_id"])
        return PipelineSummary(pipeline["id"], pipeline["web_url"], pipeline["status"], jobs)

    def _pipeline_jobs(self, project, pipeline_id, project_id):
        """Jobs and trigger jobs of a pipeline and of its child pipelines in the same project.

        A trigger job that starts another project's pipeline counts as a job here; that
        project's own build appears under its own row in the tree.
        """
        base = f"{self.project_path(project)}/pipelines/{pipeline_id}"
        jobs = self.get_json(f"{base}/jobs?per_page=100")
        bridges = self.get_json(f"{base}/bridges?per_page=100") or []
        for bridge in bridges:
            child = bridge.get("downstream_pipeline")
            if child and child.get("project_id") == project_id:
                jobs += self._pipeline_jobs(project, child["id"], project_id)
            else:
                jobs.append(bridge)
        return jobs


class PipelineSummary:
    def __init__(self, pipeline_id, url, status, jobs):
        self.id = pipeline_id
        self.url = url
        self.status = status
        self.jobs = jobs

    @property
    def published(self):
        return any(j["name"] == PUBLISH_JOB and j["status"] == "success" for j in self.jobs)

    def describe(self):
        done = sum(1 for j in self.jobs if j["status"] in ("success", "skipped"))
        text = self.status
        if not self.jobs:
            return text
        if self.status != "success":
            text += f", {done}/{len(self.jobs)} jobs done"
        text += ", published" if self.published else ", not published"
        for state in ("failed", "running", "pending"):
            names = [j["name"] for j in self.jobs if j["status"] == state]
            if names:
                text += f"; {state}: {', '.join(names)}"
        return text


class Node:
    def __init__(self, project, branch):
        self.project = project
        self.branch = branch
        self.state = None
        self.version = None
        self.package = None
        self.detail = ""
        self.pipeline = None
        self.children = []
        self.error = None


def parse_mmpackage(text):
    data = json.loads(text)
    deps = {}
    for dep in (data.get("Dependencies") or []) + (data.get("BuildDependencies") or []):
        deps.setdefault(dep["Name"], dep.get("Version"))
    return {"name": data.get("Name"), "version": data.get("Version"), "deps": deps}


def parse_downstreams(text):
    """Read gitlab.downstream from .project-manager.yaml without a YAML library."""
    result = []
    in_downstream = False
    downstream_indent = 0
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("downstream:"):
            in_downstream = True
            downstream_indent = indent
            continue
        if not in_downstream:
            continue
        if indent <= downstream_indent and not stripped.startswith("-"):
            break
        if stripped.startswith("- "):
            current = {}
            result.append(current)
            stripped = stripped[2:].strip()
        if current is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            current[key.strip()] = value.strip().strip("'\"")
    return [(d["project"], d.get("branch", "master")) for d in result if "project" in d]


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def at_least(version, minimum):
    return version is not None and version_key(version) >= version_key(minimum)


def resolve_project(name, group):
    if "/" in name:
        return name
    if not name.startswith("mfn-"):
        name = "mfn-lib-" + name
    return f"{group}/{name}"


def short_name(project):
    return project.rsplit("/", 1)[-1]


def find_version_commit(gitlab, project, branch, version):
    for commit in reversed(gitlab.mmpackage_commits(project, branch)):
        match = VERSION_SET_RE.search(commit["title"])
        if match and match.group(1) == version:
            return commit
    return None


def locate_start(gitlab, project, version, branch):
    if branch:
        candidates = [branch]
    else:
        major_minor = ".".join(version.split(".")[:2])
        candidates = [f"release-{major_minor}", "master"]
    for candidate in candidates:
        if not gitlab.branch_exists(project, candidate):
            continue
        commit = find_version_commit(gitlab, project, candidate, version)
        if commit:
            return candidate, commit
    return None, None


def trace_downstream(gitlab, node, upstream_package, upstream_version, since):
    """Find the first version of node that requires upstream_package >= upstream_version."""
    head_mm = gitlab.mmpackage(node.project, node.branch)
    if head_mm is None:
        node.state = NO_PACKAGE
        node.detail = f"no {MMPACKAGE} on {node.branch}"
        return
    node.package = head_mm["name"]
    if upstream_version is None:
        node.state = WAITING
        node.version = head_mm["version"]
        node.detail = f"{upstream_package} has no version with the change yet"
        return
    if head_mm["deps"].get(upstream_package) is None:
        node.state = UNPINNED
        node.version = head_mm["version"]
        node.detail = f"{MMPACKAGE} does not pin a {upstream_package} version"
        return

    commits = gitlab.mmpackage_commits(node.project, node.branch, since=since)
    previous = None
    if commits and commits[0].get("parent_ids"):
        parent_mm = gitlab.mmpackage(node.project, commits[0]["parent_ids"][0])
        previous = parent_mm["version"] if parent_mm else None

    arrived = None
    for commit in commits:
        mm = gitlab.mmpackage(node.project, commit["id"])
        if mm is None:
            continue
        if arrived is None:
            if at_least(mm["deps"].get(upstream_package), upstream_version):
                arrived = commit
            else:
                previous = mm["version"]
                continue
        if mm["version"] != previous:
            node.state = FIXED
            node.version = mm["version"]
            node.detail = f"requires {upstream_package}@{mm['deps'][upstream_package]}"
            node.pipeline = gitlab.pipeline_summary(node.project, commit["id"])
            return

    head = gitlab.head_commit(node.project, node.branch)
    node.version = head_mm["version"]
    node.pipeline = gitlab.pipeline_summary(node.project, head["id"])
    if arrived is not None:
        node.state = PENDING_BUMP
        node.detail = (f"requires {upstream_package}@{head_mm['deps'][upstream_package]} "
                       f"since {arrived['short_id']}, version not bumped yet")
    elif at_least(head_mm["deps"].get(upstream_package), upstream_version):
        # The branch was created after the change and has not changed mmpackage.json since.
        node.state = FIXED
        node.detail = f"requires {upstream_package}@{head_mm['deps'][upstream_package]}"
    else:
        node.state = NOT_YET
        node.detail = (f"latest {head_mm['version']} requires "
                       f"{upstream_package}@{head_mm['deps'][upstream_package]}")


def build_tree(gitlab, node, since):
    """Trace node's downstream projects in parallel, then recurse into each."""
    carried = node.version if node.state == FIXED else None
    children = [Node(p, b) for p, b in gitlab.downstreams(node.project, node.branch)]
    node.children = children

    def visit(child):
        try:
            trace_downstream(gitlab, child, node.package, carried, since)
        except GlabError as err:
            child.error = str(err)
            return
        if child.package is not None:
            build_tree(gitlab, child, since)

    if children:
        # A pool per level, because a shared pool deadlocks when parents wait on children.
        with ThreadPoolExecutor(max_workers=len(children)) as pool:
            list(pool.map(visit, children))


class Painter:
    COLORS = {FIXED: "32", PENDING_BUMP: "33", NOT_YET: "33", WAITING: "2",
              UNPINNED: "35", NO_PACKAGE: "2", "error": "31"}

    def __init__(self, color):
        self.color = color

    def paint(self, text, state):
        code = self.COLORS.get(state)
        if not self.color or not code:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"


def flatten(node, prefix="", last=True, root=True, rows=None):
    rows = [] if rows is None else rows
    branch = "" if root else ("└── " if last else "├── ")
    rows.append((prefix + branch, node))
    child_prefix = prefix + ("" if root else ("    " if last else "│   "))
    for index, child in enumerate(node.children):
        flatten(child, child_prefix, index == len(node.children) - 1, False, rows)
    return rows


STATE_LABELS = {PENDING_BUMP: "pending", NOT_YET: "not yet", WAITING: "waiting",
                UNPINNED: "unknown", NO_PACKAGE: "unknown"}


def status_text(node):
    if node.state == FIXED:
        build = node.pipeline.describe() if node.pipeline else "no pipeline found"
        return f"{build}; {node.detail}" if node.detail else build
    text = f"{STATE_LABELS[node.state]}: {node.detail}"
    if node.pipeline:
        text += f"; head build {node.pipeline.describe()}"
    return text


def render(root, painter, show_urls):
    rows = flatten(root)
    name_width = max(len(lead) + len(short_name(n.project)) for lead, n in rows)
    branch_width = max(len(n.branch) for _, n in rows)
    version_width = max(len(n.version if n.state == FIXED else "-") for _, n in rows)
    lines = []
    for lead, node in rows:
        name = (lead + short_name(node.project)).ljust(name_width)
        if node.error:
            lines.append(f"{name}  {node.branch.ljust(branch_width)}  "
                         + painter.paint(f"error: {node.error}", "error"))
            continue
        version = node.version if node.state == FIXED else "-"
        text = status_text(node)
        lines.append(f"{name}  {node.branch.ljust(branch_width)}  "
                     f"{version.ljust(version_width)}  {painter.paint(text, node.state)}")
        if show_urls and node.pipeline and node.pipeline.status != "success":
            pad = " " * (name_width + branch_width + version_width + 6)
            lines.append(pad + node.pipeline.url)
    return "\n".join(lines)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="mfn-cascade",
        description="Trace a Foundation library release down its GitLab build tree.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  mfn-cascade toolchain 1.46.1\n"
               "  mfn-cascade tcu@1.48.1\n"
               "  mfn-cascade -b master mfn-lib-core 3.21.23\n",
    )
    parser.add_argument("project", help="project name: toolchain, tcu, mfn-lib-core, or group/path")
    parser.add_argument("version", nargs="?", help="version that carries the change")
    parser.add_argument("-b", "--branch",
                        help="branch that built VERSION (default: release-X.Y, then master)")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"GitLab host (default: {DEFAULT_HOST})")
    parser.add_argument("--group", default=DEFAULT_GROUP,
                        help=f"group for bare project names (default: {DEFAULT_GROUP})")
    parser.add_argument("--no-urls", action="store_true",
                        help="omit pipeline URLs for builds that have not succeeded")
    parser.add_argument("--no-color", action="store_true", help="disable color")
    parser.add_argument("-V", "--version-info", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.version is None:
        if "@" not in args.project:
            parser.error("give a VERSION, or write PROJECT@VERSION")
        args.project, args.version = args.project.rsplit("@", 1)
    if not re.fullmatch(r"\d+(\.\d+)*", args.version):
        parser.error(f"not a version: {args.version}")
    return args


def main(argv=None):
    args = parse_args(argv)
    gitlab = GitLab(args.host)
    project = resolve_project(args.project, args.group)
    if sys.stderr.isatty():
        print(f"Tracing {short_name(project)} {args.version} on {args.host} ...", file=sys.stderr)
    try:
        branch, commit = locate_start(gitlab, project, args.version, args.branch)
        if commit is None:
            where = args.branch or f"release-{'.'.join(args.version.split('.')[:2])} or master"
            print(f"mfn-cascade: no commit sets {short_name(project)} version {args.version} "
                  f"on {where}", file=sys.stderr)
            return 1
        root = Node(project, branch)
        root.state = FIXED
        root.version = args.version
        root.package = gitlab.mmpackage(project, commit["id"])["name"]
        root.pipeline = gitlab.pipeline_summary(project, commit["id"])
        build_tree(gitlab, root, commit["committed_date"])
    except GlabError as err:
        print(f"mfn-cascade: {err}", file=sys.stderr)
        return 1
    color = not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    print(render(root, Painter(color), not args.no_urls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
