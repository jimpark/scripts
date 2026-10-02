#!/usr/bin/env python3
"""mfn-cascade.py — Trace a Foundation library release down its GitLab build tree.

Give it a project and the version that carries a change, and it follows the
project's downstream projects (mfn-lib-tcu, mfn-lib-core, mfn-lib-core-cpp,
mfn-lib-core-cs, ...) to report, for each one, the first version built on top
of that change and the state of its pipeline:

    mfn-cascade toolchain 1.46.1
    mfn-cascade tcu 1.48.1
    mfn-cascade mfn-lib-core@3.20.5
    mfn-cascade toolchain 1.46       # the newest 1.46.x build
    mfn-cascade toolchain latest     # the newest build on master

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
import shutil
import subprocess
import sys
import textwrap
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

__version__ = "1.2.0"

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

    def mmpackage_commits(self, project, branch, since=None, first_page=False):
        """Commits on branch that changed mmpackage.json, oldest first.

        With first_page, only the newest 100 such commits.
        """
        query = {"ref_name": branch, "path": MMPACKAGE, "per_page": 100}
        if since:
            query["since"] = since
        path = f"{self.project_path(project)}/repository/commits?{urllib.parse.urlencode(query)}"
        commits = self.get_json(path) if first_page else self.get(path, paginate=True)
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

    @property
    def running(self):
        return self.status in ("created", "pending", "preparing", "running",
                               "scheduled", "waiting_for_resource")

    @property
    def clean(self):
        return self.status == "success" and self.published

    def jobs_in(self, *states):
        return [j["name"] for j in self.jobs if j["status"] in states]

    def progress(self):
        done = len(self.jobs_in("success", "skipped"))
        return f"{done}/{len(self.jobs)} jobs done"


class Node:
    def __init__(self, project, branch):
        self.project = project
        self.branch = branch
        self.state = None
        self.version = None
        self.package = None
        self.detail = ""
        self.pipeline = None
        self.upstream = None
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


def set_version(commit):
    """The version a commit sets in mmpackage.json, read from its message, or None."""
    match = VERSION_SET_RE.search(commit["title"])
    return match.group(1) if match else None


def find_version_commit(gitlab, project, branch, version=None):
    """The commit that set version on branch, or with version None, the newest one.

    Returns (version, commit), or (None, None). The newest 100 commits answer most
    lookups, so the full history is read only when they do not.
    """
    for first_page in (True, False):
        commits = gitlab.mmpackage_commits(project, branch, first_page=first_page)
        for commit in reversed(commits):
            found = set_version(commit)
            if found and (version is None or found == version):
                return found, commit
        if len(commits) < 100:
            break
    return None, None


def locate_start(gitlab, project, spec, branch):
    """Resolve a version spec to (branch, version, commit), or (None, None, None).

    spec is "latest" (the newest version on master), "X.Y" (the newest version on
    release-X.Y, or on master while master builds X.Y), or an exact "X.Y.Z".
    """
    if spec == "latest":
        candidates = [branch or "master"]
    elif branch:
        candidates = [branch]
    else:
        candidates = [f"release-{'.'.join(spec.split('.')[:2])}", "master"]
    exact = spec if spec.count(".") >= 2 else None
    for candidate in candidates:
        if not gitlab.branch_exists(project, candidate):
            continue
        version, commit = find_version_commit(gitlab, project, candidate, exact)
        if commit is None:
            continue
        if exact is None and spec != "latest" and not version.startswith(spec + "."):
            continue
        return candidate, version, commit
    return None, None, None


def trace_downstream(gitlab, node, upstream_package, upstream_version, since):
    """Find the first version of node that requires upstream_package >= upstream_version."""
    node.upstream = upstream_package
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
                       f"since {arrived['short_id']}; version not bumped yet")
    elif at_least(head_mm["deps"].get(upstream_package), upstream_version):
        # The branch was created after the change and has not changed mmpackage.json since.
        node.state = FIXED
        node.detail = f"requires {upstream_package}@{head_mm['deps'][upstream_package]}"
    else:
        node.state = NOT_YET
        node.detail = (f"latest is {head_mm['version']}, which requires "
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
    CODES = {"green": "32", "yellow": "33", "red": "31", "magenta": "35", "dim": "2", "bold": "1"}

    def __init__(self, color):
        self.color = color

    def __call__(self, text, style):
        if not self.color or style not in self.CODES:
            return text
        return f"\x1b[{self.CODES[style]}m{text}\x1b[0m"


def flatten(node, lead="", rest="", rows=None):
    """Rows of (first-line prefix, detail-line prefix, node) in tree order.

    The detail prefix continues the tree's vertical bars past a node's own lines.
    """
    rows = [] if rows is None else rows
    rows.append((lead, rest + ("│  " if node.children else "   "), node))
    for index, child in enumerate(node.children):
        last = index == len(node.children) - 1
        flatten(child, rest + ("└─ " if last else "├─ "), rest + ("   " if last else "│  "), rows)
    return rows


def headline(node):
    """The status word for a node's first line, and its color."""
    if node.error:
        return "error", "red"
    if node.state == WAITING:
        return f"waiting on {node.upstream}", "dim"
    if node.state in (UNPINNED, NO_PACKAGE):
        return "unknown", "magenta"
    if node.state == PENDING_BUMP:
        return "pending", "yellow"
    if node.state == NOT_YET:
        return "not yet", "yellow"
    build = node.pipeline
    if build is None:
        return "no pipeline", "magenta"
    if build.clean:
        return "published", "green"
    if build.running:
        return f"building, {build.progress()}", "yellow"
    if build.status == "success":
        return "built, not published", "yellow"
    return build.status, "red"


JOB_NAME_LIMIT = 4


def job_list(names, verbose):
    if verbose or len(names) <= JOB_NAME_LIMIT:
        return ", ".join(names)
    return f"{', '.join(names[:JOB_NAME_LIMIT])}, and {len(names) - JOB_NAME_LIMIT} more"


def detail_lines(node, show_urls, verbose):
    """Lines printed under a node: why it has that state, and what its build is doing.

    Up to JOB_NAME_LIMIT failed jobs show. Running jobs show when there are few of them,
    and queued jobs only with --verbose; the job count on the status line covers the rest.
    """
    if node.error:
        return [node.error]
    lines = []
    if node.detail and (node.state != FIXED or verbose) and node.state != WAITING:
        lines.append(node.detail)
    build = node.pipeline
    if build is None or (build.clean and not verbose):
        return lines
    failed = build.jobs_in("failed")
    running = build.jobs_in("running")
    if node.state == FIXED:
        if failed:
            lines.append(f"failed: {job_list(failed, verbose)}")
    else:
        text = "head build published" if build.clean else f"head build {build.status}"
        if build.jobs and not build.clean:
            text += f", {build.progress()}"
        if failed:
            text += f"; failed: {job_list(failed, verbose)}"
        lines.append(text)
    if running and (verbose or (node.state == FIXED and len(running) <= 3)):
        lines.append(f"running: {', '.join(running)}")
    queued = build.jobs_in("pending", "created")
    if verbose and queued:
        lines.append(f"queued: {', '.join(queued)}")
    if show_urls and not build.clean:
        lines.append(build.url)
    return lines


def render(root, paint, show_urls, verbose, width):
    rows = flatten(root)
    name_width = max(len(lead) + len(short_name(n.project)) for lead, _, n in rows)
    branch_width = max(len(n.branch) for _, _, n in rows)
    version_width = max(len(n.version if n.state == FIXED else "-") for _, _, n in rows)
    out = []
    for lead, rest, node in rows:
        version = node.version if node.state == FIXED else "-"
        word, style = headline(node)
        out.append(f"{lead}{paint(short_name(node.project), 'bold')}"
                   f"{' ' * (name_width - len(lead) - len(short_name(node.project)))}  "
                   f"{paint(node.branch.ljust(branch_width), 'dim')}  "
                   f"{version.ljust(version_width)}  {paint(word, style)}")
        indent = rest
        for line in detail_lines(node, show_urls, verbose):
            wrapped = textwrap.wrap(line, max(width - len(indent), 30), subsequent_indent="  ",
                                    break_on_hyphens=False, break_long_words=False) or [""]
            out.extend(paint(indent + piece, "dim") if piece.startswith("http")
                       else indent + piece for piece in wrapped)
    return "\n".join(out)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="mfn-cascade",
        description="Trace a Foundation library release down its GitLab build tree.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  mfn-cascade toolchain 1.46.1\n"
               "  mfn-cascade tcu@1.48.1\n"
               "  mfn-cascade toolchain 1.46       # newest 1.46.x build\n"
               "  mfn-cascade toolchain latest     # newest build on master\n"
               "  mfn-cascade -b master mfn-lib-core 3.21.23\n",
    )
    parser.add_argument("project", help="project name: toolchain, tcu, mfn-lib-core, or group/path")
    parser.add_argument("version", nargs="?",
                        help="version that carries the change: X.Y.Z exactly, X.Y for the newest "
                             "X.Y build, or latest for the newest build on master")
    parser.add_argument("-b", "--branch",
                        help="branch that built VERSION (default: release-X.Y, then master; "
                             "master for latest)")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"GitLab host (default: {DEFAULT_HOST})")
    parser.add_argument("--group", default=DEFAULT_GROUP,
                        help=f"group for bare project names (default: {DEFAULT_GROUP})")
    parser.add_argument("--no-urls", action="store_true",
                        help="omit pipeline URLs for builds that have not succeeded")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also show each version's upstream requirement and clean builds' jobs")
    parser.add_argument("--no-color", action="store_true", help="disable color")
    parser.add_argument("-V", "--version-info", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.version is None:
        if "@" not in args.project:
            parser.error("give a VERSION, or write PROJECT@VERSION")
        args.project, args.version = args.project.rsplit("@", 1)
    if args.version != "latest" and not re.fullmatch(r"\d+(\.\d+)+", args.version):
        parser.error(f"not a version: {args.version}")
    return args


def main(argv=None):
    args = parse_args(argv)
    gitlab = GitLab(args.host)
    project = resolve_project(args.project, args.group)
    try:
        branch, version, commit = locate_start(gitlab, project, args.version, args.branch)
        if commit is None:
            if args.branch or args.version == "latest":
                where = args.branch or "master"
            else:
                where = f"release-{'.'.join(args.version.split('.')[:2])} or master"
            if args.version == "latest":
                wanted = "version"
            elif args.version.count(".") >= 2:
                wanted = f"version {args.version}"
            else:
                wanted = f"{args.version}.x version"
            print(f"mfn-cascade: found no {wanted} of {short_name(project)} on {where}",
                  file=sys.stderr)
            return 1
        root = Node(project, branch)
        root.state = FIXED
        root.version = version
        root.package = gitlab.mmpackage(project, commit["id"])["name"]
        root.pipeline = gitlab.pipeline_summary(project, commit["id"])
        build_tree(gitlab, root, commit["committed_date"])
    except GlabError as err:
        print(f"mfn-cascade: {err}", file=sys.stderr)
        return 1
    color = not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    width = shutil.get_terminal_size((100, 24)).columns
    print(render(root, Painter(color), not args.no_urls, args.verbose, width))
    return 0


if __name__ == "__main__":
    sys.exit(main())
