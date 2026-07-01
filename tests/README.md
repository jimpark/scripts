# tests

Test scripts for the utilities in the parent directory. They live here, rather
than alongside the scripts, so they stay out of the way of the tools you run
directly from `PATH`.

Each test adds the repo root to `sys.path` itself, so you can run them from
anywhere with plain `python`:

```sh
python tests/test_splice.py
```

They use only the standard library (no pytest required) and print an `OK` line
on success, or raise `AssertionError` on failure.

## Tests

- **test_splice.py** — Verifies the incremental row splicing in `branch_tui`
  (`splice_expand` / `splice_collapse`), which the interactive tools use to
  expand and collapse folders without rebuilding the whole row list. Drives many
  random expand/collapse sequences and asserts the incrementally maintained rows
  stay byte-identical (type/depth/node/expanded/number/id/branch) to a full
  `build_visible`, with gap-free branch numbering.
