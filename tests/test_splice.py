#!/usr/bin/env python3
"""Equivalence test for the incremental splice helpers in branch_tui.

The whole point of splice_expand/splice_collapse is to leave `rows` identical to
what a full build_visible() would produce for the same `expanded` set -- just
without rebuilding every row. This drives random expand/collapse sequences and
asserts, after every step, that the incrementally maintained rows match a fresh
full build field-for-field (type/depth/node/expanded/number/id/branch).

Run:  python tests/test_splice.py   (from the repo root, or from inside tests/)
"""
import os
import random
import sys

# The scripts under test live one directory up (this file is in tests/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from branch_tui import (Branch, Row, build_tree, build_visible, splice_collapse,
                        splice_expand)


def make_branches(n_dirs, n_sub, n_leaf):
    """A tree with two folder levels and leaves, e.g. dir3/sub2/feature_7."""
    names = []
    for d in range(n_dirs):
        for s in range(n_sub):
            for l in range(n_leaf):
                names.append("dir{0}/sub{1}/feature_{2}".format(d, s, l))
    # a few top-level leaves too, to mix depths
    for l in range(n_leaf):
        names.append("root_leaf_{0}".format(l))
    return [Branch(nm, "local", nm, False, nm) for nm in names]


def rows_equal(a, b):
    if len(a) != len(b):
        return False, "len {0} != {1}".format(len(a), len(b))
    for i, (x, y) in enumerate(zip(a, b)):
        for s in Row.__slots__:
            if getattr(x, s) != getattr(y, s):
                return False, "row {0} field {1}: {2!r} != {3!r}".format(
                    i, s, getattr(x, s), getattr(y, s))
    return True, ""


def folder_rows(rows):
    return [i for i, r in enumerate(rows) if r.type == "folder"]


def check_numbers(rows):
    """Branch numbers must be a gap-free 1..N in visible order; folders None."""
    n = 0
    for r in rows:
        if r.type == "folder":
            assert r.number is None, "folder has a number"
        else:
            n += 1
            assert r.number == n, "branch number {0} != {1}".format(r.number, n)


def driver_test(seed):
    rnd = random.Random(seed)
    branches = make_branches(4, 3, 5)
    root = build_tree(branches)

    # Start from the all-expanded state (every folder open), the default these
    # tools open with, and maintain rows incrementally against `expanded`.
    all_folders = {r.node.path for r in build_visible(root, set(), None)
                   if r.type == "folder"}
    # discover every folder path by expanding fully once
    expanded = set()
    prev = -1
    while len(expanded) != prev:
        prev = len(expanded)
        for r in build_visible(root, expanded, None):
            if r.type == "folder":
                expanded.add(r.node.path)
    all_folders = set(expanded)

    rows = build_visible(root, expanded, None)

    for step in range(400):
        frows = folder_rows(rows)
        idx = rnd.choice(frows)
        folder = rows[idx]
        path = folder.node.path
        if folder.expanded:
            expanded.discard(path)
            splice_collapse(rows, idx)
        else:
            expanded.add(path)
            splice_expand(rows, idx, expanded)

        ok, why = rows_equal(rows, build_visible(root, expanded, None))
        assert ok, "seed {0} step {1}: {2}".format(seed, step, why)
        check_numbers(rows)

    return len(all_folders)


def test_build_visible_shapes():
    branches = make_branches(2, 2, 2)
    root = build_tree(branches)
    rows = build_visible(root, set(), None)
    assert all(isinstance(r, Row) for r in rows)
    # item access still works
    assert rows[0]["type"] in ("folder", "branch")
    # filter path returns Row too, everything forced open
    import re
    frows = build_visible(root, set(), re.compile("feature_1"))
    assert all(isinstance(r, Row) for r in frows)
    assert all(r.expanded for r in frows if r.type == "folder")


if __name__ == "__main__":
    test_build_visible_shapes()
    for seed in range(25):
        nf = driver_test(seed)
    print("OK: 25 random expand/collapse sequences x 400 steps match full "
          "build_visible; numbering gap-free; folders discovered = {0}".format(nf))
