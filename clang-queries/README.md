# clang-queries

The query library for [`clang-query-run.py`](../README.md#clang-query-runpy).
Every `*.query` file here is a [`clang-query`][cq] script the runner can execute
across a whole `compile_commands.json`. Drop a new `.query` file in this folder
and it shows up automatically — in the menu (run `clang-query-run` with no
arguments) and in `clang-query-run --list`.

[cq]: https://clang.llvm.org/docs/LibASTMatchersReference.html

## Add a query in three steps

1. **Create `your-name.query`** with a header and a matcher:

   ```
   # title: throw-expression audit
   # description: every throw site in the project
   set output diag
   match cxxThrowExpr().bind("throw")
   ```

2. **Run it:** `clang-query-run -f clang-queries/your-name.query`
   (or just `clang-query-run` and pick it from the menu).

3. **(Optional) add a rubric** — see [Reports](#reports) below — to enable
   `--report` for an AI investigation packet.

That's it. No registration step; the runner globs this directory.

## The header

The first comment block may carry metadata the menu and `--list` display:

```
# title: <short name shown in the menu>
# description: <one line — keep it to a single line; continuation lines are ignored>
```

Both are optional. `title` defaults to the filename stem; `description` defaults
to empty. The header ends at the first non-comment, non-blank line, so put it at
the very top. Ordinary `#` comments (no `key:`) are fine anywhere.

## Output modes — pick based on what you want back

The runner adapts to the query's `set output` mode:

- **`set output diag`** (or no `set output`) → **locations.** Each match must
  **`.bind("name")`** a node; the bound node's `file:line:col` is what gets
  reported. These queries get dedup, source context, `--json`, and `--report`.
  This is what you want for an audit. (Without a bind — and with
  `set bind-root false` — nothing prints.)

- **`set output dump` / `print` / `detailed-ast`** → **raw AST text.** The
  runner streams `clang-query`'s output per translation unit (still parallel,
  still flagging parse failures) but can't aggregate it, so `--json` / `--report`
  are rejected for these. Use them to *explore* the AST while writing a matcher.

A handy pattern: prototype with `set output dump` to see node shapes, then
switch to `set output diag` + `.bind(...)` once the matcher is right.

## Reports

`--report` builds a Markdown packet: a rubric (the task framing for an AI)
followed by every match with source context. The rubric comes from a sidecar
named after the query:

```
your-name.query        # the matcher
your-name.rubric.md    # optional — its full text is prepended to --report
```

If the sidecar is absent, `--report` falls back to a generic "analyse these
findings" header. See [`path-string.rubric.md`](path-string.rubric.md) for a
worked example (it buckets each finding and states a per-finding output schema).

## Writing type-aware matchers

The whole reason to use `clang-query` over grep is that it sees *types* and
*overloads*. [`path-string.query`](path-string.query) is a good template:

- It resolves a class across standard-library spellings — libc++
  `std::__1::filesystem::path`, the older `std::__fs::filesystem::path`, and
  libstdc++ — via `matchesName("(^|::)(__[a-z0-9]+::)?filesystem::path$")`.
- It binds the `memberExpr` (not the call) so the reported location points at
  the method-name token.
- It uses `unless(isExpansionInSystemHeader())` to drop the standard library's
  own internal calls — almost always what you want.

The full matcher vocabulary is in the [AST Matcher Reference][cq]. To experiment
interactively, run `clang-query -p <build-dir> <source.cpp>` and type `match …`
at the prompt before saving it here.

## macOS note

On macOS, `clang-query` must parse with headers matching the compiler in the
compile DB, or every TU fails with `fatal error: 'filesystem' file not found`
(silent *missed* findings). The runner auto-injects the macOS SDK
`-isysroot`/`-resource-dir` to fix the common case; this is a runner/toolchain
concern, not a per-query one — see the **macOS gotcha** in the
[main README](../README.md#clang-query-runpy) for the details and the
`--no-auto-sdk` escape hatch.
