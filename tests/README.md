# tests

Test scripts for the utilities in the parent directory. They live here, rather
than alongside the scripts, so they stay out of the way of the tools you run
directly from `PATH`.

Each test adds the repo root to `sys.path` itself, so you can run them from
anywhere with plain `python`:

```sh
python tests/test_splice.py
python tests/test_glob.py
```

They use only the standard library (no pytest required) and print an `OK` line
on success, or raise `AssertionError` on failure. One exception:
`test_script_runs` needs the `regex` module that `script-runs` itself depends
on, so run it with `uv run tests/test_script_runs.py` (the inline dependency is
installed for you).

## Tests

- **test_splice.py** — Verifies the incremental row splicing in `branch_tui`
  (`splice_expand` / `splice_collapse`), which the interactive tools use to
  expand and collapse folders without rebuilding the whole row list. Drives many
  random expand/collapse sequences and asserts the incrementally maintained rows
  stay byte-identical (type/depth/node/expanded/number/id/branch) to a full
  `build_visible`, with gap-free branch numbering.
- **test_glob.py** — Covers how `git-open` reads its query: as a glob when it's
  glob-shaped (`*.props`, `build/*.props`), as a regex otherwise (`.*\.props$`),
  and literally when it's neither and won't compile. Checks the paths each
  selects — including a glob's anchoring at both ends — and that every
  half-typed query still compiles.
- **test_script_runs.py** — Conformance suite for `script-runs` against the v2.0
  companion fixture (`docs/script-run-extraction-tests.json`): all 29 Latin
  backward-compatibility cases plus the 11 generalization cases, each under its
  own target script (Greek, Cyrillic, Arabic, Hebrew, Latin). Runs every case
  under the default policy and each declared policy-sensitivity variant under its
  non-default knob, comparing extracted run substrings exactly (code points, not
  glyphs). Also verifies each case's `input_codepoints` redundancy list — which
  catches a re-escaped fixture or a cross-script confusable (Greek `Α` U+0391 vs
  Latin `A` U+0041) substituted by copy-paste — that every emitted `(start, end)`
  offset slices back to its run, and that an invalid `--script` is rejected.
