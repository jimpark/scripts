#!/usr/bin/env python3
"""Tests for the glob/regex query handling in git-open.

git-open's query box speaks regex, except when what you typed reads as a glob
(`*.props`), which it then honours as one. The two languages overlap, so most
of what matters here is the *boundary*: which queries tip into glob, which stay
regex, and that each still matches the paths it should.

Run:  python tests/test_glob.py   (from the repo root, or from inside tests/)
"""
import importlib.util
import os
import sys

# The scripts under test live one directory up (this file is in tests/). The
# hyphen in "git-open.py" keeps it out of reach of a plain import, so load it
# by path.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_git_open():
    spec = importlib.util.spec_from_file_location(
        "git_open", os.path.join(ROOT, "git-open.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


git_open = load_git_open()

# A stand-in repo: enough shape to catch a glob that reaches too far (deep.props
# under a nested folder), one that starts mid-name (mytest_x.py), and one that
# matches a folder rather than a file name (docs/props.md).
PATHS = [
    "Directory.Build.props",
    "a/build/y.props",
    "build/gen/deep.props",
    "build/x.props",
    "docs/props.md",
    "src/app/main.c",
    "src/mytest_x.py",
    "tests/test_splice.py",
]


def matches(query):
    """The paths a query selects, through the same compile git-open uses."""
    finder = git_open.FileFinder.__new__(git_open.FileFinder)
    finder.query = query
    finder._compile_filter()
    if finder.filt is None:
        return list(PATHS)
    return [p for p in PATHS if finder.filt.search(p)]


def check(query, expected, kind):
    """Assert a query picks `expected` and was read as regex/glob/literal."""
    finder = git_open.FileFinder.__new__(git_open.FileFinder)
    finder.query = query
    finder._compile_filter()
    actual_kind = ("glob" if finder.is_glob else
                   "literal" if finder.bad_regex else "regex")
    assert actual_kind == kind, \
        "{0!r}: read as {1}, expected {2}".format(query, actual_kind, kind)
    got = matches(query)
    assert got == expected, \
        "{0!r}: matched {1}, expected {2}".format(query, got, expected)


def test_glob_queries():
    # The case that used to match nothing at all: a leading * is not a regex.
    check("*.props", ["Directory.Build.props", "a/build/y.props",
                      "build/gen/deep.props", "build/x.props"], "glob")
    # Valid as a regex, but only ever meant as a glob -- the reason the choice
    # keys off the query's syntax rather than off whether it compiles.
    check("build/*.props", ["a/build/y.props", "build/gen/deep.props",
                            "build/x.props"], "glob")
    # A glob is anchored to a folder boundary, so it can't start mid-name.
    check("test_*.py", ["tests/test_splice.py"], "glob")
    # ...and anchored at the tail, so it can't stop mid-name.
    check("*.prop", [], "glob")
    check("src/*.c", ["src/app/main.c"], "glob")   # * spans /
    check("?irectory.Build.props", ["Directory.Build.props"], "glob")
    check("*.[ch]", ["src/app/main.c"], "glob")
    check("*", list(PATHS), "glob")


def test_regex_queries():
    # Regex-only syntax keeps a query in regex, even with a * in it.
    check(".*\\.props$", ["Directory.Build.props", "a/build/y.props",
                          "build/gen/deep.props", "build/x.props"], "regex")
    check("^src/", ["src/app/main.c", "src/mytest_x.py"], "regex")
    check("(props|targets)$", ["Directory.Build.props", "a/build/y.props",
                               "build/gen/deep.props", "build/x.props"],
          "regex")
    # No glob syntax, no regex-only syntax: a plain regex, as before.
    check("props", ["Directory.Build.props", "a/build/y.props",
                    "build/gen/deep.props", "build/x.props", "docs/props.md"],
          "regex")
    check("", list(PATHS), "regex")     # empty: no filter at all


def test_literal_fallback():
    # Broken regex, and not glob-shaped either: match it literally, as before.
    check("main(c", [], "literal")
    check("a{2,1}b", [], "literal")


def test_glob_never_raises():
    # Whatever half-finished thing is in the box between keystrokes has to
    # compile -- the list filters live, with no chance to be "done" typing.
    for query in ["*", "[", "*.[", "[]", "**", "?", "[a-", "*.{", "["]:
        finder = git_open.FileFinder.__new__(git_open.FileFinder)
        finder.query = query
        finder._compile_filter()        # raising here is the failure
        assert finder.filt is not None, "{0!r}: no filter".format(query)


def main():
    test_glob_queries()
    test_regex_queries()
    test_literal_fallback()
    test_glob_never_raises()
    print("OK")


if __name__ == "__main__":
    main()
