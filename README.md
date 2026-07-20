# scripts

A small collection of standalone utility scripts. Each is self-contained — grab
the one you need and run it. (A few exceptions ship with a companion file: keep
it beside the script. [`git-switch.py`](#git-switchpy) and
[`delete-branch.py`](#delete-branchpy) share their TUI engine through a
neighbouring `branch_tui.py` module; [`git-open.py`](#git-openpy),
[`git-grep.py`](#git-greppy), and [`git-diff.py`](#git-diffpy) reuse that same
engine and share their editor handling through neighbouring `editor_config.py`
and `editor_ide.py` modules; and [`clang-query-run.py`](#clang-query-runpy)
loads its AST matchers from a neighbouring `clang-queries/` directory.) Details
for each are below.

| Script | What it does |
| ------ | ------------ |
| [`backport.py`](#backportpy) | Cherry-pick one author's commits from a source branch onto a target branch. |
| [`baseconv.py`](#baseconvpy) | Convert a value between binary, decimal, octal, hex, and base64. |
| [`bedrock-copilot.py`](#bedrock-copilotpy) | Launch the GitHub Copilot CLI against a model on AWS Bedrock, with model + effort pickers. |
| [`clang-query-run.py`](#clang-query-runpy) | Run any `clang-query` AST matcher across a whole compile DB, in parallel, and emit an AI-ready investigation packet. |
| [`claude-user.py`](#claude-userpy) | Launch `claude` as a named account — keep a personal and an enterprise login side by side. |
| [`configure-vscode-bedrock.py`](#configure-vscode-bedrockpy) | Point the Claude Code VS Code extension at AWS Bedrock, safely. |
| [`cpp-unicode-escapes.py`](#cpp-unicode-escapespy) | Rewrite misused `\xNNNN` escapes as proper `\uNNNN` in C++ string/char literals. |
| [`delete-branch.py`](#delete-branchpy) | Interactively check off Git branches (local and remote) — even whole folders — and delete them. |
| [`docx-runs.py`](#docx-runspy) | Resolve and report the language of every text run in a `.docx`, with per-character script classification. |
| [`git-batch.py`](#git-batchpy) | Run one git command across every git repo in the current directory and collate the results. |
| [`git-diff.py`](#git-diffpy) | Interactively browse `git diff` in a folder tree, search the changes, and open a changed line at its spot. |
| [`git-grep.py`](#git-greppy) | Interactively `git grep`, browse the hits in a folder tree, and open one at its line in your editor. |
| [`git-open.py`](#git-openpy) | Interactively find a tracked file by regex or glob in a folder tree and open it in your editor. |
| [`git-prune.py`](#git-prunepy) | Delete local Git branches that no longer exist on a remote. |
| [`git-switch.py`](#git-switchpy) | Interactive, vim-style Git branch switcher with a collapsible folder tree and remote branches. |
| [`html-info.py`](#html-infopy) | Print useful basic information about an HTML, XML, or XHTML document. |
| [`latin-runs.py`](#latin-runspy) | Extract embedded Latin-script runs (with their neutral glue) from mixed-script Unicode text. |
| [`rapid-mlx-copilot.py`](#rapid-mlx-copilotpy) | Pick a local MLX model your Mac can run and launch the GitHub Copilot CLI against it. |
| [`rtf-runs.py`](#rtf-runspy) | Segment RTF body text into runs and report the language/character set of each. |
| [`unicode-clipboard.py`](#unicode-clipboardpy) | Copy Unicode characters to the clipboard by codepoint, so you can paste the untypeable. |
| [`unicode-info.py`](#unicode-infopy) | Fetch and display Unicode character information for a codepoint. |
| [`update-scripts.py`](#update-scriptspy) | Update these scripts in place by fast-forwarding the checkout they live in. |

**macOS:** Each script has a matching bash wrapper with the same base name (e.g. `backport`, `baseconv`). Add the `scripts/` folder to your `PATH` and invoke any script by its bare name — no `python` prefix, no directory qualifier needed.

---

## `baseconv.py`

Convert a value between **binary**, **decimal**, **octal**, **hex**, and
**base64** — any format to any other.

Every value is modeled as an underlying sequence of **raw bytes**; each format
is just one way of encoding those bytes. A conversion therefore always means:
decode the input (`--from`) into bytes → re-encode as the output (`--to`).

| Format   | Meaning                                              | Example (`hi`)     |
| -------- | ---------------------------------------------------- | ------------------ |
| `bin`    | Bits, padded to whole bytes (8 bits each)            | `0110100001101001` |
| `dec`    | Base-10 of the big-endian integer value of the bytes | `26729`            |
| `oct`    | Octal of the big-endian integer value of the bytes   | `64151`            |
| `hex`    | Two hex digits per byte, zero-padded to even length  | `6869`             |
| `base64` | Standard RFC 4648 base64                             | `aGk=`             |

### Usage

```sh
baseconv --from <FORMAT> --to <FORMAT> [VALUE]
```

or invoke the script directly:

```
python baseconv.py --from <FORMAT> --to <FORMAT> [VALUE]
```

- `VALUE` may be passed as an argument, or **omitted to read from stdin**.
- Output is written to **stdout**, followed by a newline.
- Surrounding whitespace and `0b` / `0x` prefixes on the input are ignored.
- Run `python baseconv.py --help` for the full reference.

```sh
# hex -> base64
python baseconv.py --from hex --to base64 48656c6c6f      # -> SGVsbG8=

# base64 -> hex, reading from stdin
echo SGVsbG8= | python baseconv.py --from base64 --to hex # -> 48656c6c6f

# decimal -> binary
python baseconv.py --from dec --to bin 65535              # -> 1111111111111111

# hex -> octal
python baseconv.py --from hex --to oct ff                 # -> 377
```

### Notes & caveats

- **`dec` and `oct` go through the integer value** of the bytes, so they cannot
  preserve leading zero-bytes (`0x00ff` and `0xff` both read back as `255`).
  `bin`, `hex`, and `base64` are byte-exact and round-trip losslessly.
- **Negative numbers** have no byte representation and are rejected.

Exit status: `0` success · `1` invalid input for the chosen `--from` format ·
`2` usage error (bad or missing arguments).

**Requirements:** Python 3.6+ (standard library only; no dependencies).

---

## `configure-vscode-bedrock.py`

Configures the **Claude Code VS Code extension** to use models on **AWS
Bedrock** by writing the right keys into your VS Code `settings.json`.

Before touching anything it checks the AWS CLI, verifies your AWS profile with
`sts get-caller-identity`, and runs a live `bedrock-runtime invoke-model` call
against each model you've chosen — so a bad profile, an incomplete Anthropic
**First Time Use (FTU)** form, or a typo'd inference-profile ID is caught up
front rather than the next time you open the editor.

The settings file is written **safely**: the existing file is copied to a
`settings.json.bak` sibling, then the new content is written to a temp file in
the same directory and **atomically renamed** into place. If anything fails the
original is left untouched, and you can restore from the backup.

### Usage

```sh
configure-vscode-bedrock
```

or invoke the script directly:

```sh
python configure-vscode-bedrock.py
```

Any value you don't pass as a flag is **prompted for** (with a default), unless
you pass `--non-interactive`, in which case the default is used.

```sh
# fully interactive
python configure-vscode-bedrock.py

# scripted, picking Opus as the default model
python configure-vscode-bedrock.py --profile dev --region us-east-1 --default-model opus

# preview the exact settings without writing or calling AWS
python configure-vscode-bedrock.py --non-interactive --skip-validation --dry-run
```

| Option | Effect |
| ------ | ------ |
| `--profile <name>` | AWS profile to use (default: prompt, then `default`). |
| `--region <region>` | AWS region (default: `us-east-1`). |
| `--sonnet` / `--opus` / `--haiku` `<id>` | Inference-profile ID for each model. |
| `--default-model {sonnet,opus,haiku}` | Which model the extension selects by default. |
| `--settings-path <path>` | Override the target `settings.json` (handy for testing). |
| `--skip-validation` | Skip all live AWS calls (profile + Bedrock checks). |
| `--non-interactive` | Never prompt; use flags and defaults only. |
| `--dry-run` | Print what would change; write nothing. |

Run `python configure-vscode-bedrock.py --help` for the full reference.

The two keys written are `claudeCode.environmentVariables` (an array of
`{name, value}` env-var objects, including `CLAUDE_CODE_USE_BEDROCK=1`) and
`claudeCode.selectedModel`. **Reload the VS Code window afterwards** for the
changes to take effect.

### Notes & caveats

- **The model IDs are examples and go stale.** List what's actually available
  to you with
  `aws bedrock list-inference-profiles --region <REGION> --profile <PROFILE>`,
  and pass the right ones via `--sonnet` / `--opus` / `--haiku`.
- **`settings.json` must be plain JSON.** VS Code allows `//` comments, but this
  script can't preserve them — if it can't parse the file it refuses to write
  and prints the keys for you to add manually, leaving the file untouched.
- A backup is written to `settings.json.bak` on every run (overwriting any
  previous backup). To roll back, copy it over `settings.json`.
- The live test needs **AWS CLI v2** (it uses `--cli-binary-format`); a warning
  is printed if a different version is detected.
- **Requirements:** Python 3.6+ (standard library only) and the AWS CLI on
  `PATH`.

---

## `html-info.py`

Prints useful basic information about an **HTML**, **XML**, or **XHTML**
document by inspecting its opening declarations, root element, and common
metadata in the `<head>`.

It reports things that are usually quick to spot near the top of the file, such
as the **doctype**, **XML declaration**, **root tag**, **language**,
**text direction**, **declared / inferred encoding**, **title**, and common
`<meta>` values like **description**, **author**, **viewport**, and
**generator**. It also reports the canonical URL, namespaces, a **SHA-256**
checksum of the raw input bytes, whether `<head>` and `<body>` were found, and
a few simple tag counts.

### Usage

```sh
html-info [--format {human,json}] [FILE]
```

or invoke the script directly:

```sh
python html-info.py [--format {human,json}] [FILE]
```

- Pass `FILE` to inspect a file on disk, or omit it to **read bytes from stdin**.
- `--format human` prints readable key/value lines.
- `--format json` prints structured JSON for scripting.
- Run `python html-info.py --help` for the full reference.

```sh
# inspect a normal HTML file
python html-info.py index.html

# machine-readable output
python html-info.py --format json page.xhtml

# inspect XML from stdin
cat feed.xml | python html-info.py
```

### Notes & caveats

- The encoding is **sniffed** from a BOM, XML declaration, or `<meta charset>`
  / Content-Type meta tag; if none is present, UTF-8 is assumed.
- The **mobile-friendly hint** is just that: a hint. It looks for a viewport
  meta tag such as `width=device-width` or `initial-scale=...`; it is not a full
  responsive-design audit.
- HTML is parsed leniently enough for common real-world files, while XML/XHTML
  declarations and namespaces are still surfaced when present.
- **Requirements:** Python 3.6+ (standard library only; no dependencies).

---

## `git-batch.py`

Runs **one git command across every git repo** in the current directory and
collates the results — the tool for a `~/projects`-style folder full of clones
when you want to `fetch` them all, see which have uncommitted work, or check
what branch each one is on.

It scans the **immediate subdirectories** (no recursion) for anything with a
`.git` entry — normal clones, worktree checkouts, and submodule checkouts all
count — runs the command in each, and prints one section per repo (stdout and
stderr merged) followed by a summary of successes and failures. Successful
repos are reported first and **failed repos last** (each group in sorted name
order), so the failures sit right above the summary instead of scrolling
away. **Symlinked directories are followed**, deduped by resolved path so
a repo reachable under two names only runs once (the real directory's name
wins over a symlink's).

Repos are processed **in parallel** by default, which is what makes network
commands like `fetch`/`pull` across a dozen repos fast; the report is
deterministic regardless. While they run, a **live progress counter** naming
the repos still in flight (`[78/80] running: linux, llvm-project`) ticks on
stderr, so a long pull/rebase pass never looks hung — and if one repo is
slow, it's the one on screen. Failures are echoed the moment they happen.
The counter only appears when stderr is a terminal, so redirected runs stay
clean.

### Usage

Run it from the directory that *contains* your repos:

```sh
git-batch <git command and args…>
```

or invoke the script directly:

```sh
python git-batch.py <git command and args…>
```

Everything after the options is passed to git **verbatim**, so any git command
works:

```sh
git-batch status -sb            # what's dirty, and what branch is each on?
git-batch fetch --prune         # update all remotes, in parallel
git-batch -j 1 pull --ff-only   # pull each repo, one at a time
git-batch -q fetch              # only show repos where the fetch failed
git-batch log -1 --oneline      # last commit in each repo
```

| Option | Effect |
| ------ | ------ |
| `-C <dir>` | Scan this directory instead of the current one. |
| `-j`, `--jobs <N>` | Repos to process in parallel (default: scales with CPU count; `1` disables parallelism). |
| `-q`, `--quiet` | Only print sections for repos where the command failed; successes fold into the summary. |
| `--timeout <seconds>` | Kill a repo's command (and everything it spawned) after this long; the repo reports as *timed out*. Default: no limit. |

Run `python git-batch.py --help` for the full reference.

### Notes & caveats

- **There is no confirmation and no safety net.** The command is passed through
  as-is, so `git-batch reset --hard` really will reset every repo. It's a
  power tool; point it carefully.
- If the git args themselves start with a dash before any subcommand, put `--`
  first so the script's own option parser doesn't claim them
  (e.g. `git-batch -- -c core.pager=cat log -1`).
- **Interactive commands aren't supported — they fail fast, never hang.** A
  batch run can't answer a prompt, and with output captured a prompt would be
  an invisible hang (git asks on `/dev/tty`, which you'd never see). So each
  repo's git runs with stdin closed, `GIT_TERMINAL_PROMPT=0` (credential
  prompts become errors), `GIT_EDITOR=false` (an attempted editor launch
  fails — pass `--no-edit` or `-m`), and ssh in `BatchMode` (skipped if you
  set your own `GIT_SSH_COMMAND`/`GIT_SSH`). Agent-loaded keys, passwordless
  keys, and credential helpers still work — only *prompting you* is disabled,
  and the failing repo shows up immediately in the progress line and the
  report.
- **Stalled network connections can't hang the run.** The injected ssh command
  also enables keepalives (`ConnectTimeout=15`, `ServerAliveInterval=15
  ServerAliveCountMax=4`), so a connection that goes silent — seen in the wild
  against `ssh.dev.azure.com` — aborts after ~60 seconds instead of waiting
  forever (ssh's default). For hangs from any other source (hooks, HTTPS
  stalls, credential helpers), `--timeout <seconds>` is a hard per-repo cap
  that kills the repo's whole process tree and reports it as timed out.
- The exit status is aggregate: `0` only if the command succeeded in **every**
  repo, `1` if any failed (the summary lists which).

Exit status: `0` the command succeeded in every repo · `1` no repos found, or
the command failed in at least one repo · `2` usage error (bad or missing
arguments).

**Requirements:** Python 3.6+ (standard library only; no dependencies) and Git
on `PATH`.

---

## `git-prune.py`

Deletes local Git branches that no longer exist on a remote — handy after remote
branches have been merged and deleted (e.g. squash-merged PRs), leaving stale
local copies behind.

It runs `git fetch --prune`, finds local branches that no longer have a
counterpart on the remote, and — for each one whose work is **already merged
into the target branch** — deletes it **after you confirm** with `y`. Branches
that aren't merged yet are listed and kept. The branch you currently have
checked out is always skipped.

Crucially, "merged" includes **squash- and rebase-merged** branches, not just
classic merge commits (see [How "merged" is decided](#how-merged-is-decided)).

### Usage

Run it from inside the repository you want to clean up:

```sh
git-prune
```

or invoke the script directly:

```sh
python git-prune.py
```

You'll see which branches are being kept (unmerged), which will be deleted, and
a `y/n` prompt; anything other than `y` cancels without changing anything.

| Option | Effect |
| ------ | ------ |
| `--remote <name>` | Compare against this remote instead of `origin`. |
| `--into <name>` | Test "is it merged?" against this branch instead of the current one. |
| `--force` | Delete **every** branch with no remote, merged or not (uses `git branch -D`). |
| `--yes` | Skip the confirmation prompt (non-interactive; use with care). |

```sh
python git-prune.py --into main
python git-prune.py --remote upstream
python git-prune.py --force --yes
```

Run `python git-prune.py --help` for the full reference.

### How "merged" is decided

A candidate branch is considered merged into the target (the current branch, or
`--into <name>`) if **either**:

1. its tip is an ancestor of the target — a normal or fast-forward merge; **or**
2. `git cherry` finds an equivalent patch already in the target — i.e. the
   branch's combined diff was applied under a different commit, which is what
   **squash and rebase merges** produce.

This matters because `git branch -d` only recognizes case (1), so it reports
squash/rebase-merged branches as *"not fully merged"* even though their changes
are in `main`. This script handles case (2) as well, so those branches are
correctly deleted without needing `--force`.

### Notes & caveats

- Genuinely unmerged branches are **kept** and listed. Pass `--force` to delete
  every no-remote branch regardless (this discards unmerged commits, so review
  the printed list first).
- The merge check runs against the **current branch** unless you pass `--into`.
  If you're not on your integration branch (e.g. `main`/`master`), use `--into`
  so branches aren't wrongly judged unmerged.
- The check compares against the **local** target branch, which `git fetch`
  does **not** fast-forward. If the target is behind its upstream the result
  would be stale, so (unless `--force`) the script **stops with an error and
  instructions** rather than risk keeping already-merged branches:

  ```
  Error: 'main' is 3 commit(s) behind 'origin/main', so the merge check would be
  stale and might keep branches that are in fact merged.
  Do one of:
    * compare against the up-to-date remote branch:  --into origin/main
    * update 'main' first (e.g. 'git pull --ff-only' while it is checked out)
    * re-run with --force to skip this check
  ```
- "Exists on remote" is matched by branch name against `<remote>/*` (the
  `<remote>/HEAD` symref is ignored), so a local branch is kept as long as a
  same-named branch exists on the remote.
- If a deletion fails, the remaining branches are still processed and the script
  exits with a non-zero status.
- **Requirements:** Python 3.6+ (standard library only) and Git on `PATH`.

---

## `backport.py`

Cherry-picks a single author's commits from a **source** branch onto a
**target** branch. It's the tool you reach for when one person's work landed on
a feature branch and you need just *their* commits replayed onto a release or
maintenance branch — without dragging along everyone else's.

It discovers the author's commits between the merge-base and the source branch,
lets you trim the list in your editor (like `git rebase -i`), then cherry-picks
them oldest-first. Conflicts pause the run so you can resolve and resume.

### Usage

```sh
backport <source_branch> <target_branch> [options]
```

or invoke the script directly:

```sh
python backport.py <source_branch> <target_branch> [options]
```

By default it backports **your own** commits (from `git config user.email`, or
`user.name`) onto your current branch, which must already contain
`<target_branch>`. Use `--create-branch` to start from a fresh branch instead.

```sh
# your commits from feature/new-ui onto main (current branch must contain main)
python backport.py feature/new-ui main

# someone else's commits, onto a brand-new branch cut from main
python backport.py feature/new-ui main --user jane@example.com \
    --create-branch backport/jane-ui

# see what would happen, change nothing
python backport.py feature/new-ui main --dry-run

# skip the editor, sign off each commit, prefer their side on conflicts
python backport.py feature/new-ui main --no-edit -s -X theirs
```

When the editor opens, **delete the lines** for any commits you don't want.
Reordering is not supported. Save and close to begin cherry-picking.

| Option | Effect |
| ------ | ------ |
| `--user <name_or_email>` | Filter by this author instead of your git user. |
| `--create-branch <name>` | Create and check out `<name>` from `<target_branch>` first. |
| `--base <ref>` | Override the auto-detected merge-base used to find commits (see the caveat on reverted / `-s ours` merges below). |
| `--dry-run` | Print the commits that would be backported, then exit. |
| `--no-edit` | Skip the interactive editor; pick every discovered commit. |
| `-s`, `--signoff` | Pass `-s` to `git cherry-pick` (adds a `Signed-off-by` trailer). |
| `-X <strategy>` | Pass `-X <strategy>` to `git cherry-pick` (e.g. `theirs`, `ours`). |

### Pausing, resuming, aborting

If a cherry-pick conflicts, the run stops and tells you what to do. Resolve the
conflict (`git add` the fixed files), then:

```sh
python backport.py --continue   # finish the conflicted commit and carry on
python backport.py --abort      # rewind everything to where you started
```

`--continue` will commit the resolution for you (preserving `-s` if you used it),
or pick up cleanly if you committed it yourself. `--abort` runs
`git cherry-pick --abort` if needed and `git reset --hard` back to the commit you
started from.

### How it works

- Commits by the author are found between the merge-base and `<source_branch>`,
  oldest first, so they apply in their original chronological order.
- **Already-backported commits are skipped automatically.** Cherry-picking
  creates a *new* commit with a new SHA, so a naive SHA range would re-propose
  everything on the next run. Instead, `git cherry` is used to compare by
  **patch-id** (a hash of the diff), so commits whose changes already exist on
  the target — even under a different SHA from an earlier backport — are
  excluded up front and reported (`Excluded N commit(s) already present`).
- **An audit trail is recorded.** Each cherry-pick is run with `-x`, appending
  `(cherry picked from commit <sha>)` to the message so you can always trace a
  backported commit to its origin.
- Progress is stored in **`.git/.backport`** — a state file with a metadata
  header (starting commit, branches, author, signoff/strategy flags) and the
  list of commits. It lives in `.git/`, so it never dirties your working tree.
  Completed commits are commented out as the run proceeds, so an interrupted
  backport can resume exactly where it left off (`--continue` reapplies the
  saved `-x`/`-s`/`-X` options, even for a commit you paused on to resolve).

### Notes & caveats

- The working tree must be **clean** before a new run starts (commit or stash
  first). `--dry-run` doesn't require this since it changes nothing.
- **Squash merges are the one case patch-id can't catch.** When a backport PR is
  *squashed* into the target, several commits collapse into one whose diff
  matches no individual source commit — so those commits get proposed again. The
  execution loop still protects you: replaying one produces no changes, so it is
  **auto-skipped** (and reported) rather than committed. To avoid the noise
  entirely, merge or rebase backport PRs (preserving commits) instead of
  squashing, or just delete those lines in the editor.
- A cherry-pick whose changes are **already present** in the target produces an
  empty commit; those are **auto-skipped** rather than recorded.
- **Reverted or `-s ours` merges can hide commits — use `--base` to recover.**
  Discovery starts at the merge-base of source and target. A *merge* from source
  into target moves that merge-base forward, which is normally correct (the
  merged commits really are in the target). But if that merge was later
  **reverted**, or made with **`git merge -s ours`**, the ancestry says "merged"
  while the actual changes are gone — so the tool reports nothing to backport.
  It prints a hint when source looks fully merged; pass `--base <fork-point>`
  (the commit where the branches diverged) to scan from there instead. Commits
  whose content really is present are still auto-skipped at execution, so you
  can't double-apply. (Plain cherry-picks do **not** move the merge-base, so
  they never trigger this.)
- Without `--create-branch`, your current branch must already contain
  `<target_branch>` in its history, so commits land on the right base.
- **Requirements:** Python 3.6+ (standard library only) and Git on `PATH`.

---

## `rapid-mlx-copilot.py`

An interactive launcher (macOS / Apple Silicon) that runs the **GitHub Copilot
CLI** against a **local [rapid-mlx](https://github.com/raullenchai/Rapid-MLX)
model server** instead
of a cloud provider — so Copilot runs fully offline on your own machine.

It does the heavy lifting of picking a model your Mac can actually run:

1. Reads this machine's RAM and chip.
2. Lists rapid-mlx model aliases, estimates each one's memory working set using
   an [LLM-Calc](https://github.com/RayFernando1337/LLM-Calc)-style estimate
   (weights + KV-cache for the configured context + OS overhead), and shows
   **only the models that fit in this machine's RAM**.
3. Tags the recommended pick for **`[general]`**, **`[planning]`**, and
   **`[coding]`** use.
4. Lets you pick one. If it's already serving, it's reused; if a *different*
   model is serving, that server is stopped and the chosen one is started.
   Models that aren't downloaded yet are pulled on demand.
5. Configures the Copilot CLI to talk to the local server and launches it.

### Usage

```sh
rapid-mlx-copilot                 # pick a model, then launch Copilot
```

or invoke the script directly (no Windows `.cmd` wrapper — this script is Mac-only):

```sh
uv run rapid-mlx-copilot.py [options] [-- copilot args…]
```

| Option | Effect |
| ------ | ------ |
| `-s`, `--serve-only` | Start/reuse the chosen model and wait — don't launch Copilot (attach from another terminal). |
| `--stop [TARGET]` | Stop running rapid-mlx server(s) and exit. `TARGET` is an optional model alias, PID, port, or `all`; with no `TARGET` you pick one interactively. |
| `--context <N>` | Context length used to size the KV cache in the RAM estimate (e.g. `16384` or `16k`). Default: `16384`. |
| `--budget <X>` | Cap usable RAM for the "runnable" filter: a number in GB (e.g. `24`) or a percentage of total (e.g. `80%`). Default: all of this machine's RAM. |
| `-h`, `--help` | Show help. |

Any other arguments (or anything after `--`) are forwarded to `copilot`.

```sh
# size the runnable filter for a longer context and a conservative RAM budget
rapid-mlx-copilot --context 32k --budget 80%

# just start/keep a model serving and attach Copilot from elsewhere later
rapid-mlx-copilot --serve-only

# stop running model server(s): interactively, or by alias/port/all
rapid-mlx-copilot --stop
rapid-mlx-copilot --stop all
```

The menu marks each model as **●** downloaded or **○** will-download, flags any
that are **(running)**, and stars the **★** top pick per category.

### Notes & caveats

- **The "can this machine run it" filter is an estimate, not a guarantee.** It
  compares `weights + KV-cache(context) + OS overhead` against total RAM. Lower
  `--context` (or the in-file `OS_OVERHEAD_GB`) to be more conservative; raise
  `--context` to size the KV cache for longer conversations.
- The server is **left running** after Copilot exits (warm cache); the script
  prints the `kill <pid>` to stop it. The recommendation lists, port, and other
  tunables are constants near the top of the script — edit to taste.
- The **first** Copilot question may take ~1–2 min while the model prefills
  Copilot's large prompt; it's a one-time cost and cached afterwards.
- Server output is logged to **`rapid_mlx.log`** in the current directory.
- **Requirements:** macOS with
  [`rapid-mlx`](https://github.com/raullenchai/Rapid-MLX) and the
  [GitHub Copilot CLI](https://github.com/github/copilot-cli) (`copilot`) on
  `PATH`, plus `uv` (or just run the `.py` with Python 3.6+; standard library
  only).

---

## `bedrock-copilot.py`

An interactive launcher that runs the **GitHub Copilot CLI** against a model on
**AWS Bedrock**. It stands up a local
[LiteLLM](https://github.com/BerriAI/litellm) proxy that presents Bedrock as an
OpenAI-compatible endpoint, points Copilot's "bring your own key" (BYOK)
settings at it, and tears the proxy down when Copilot exits.

```
Copilot CLI ──BYOK──▶ LiteLLM proxy (localhost) ──boto3──▶ AWS Bedrock
```

What it does:

1. Resolves AWS credentials **without hardcoding them** — from an AWS profile /
   SSO, 1Password, the ambient environment, or a hidden prompt — and validates
   them with `sts get-caller-identity`.
2. Discovers the Bedrock text models your credentials can reach and lets you
   pick one (or pass `--model`).
3. Lets you choose a reasoning-**effort** level (or pass `--effort`).
4. Verifies access to the chosen model with a tiny Bedrock `converse` call.
5. Starts the LiteLLM proxy and launches Copilot against it.

### Usage

```sh
bedrock-copilot --profile dev        # pick model + effort, then launch Copilot
```

or invoke the script directly:

```sh
uv run bedrock-copilot.py [options] [-- copilot args…]
```

| Option | Effect |
| ------ | ------ |
| `--model <id>` | Bedrock model id to use, skipping the menu (bare id or `bedrock/<id>`). |
| `--effort <level>` | Reasoning effort, skipping the prompt: `none`, `low`, `medium`, `high`, `xhigh`, `max`. |
| `--region <REGION>` | AWS region (default: `$AWS_REGION` or `us-east-1`). |
| `--port <N>` | Local proxy port (default: `4000`). |
| `--profile <NAME>` | AWS profile to use (else `$AWS_PROFILE`). |
| `--op-key-ref` / `--op-secret-ref` / `--op-token-ref` | 1Password secret references for the access key id / secret / optional session token. |
| `--provider-type <T>` | `COPILOT_PROVIDER_TYPE` (default: `openai`, matching the LiteLLM proxy). |
| `--skip-validation` | Skip the `sts` + Bedrock access checks. |
| `-h`, `--help` | Show help, including the list of required tools. |

Any other arguments (or anything after `--`) are forwarded to `copilot`.

```sh
# fully non-interactive: explicit profile, model, and effort
bedrock-copilot --profile dev \
  --model anthropic.claude-3-5-sonnet-20241022-v2:0 --effort high

# pull AWS keys straight from 1Password instead of a profile
bedrock-copilot --op-key-ref "op://Private/AWS/access key id" \
                --op-secret-ref "op://Private/AWS/secret access key"
```

### Notes & caveats

- **Credentials are never written to disk.** With a profile / SSO the script
  sets only `AWS_PROFILE` and lets boto3's chain do the auth; other sources live
  only in the process environment.
- **The model is fixed for the session — effort is not.** Copilot's BYOK is
  single-model, so changing the model means relaunching; the effort level can be
  changed live inside Copilot.
- **Listing ≠ access.** The menu shows models that *exist* in the region; the
  `converse` check confirms you can actually *invoke* the one you pick. Today's
  Claude models are inference-profile-only and won't appear via
  `list-foundation-models` — pass them with `--model`, or upgrade the AWS CLI so
  `list-inference-profiles` is available.
- **Requirements** (the script checks these and errors if any are missing): the
  [GitHub Copilot CLI](https://github.com/github/copilot-cli) (`copilot` — *not*
  `gh copilot`), the [AWS CLI v2](https://aws.amazon.com/cli/), and `uv` (which
  installs LiteLLM from the script's inline dependencies). The
  [1Password CLI](https://developer.1password.com/docs/cli/) (`op`) is needed
  only for the `--op-*` flags.

---

## `claude-user.py`

Launches **`claude` as a named account**, so a personal login and an enterprise
login can sit side by side without either one logging the other out. Each
account is a directory named `~/.claude-<name>`; the script points
`CLAUDE_CONFIG_DIR` at it and hands off to `claude`.

```sh
claude-user personal      # ≡ CLAUDE_CONFIG_DIR=~/.claude-personal claude
```

With no name it lists the accounts it finds and lets you pick one. Naming an
account that doesn't exist offers to create it, so a new account is just a
matter of launching it — `claude` then prompts for `/login`.

A new account isn't empty. The parts of `~/.claude` that are **content you
author** rather than **identity** are symlinked into it, so they're written once
and shared by every account — and preferences are **copied**, so they carry over
without coupling the accounts together:

```
~/.claude-personal/
  skills/ agents/ commands/ plugins/    ->  ~/.claude/…   (written once,
  rules/ themes/ workflows/                                shared everywhere)
  CLAUDE.md  keybindings.json           ->  ~/.claude/…
  settings.json    (its own copy: your prefs + this account's theme)
  projects/ sessions/ history.jsonl     (its own — never shared)
```

Each account also gets its **own theme**, chosen at creation (prompted, or
`--theme`), defaulting to one that contrasts with `~/.claude`'s — so which
account you're typing into is visible the moment claude draws.

What's **not** shared matters just as much. `projects/`, `history.jsonl` and
`sessions/` hold your conversation transcripts; linking them would pour
enterprise conversations into the personal account and back, defeating the point
of separate accounts. On Linux and Windows `.credentials.json` *is* the login,
so linking it would merge the accounts into one.

### Usage

```sh
claude-user [name] [claude args…]
```

or invoke the script directly:

```sh
uv run claude-user.py [name] [claude args…]
```

```sh
claude-user                      # pick an account from a list
claude-user personal             # launch claude as the personal account
claude-user personal --resume    # extra arguments go to claude
claude-user --list               # list accounts and exit
claude-user --create work        # make ~/.claude-work without being asked
claude-user -c --theme light-ansi work   # create with an explicit theme
claude-user --link work          # add any missing shared links, then launch
```

| Option | Effect |
| ------ | ------ |
| `-l`, `--list` | List the accounts (name and directory) and exit. |
| `-c`, `--create` | Create the account's directory if it doesn't exist, without prompting. |
| `--no-share` | Create the account without linking or copying anything from `~/.claude`. |
| `--link` | Add any missing shared links to an existing account, then launch. |
| `--theme <t>` | Theme for a newly created account: `dark`, `light`, `dark-daltonized`, `light-daltonized`, `dark-ansi`, `light-ansi`. Default: prompts, suggesting a contrast with `~/.claude`'s. |
| `-V`, `--version` | Print the version and exit. |

Options *before* the name are the script's; the name and everything after it go
to `claude` verbatim — so `claude-user work --list` lists claude's sessions, not
accounts. Forwarding therefore needs a name; use `--` to pass one that looks
like an option.

### Notes & caveats

- **The default `~/.claude` is deliberately not listed.** That's what bare
  `claude` already uses, so it needs no wrapper — keep the everyday account
  there and give the other one a name. It's also the source every other account
  links back to, so it's the one to keep.
- **Sharing never clobbers.** Anything that already exists in the account is
  left alone, so `--link` is safe to re-run and safe on an account with real
  content of its own. Share targets missing from `~/.claude` are created first
  (`skills/` often doesn't exist yet) so links are live rather than dangling:
  directories empty, `CLAUDE.md`/`keybindings.json` with inert content (an empty
  file and `{}` — both verified to load cleanly and change nothing).
- **`settings.json` is copied, not linked.** A symlink would share *writes*: one
  account's `/model` change — or one of `claude`'s own one-time settings
  migrations, which we watched fire — would silently rewrite every other
  account. The copy carries over what's worth keeping (attribution, enabled
  plugins, tui) and then the accounts diverge freely. It's also where each
  account's theme lives. There's nothing sensitive in it to worry about:
  credentials are in the Keychain (or `.credentials.json`), and enterprise
  policy lives in a machine-wide `managed-settings.json` outside the config dir.
- **The shared `plugins/` stays coherent with per-account settings.** It's a
  superset: enabling a plugin from any account installs into the shared
  `plugins/` but flips `enabledPlugins` only in that account's own settings, so
  references always resolve.
- **On Windows, symlinks need Developer Mode or an elevated shell.** The seven
  shared directories fall back to NTFS junctions, which need no privilege; only
  `CLAUDE.md` and `keybindings.json` are skipped (with a warning) when the
  privilege is missing. The account itself always works — it's just less shared.
- **Isolation works on every platform, but by two different routes.**
  `CLAUDE_CONFIG_DIR` moves the whole config tree, and on Linux/Windows the
  credentials sit inside it as `.credentials.json`. macOS keeps credentials in
  the login Keychain instead, and isolation still holds because `claude` derives
  the Keychain item's service name from a hash of the config directory path.
  Note that the official documentation currently claims the opposite — that
  macOS credentials are shared across config directories. Verified against
  **claude 2.1.211**: run with an empty config directory on a logged-in Mac, it
  reports "Not logged in", which only happens if the Keychain lookup missed.
  Worth re-checking if a future version regresses.
- **On macOS the spelling of the path is load-bearing**, as a consequence of
  that hash: two spellings of the same directory hash differently and would each
  get their own Keychain item, showing up as a surprise logout. The script
  always canonicalises the path, so a given name maps to one string forever —
  but a hand-rolled `CLAUDE_CONFIG_DIR=~/.claude-personal` in your shell is a
  *different* account from `CLAUDE_CONFIG_DIR=/Users/you/.claude-personal`.
- Creating an account only makes the directory; the login happens inside
  `claude`. Accounts are switched per invocation, never globally, so a shell
  with `claude-user personal` running doesn't disturb anything else.
- Naming a missing account **non-interactively** is an error rather than a
  silent create, on the grounds that a script naming an account that isn't there
  has a typo, not a new account. `--create` says yes in advance.

Exit status: `0` claude ran (its own status is passed through), or `--list`
succeeded · `1` no accounts found, unknown account, `claude` not on `PATH`, or
the pick was cancelled · `2` usage error (bad or missing arguments).

**Requirements:** Python 3.6+ (standard library only; no dependencies) and
[Claude Code](https://claude.com/claude-code) (`claude`) on `PATH`.

---

## `unicode-clipboard.py`

Builds a string from one or more Unicode **codepoints** and places it on the
**system clipboard**, so you can paste characters you can't type — emoji,
symbols, accented letters, zero-width marks — into any program that pastes from
the clipboard. Works on **macOS**, **Windows**, and **Linux**.

Each codepoint may be written in any of these case-insensitive forms:

| Form        | Example        | Notes                     |
| ----------- | -------------- | ------------------------- |
| `U+<hex>`   | `U+1F600`      |                           |
| `<hex>h`    | `1F600h`       |                           |
| `0x<hex>`   | `0x1F600`      |                           |
| `\u<hex>`   | `\u00e9`     | 4 hex digits              |
| `\U<hex>`   | `\U0001F600`   | 8 hex digits              |
| `&#<dec>;`  | `&#233;`       | HTML decimal entity       |
| `&#x<hex>;` | `&#xE9;`       | HTML hex entity           |

Multiple codepoints are concatenated, in order, into a single string.

### Two ways to specify the characters

**Codepoint mode (default).** Pass one or more codepoints as separate arguments
(the table above), and they're concatenated into the string to copy.

**String mode (`--string` / `-s`).** Pass a whole string whose embedded escapes
are decoded like a **Python or C/C++ string literal**, then copied. So
`-s "Hello\u002c World\u0021"` copies `Hello, World!`. Recognised escapes:

| Escape       | Meaning                                  |
| ------------ | ---------------------------------------- |
| `\uXXXX`     | Unicode codepoint, exactly 4 hex digits  |
| `\UXXXXXXXX` | Unicode codepoint, exactly 8 hex digits  |
| `\N{NAME}`   | by Unicode name, e.g. `\N{BULLET}` (also named sequences) |
| `\xH...`     | hex escape, one or more hex digits       |
| `\ooo`       | octal escape, 1–3 octal digits           |
| `\a \b \f \n \r \t \v` | bell, backspace, form-feed, newline, carriage-return, tab, vertical-tab |
| `\\ \' \" \?` | literal backslash, quote, double-quote, question mark |

Anything that isn't an escape is taken literally; an unrecognised escape (or a
dangling backslash) is an error.

In **either** mode, if the value is omitted the input is read from **stdin** —
codepoint mode splits stdin on whitespace, string mode reads it whole (minus a
single trailing newline).

### Usage

```sh
unicode-clipboard <codepoint> [<codepoint> ...]
```

or invoke the script directly:

```sh
python unicode-clipboard.py <codepoint> [<codepoint> ...]
```

- Pass one or more codepoints as arguments, or **omit them to read from stdin**
  (whitespace-separated), so you can pipe a list in.
- A confirmation summary is printed unless you pass `-q` / `--quiet`.
- Run `python unicode-clipboard.py --help` for the full reference.

```sh
# copy a single grinning-face emoji
python unicode-clipboard.py U+1F600

# copy "Hi" (codepoints are concatenated in order)
python unicode-clipboard.py U+0048 U+0069

# mix forms; copy é, a snowman, and the letter A
python unicode-clipboard.py 00e9h 0x2603 U+0041

# read a list from stdin, quietly
echo 'U+2603 U+FE0F' | python unicode-clipboard.py -q

# string mode: decode embedded escapes, then copy "Hello, World!"
python unicode-clipboard.py -s 'Hello\u002c World\u0021'

# string mode mixing literal text, a tab, and an astral (emoji) escape
python unicode-clipboard.py -s 'tab\there \U0001F389'

# string mode using Unicode names
python unicode-clipboard.py -s 'caution \N{SNOWMAN} ahead'

# string mode reading the string from stdin (trailing newline is dropped)
echo 'café' | python unicode-clipboard.py -s
```

### Notes & caveats

- **Clipboard backends (no third-party dependencies):** macOS uses `pbcopy`;
  Windows uses the Win32 clipboard API via `ctypes` (full Unicode — unlike the
  built-in `clip.exe`, which mangles non-ASCII to the console code page); Linux
  uses `wl-copy` (Wayland) or `xclip` / `xsel` (X11), whichever is found. On
  Linux, if none of those tools is installed the script says so and tells you
  what to install.
- **UTF-16 surrogates** (`U+D800`–`U+DFFF`) and codepoints above `U+10FFFF` are
  rejected — they aren't valid characters.
- Whether a pasted glyph actually *renders* depends on the destination program's
  font; the bytes on the clipboard are correct regardless.

Exit status: `0` success · `1` no usable clipboard backend, or the copy failed ·
`2` usage error (bad or missing arguments).

**Requirements:** Python 3.6+ (standard library only; no dependencies). On Linux,
one of `wl-copy`, `xclip`, or `xsel` on `PATH`.

---

## `unicode-info.py`

Fetches a single codepoint's page from **unicodeplus.com**, parses its property
tables, and prints them grouped into readable sections.

It reports the **name**, **codepoint**, **Unicode version**, **block**, and
**plane**; the **bidirectional class**, **mirroring**, and **case mappings**;
the **category**, **script**, and **combining class**; and a full set of
**encodings and escape sequences** — UTF-8/16/32, HTML, URL, and the literal
forms used by CSS, JavaScript, JSON, C/C++, Java, Python, Rust, and Ruby. Any
properties the page exposes that don't fall into those groups are listed under
*Other Properties*.

### Usage

```sh
unicode-info <codepoint>
```

or invoke the script directly:

```sh
python unicode-info.py <codepoint>
```

The codepoint may be written as `U+<hex>` or `<hex>h`:

```sh
python unicode-info.py U+0041     # LATIN CAPITAL LETTER A
python unicode-info.py 1F600h     # GRINNING FACE
python unicode-info.py u+00e9     # case-insensitive
```

Run `python unicode-info.py --help` for the full reference.

### Notes & caveats

- **It scrapes a third-party web page**, so it needs network access and will
  break if unicodeplus.com changes its markup. If the page can't be parsed the
  script says so and points you at the URL to check manually.
- Output is **colorized** (bold labels, cyan section rules) when stdout is a
  terminal; set `NO_COLOR` to disable it, or redirect to a file for plain text.
- The script forces **UTF-8 output**, so the rendered glyph and the box-drawing
  rules display correctly even on Windows consoles that default to a legacy
  code page. Whether a given glyph actually shows depends on your terminal font.

Exit status: `0` success · `1` network error or the page could not be parsed ·
`2` usage error (bad or missing arguments).

**Requirements:** Python 3.6+ (standard library only; no dependencies) and
network access.

---

## `rtf-runs.py`

Segments the body text of an **RTF** document into **runs** — maximal
stretches of text whose language and character set stay constant — and
reports, for each run, the Western/Latin proofing language, the East-Asian
(Far East) proofing language, the active font, and the character set /
codepage used to decode it.

By default a new run starts on any change to `\lang`, `\langfe`, or `\f`
(and therefore `\fcharset`). Bytes written as `\'xx` escapes are decoded
using the codepage implied by the active font's `\fcharset` (falling back to
the document's `\ansicpg`); `\uN` Unicode escapes are decoded directly and
their `\ucN` fallback bytes are skipped.

### Usage

```sh
rtf-runs FILE.rtf [options]
```

or invoke the script directly:

```sh
python rtf-runs.py FILE.rtf [options]
```

| Option | Effect |
| ------ | ------ |
| `--json` | Emit JSON Lines (one JSON object per run) instead of a human-readable table. |
| `--min-len <N>` | Drop runs whose stripped text is shorter than `N` (e.g. `1` to drop whitespace-only runs). |
| `--break-on <fields>` | Comma list of fields that start a new run: `lang`, `langfe`, `font`, `charset` (default: `lang,langfe,font`). |

```sh
# human-readable table
python rtf-runs.py document.rtf

# one JSON object per run, for scripting
python rtf-runs.py document.rtf --json

# drop whitespace-only runs
python rtf-runs.py document.rtf --min-len 1

# also break runs on a raw \fcharset change, not just font number
python rtf-runs.py document.rtf --break-on lang,langfe,font,charset
```

Run `python rtf-runs.py --help` for the full reference.

### Notes & caveats

- **Per-slot fonts aren't modeled separately.** RTF keeps three font "slots"
  active at once (`\loch` low-ANSI, `\hich` high-ANSI, `\dbch` double-byte).
  This tool tracks only the single current font set by `\f`, which is what
  `\hich`/`\loch` text usually resolves to in Word output. For the common
  case (Latin + one CJK font) the reported charset is correct; deeply mixed
  slots may need slot-aware decoding.
- Header destinations (`fonttbl`, `colortbl`, `stylesheet`, `info`, and any
  `{\* ...}` ignorable destination) are parsed but not emitted as runs.
- The LCID-to-language-name table covers a useful subset, not every Windows
  LCID; unrecognised codes are reported as `LCID <n>`.

Exit status: `0` success · `1` the file could not be read ·
`2` usage error (bad or missing arguments).

**Requirements:** Python 3.6+ (standard library only; no dependencies).

---

## `cpp-unicode-escapes.py`

Finds C++ string and character literals that misuse `\xNNNN` to mean a
**Unicode code point** and rewrites them as the proper `\uNNNN`
universal-character-name.

This matters because C++'s `\x` escape is **greedy** — it consumes *every*
hex digit that follows, not just two or four. So `"\x4E00"` is a single
escape of value `0x4E00` (implementation-defined, often truncated in a narrow
string), and `"\xABcat"` silently swallows the `c` and `a` as hex digits. When
the intent was a 16-bit Unicode scalar, the correct spelling is the
fixed-width `\uNNNN`, which always takes exactly four hex digits.

It converts **only the unambiguous case**: `\x` followed by exactly four hex
digits that are not followed by a fifth. It is **literal-aware**, not a blind
regex, so it leaves alone:

- 1–3 digit escapes (`\x41`, `\xAB`) — genuine byte values;
- 5+ digit escapes (`\x10FFFF`) — ambiguous; convert those by hand;
- surrogate values `\xD800`–`\xDFFF` — `\u` may not name a surrogate, so the
  conversion would turn legal code into ill-formed code;
- anything outside a string or character literal;
- comments (`//` and `/* */`), raw string literals `R"(...)"`, and the
  contents of escaped backslashes (`\\x...`);
- the C++14 digit separator (`1'000'000`).

By default it **edits files in place**; use `--dry-run` for a report that
writes nothing. I/O is byte-preserving (original line endings and any
non-ASCII bytes are untouched). It scans large trees quickly via a cheap
byte-level pre-filter (files with no candidate are never decoded or scanned),
`.git`/`.svn`/`.hg` pruning, and a ripgrep-style **thread pool** that overlaps
read latency across many files.

### Usage

```sh
cpp-unicode-escapes PATH [PATH ...] [options]
```

or invoke the script directly:

```sh
python cpp-unicode-escapes.py PATH [PATH ...] [options]
```

- Each `PATH` may be a file or a directory; directories are walked recursively.
- Output is sorted by path for deterministic results regardless of thread order.

| Option | Effect |
| ------ | ------ |
| `--dry-run` | Report the changes that would be made without writing any files. |
| `--ext <list>` | Comma-separated extensions to scan when given a directory (default: common C/C++ extensions). |
| `-q`, `--quiet` | Suppress the per-change lines; show only a summary. |
| `-j`, `--jobs <N>` | Number of worker threads (default: scales with CPU count; `1` disables parallelism). |

```sh
# edit one file in place
python cpp-unicode-escapes.py src/foo.cpp

# recurse directories, in place
python cpp-unicode-escapes.py src/ include/

# preview only, write nothing
python cpp-unicode-escapes.py src/ --dry-run

# custom extension set
python cpp-unicode-escapes.py . --ext .cpp,.h,.cuh

# tune parallelism for a very large tree
python cpp-unicode-escapes.py . -j 16
```

Run `python cpp-unicode-escapes.py --help` for the full reference.

### Notes & caveats

- **One ambiguous case to eyeball:** a literal like `"\xABcat"` becomes
  `"ꯊ"`, because C++'s greedy `\x` *already* reads `ABca` as four hex
  digits there — the conversion preserves the value but may not match the
  author's intent. Run `--dry-run` first and scan for any 4-hex run
  immediately followed by a hex letter (`a`–`f`) if you want to spot these.
- Since the default is to edit in place, your **version control diff is the
  safety net** — review it before committing.

Exit status: `0` success (whether or not anything changed) ·
`1` one or more files could not be read or written ·
`2` usage error (bad or missing arguments).

**Requirements:** Python 3.6+ (standard library only; no dependencies).

---

## `docx-runs.py`

Reports the resolved **language of every text run** in a `.docx`. A *run*
(`<w:r>`) is a contiguous span of text with uniform formatting; its language
lives in `<w:lang>`, but Word rarely stamps every run, so the value is resolved
by walking the WordprocessingML inheritance hierarchy. A `.docx` is just a ZIP
of XML parts, so this uses the standard library only (`zipfile` +
`xml.etree.ElementTree`) — no `python-docx` or other dependency.

For each run the language is resolved in order: direct run properties
(`w:rPr/w:lang`) → character style (`w:rStyle`, following `w:basedOn`) →
paragraph style (`w:pStyle`, or the document's default paragraph style if none)
→ document defaults (`w:docDefaults`). The paragraph mark's own properties
(`w:pPr/w:rPr`) are deliberately **not** consulted: per ECMA-376 they format the
pilcrow, not the runs inside the paragraph.

`<w:lang>` carries up to three attributes — `w:val` (Latin/Western),
`w:eastAsia` (CJK/Hangul/Kana), and `w:bidi` (RTL/complex) — applied
per character by script. The tool classifies the run's characters to report
which slot actually applies, falling back to the run's `<w:rFonts w:hint>` and
then the surrounding script for script-neutral runs (digits, punctuation).

### Usage

```sh
docx-runs FILE.docx [options]
```

or invoke the script directly:

```sh
python docx-runs.py FILE.docx [options]
```

| Option | Effect |
| ------ | ------ |
| `--json` | Emit machine-readable JSON (one object per run) instead of a human-readable listing. |
| `--merge` | Coalesce adjacent runs of the same effective language within a paragraph into one segment per contiguous language span. |

```sh
# human-readable listing
python docx-runs.py document.docx

# coalesce Word's fragmented runs into clean per-language segments
python docx-runs.py document.docx --merge

# machine-readable output for scripting
python docx-runs.py document.docx --json
```

Run `python docx-runs.py --help` for the full reference.

### Notes & caveats

- **`--merge` treats invisible characters as language-neutral.** Whitespace,
  zero-width spaces, and Unicode bidi controls (LRM/RLM, the isolates, and the
  legacy embeddings/overrides) carry no language; they fold into the
  surrounding segment instead of shattering a contiguous language span.
- **One effective language per run.** The script reports the *dominant* script
  of each run rather than labelling every code point individually, which is a
  simplification of the per-character model for runs that genuinely mix scripts.
- Runs marked `<w:noProof/>` (spell/grammar check disabled) are still reported;
  treat them as language-agnostic if you're feeding a spellchecker.
- The application/OS editing locale (the lowest fallback) isn't stored in the
  file, so a run that resolves to nothing is reported as undetermined.

Exit status: `0` success · `1` the file is not a readable `.docx` ·
`2` usage error (bad or missing arguments).

**Requirements:** Python 3.6+ (standard library only; no dependencies).

---

## `latin-runs.py`

Extracts every embedded **Latin-script run** — English phrases, product names,
URLs, version strings, copyright notices — from text whose primary content is
one or more **non-Latin scripts** (Korean, Arabic, Hebrew, …), including
right-to-left ones. Each run is reported with the language-**neutral glue**
(digits, spaces, punctuation, symbols) that logically belongs to it, and with
its `(start, end)` offsets.

The whole difficulty is the neutral characters. A comma, a space, a `©`, or a
digit carries no script identity of its own, yet it may be an integral part of a
Latin entity (`Windows 11 (23H2)`, `© 2026 Example Corp`, `macOS™`) — or it may
be a bridge between the surrounding non-Latin text and an English phrase, in
which case it belongs to neither. This tool folds neutral glue **in** when it
belongs (internally, trailing, or leading) and leaves it **out** when it bridges
or dangles, in all positions. It is a conforming implementation of *"Latin Run
Extraction from Mixed-Script Text"*, spec v1.4 (in
[`docs/`](docs/latin-run-extraction-spec-v1.4.md)), which adapts the
neutral-resolution phase of the Unicode Bidirectional Algorithm (UAX #9): every
strong script is treated alike and all work happens in **logical order**, so the
result is indifferent to RTL display.

### Usage

```sh
latin-runs [FILE] [policy options]
```

or invoke the script directly:

```sh
uv run latin-runs.py [FILE] [policy options]
```

- Pass `FILE` to read a UTF-8 text file, or **omit it to read from stdin**.
- Each extracted run prints as `start  end  text` (offsets in **code points**);
  `--json` emits one JSON object per run instead.
- Run `latin-runs --help` for the full reference.

```sh
# a URL embedded in Korean, from stdin
echo '주소는 https://example.com/a?b=1 입니다' | latin-runs
#      4     29  https://example.com/a?b=1

# a copyright line embedded after a Korean sentence
echo '텍스트. © 2026 Watch Tower Bible and Tract Society of Pennsylvania' | latin-runs

# machine-readable output
latin-runs --json report.txt

# capture a bare leading year that defaults would drop (© would anchor it)
echo '한국어 100 GB+ 저장' | latin-runs --numerals-bind-to-latin   # -> 100 GB+
```

### Policy knobs

Every knob from spec §9 is exposed as a flag; the engine takes the defaults
(which match the companion conformance fixture) unless you override them.

| Option | Effect |
| ------ | ------ |
| `--no-strip-terminal-punct` | Keep a trailing `. , ; : ! ?` that was captured as glue (default: strip it — it usually punctuates the *host* sentence). |
| `--numerals-bind-to-latin` | Let a **leading** digit group bind to adjacent Latin without a `©`-style anchor (captures a bare `2026 Windows`). |
| `--no-trailing-digits-bind` | Make **trailing** digit groups purely provisional, so `Windows 11` → `Windows` (symmetric with leading behaviour). |
| `--max-bridge <N>` | Refuse the sandwich merge when the neutral run between two Latin runs is longer than `N` grapheme clusters (default: `inf`, no limit). |
| `--bidi-controls {strip,preserve_pairs}` | How to treat bidi formatting characters (default: `strip` them before analysis). |
| `--min-latin-letters <N>` | Minimum Latin letters for a run to be emitted (default: `1`). |
| `--affinity-override CP=AFFINITY` | Move a character's binding affinity, e.g. `U+00AE=RIGHT` to make `®` a prefix anchor. Repeatable. `AFFINITY` ∈ `RIGHT,LEFT,SEP,DIGIT,STOP`. |
| `--no-cjk-punct-strong` | Do **not** strengthen CJK punctuation / full-width forms to strong (they become neutral glue, so `Alpha。Beta` merges into one run). |

```sh
# keep host-sentence punctuation and require at least 3 Latin letters
latin-runs --no-strip-terminal-punct --min-latin-letters 3 notes.txt

# treat the registered-trademark sign as a leading anchor
echo '한국어 ®Brand 텍스트' | latin-runs --affinity-override U+00AE=RIGHT   # -> ®Brand
```

### Notes & caveats

- **Offsets are code points** into the original string (the documented unit).
  In the default `strip` mode `text[start:end]` is exactly the emitted run; under
  `--bidi-controls preserve_pairs` the span may still enclose shed (unmatched)
  control characters, so the emitted text can be shorter than the raw slice.
- **`preserve_pairs` is conservative by design.** Unmatched isolates/embeddings
  are always shed, and a matched pair is retained inside a run only when the
  whole pair falls within that one run; a pair that would straddle a run boundary
  is excluded entirely (spec §8.2). For matching, indexing, and translation
  memory, the default `strip` is the right choice.
- **Full-width digits are never captured as a trailing version number.** Unlike
  ASCII digits (`Windows 11` → `Windows 11`), `Windows １１` yields `Windows`:
  a full-width character signals CJK context and is treated as host text
  (spec §5.2e, §7.3), the same reasoning that makes Arabic-Indic digits strong.
- **Unicode data comes from two sources**, reported by `latin-runs
  --unicode-version`: the third-party `regex` module supplies UAX #29 grapheme
  segmentation and the `Script` / `Script_Extensions` / `Extended_Pictographic`
  / `Regional_Indicator` properties; the standard library's `unicodedata`
  supplies general categories. The spec's minimum reference is Unicode 16.0;
  documented differences from a later UCD version are not conformance failures.
- Conformance is checked by `tests/test_latin_runs.py`, which drives all 29 cases
  of the machine-readable fixture (`docs/latin-run-extraction-tests.json`) plus
  its per-knob sensitivity variants.

Exit status: `0` success · `1` the input could not be read or decoded ·
`2` usage error (bad or missing arguments).

**Requirements:** Python 3.7+ and the [`regex`](https://pypi.org/project/regex/)
module (Python's `unicodedata` has no `Script` property or grapheme
segmentation). The dependency is declared inline in the script, so the wrapper's
`uv run` installs it automatically; if you invoke the `.py` with plain Python,
`pip install regex` first.

---

## `git-switch.py`

A full-screen, **interactive Git branch switcher** in the spirit of `fzf` /
`lazygit`: it lists your branches, lets you home in on one three different ways,
and checks it out. The picker is **modal**, like vim.

**NORMAL mode** (the default):

| Key | Action |
| --- | ------ |
| `j` / `k` or `↑` / `↓` | move the highlight cursor |
| `g` / `G` | jump to the top / bottom |
| `h` / `←` | collapse the folder (or hop to the parent folder) |
| `l` / `→` | expand the folder (or descend into it) |
| *digits* then `Enter` | select a branch by **number** (the cursor follows as you type, so `12⏎` lands on branch 12) |
| `Enter` | expand/collapse a folder, or switch to a branch |
| `/` | enter FILTER mode |
| `Tab` (or `r`) | toggle **remote** branches in / out of the list |
| `q` / `Esc` | quit without switching |

**FILTER mode** (entered with `/`): type a **regular expression** that filters
the branch names; `↑`/`↓` move among the matches, `Enter` switches to the
highlighted branch, `Backspace` edits, `Esc` clears the filter and returns to
NORMAL. (An invalid regex falls back to a literal match, flagged in the footer.)

Branch names are split on `/` into a collapsible **folder tree**, so
`feature/login` and `feature/logout` tuck under a `feature/` folder. Folders
start **collapsed**; expand them on demand, or just start typing a filter — a
filter auto-expands every folder that contains a match.

Press `Tab` (or `r`) to fold in **remote** branches; they nest under their
remote as a folder (`origin/ › feature/ › login`). Picking a remote branch that
has no local counterpart **creates a local tracking branch and switches to it**
(`git switch -c <name> --track <remote>/<name>`); if a local branch of that name
already exists, it just switches to the local one.

### Usage

Run it from inside the repository:

```sh
git-switch [options]
```

or invoke the script directly:

```sh
python git-switch.py [options]
```

| Option | Effect |
| ------ | ------ |
| `-r`, `--remotes` | Start with remote branches already included. |
| `--no-color` | Disable colored output (also honors `NO_COLOR`). |

Run `python git-switch.py --help` for the full key reference.

### Notes & caveats

- **The current branch is marked `*`** and the cursor opens on it; selecting it
  is a no-op. `git switch` handles the actual checkout, so an unclean working
  tree that would be clobbered makes it refuse — its message is printed and the
  tool exits non-zero, exactly as a manual `git switch` would.
- It draws on the **alternate screen** over `stderr` and reads keys in raw mode
  from `stdin`; both must be a terminal (piping in or out prints an error).
- **No third-party dependencies** — the TUI is hand-rolled with raw terminal
  mode and ANSI escapes (no `curses`), so the one script runs on **macOS,
  Linux, and Windows** (Windows 10+ console, VT mode enabled automatically).

Exit status: `0` a branch was switched, or you quit without choosing ·
`1` not inside a Git repository, not an interactive terminal, or `git switch`
failed.

**Requirements:** Python 3.6+ (standard library only; no dependencies), Git on
`PATH`, and the `branch_tui.py` module beside it (shared with `delete-branch.py`).

---

## `delete-branch.py`

The same vim-style picker as [`git-switch.py`](#git-switchpy), but instead
of switching you **check off** as many branches as you like — local *and*
remote, or whole folders — and delete them in one pass. It shares its entire
navigation engine with `git-switch.py` (the folder tree, regex filter,
remotes toggle, and all the keys behave identically).

The differences are the checkboxes and what `Enter` does:

| Key | Action |
| --- | ------ |
| `Space` | check / uncheck the branch — or the **whole folder** — under the cursor |
| `Enter` | same meaning as in `git-switch.py`: expand/collapse a folder, or check/uncheck a branch — it **never deletes** |
| `d` | delete everything that's checked (after a confirmation) |
| `F` | toggle **force**: `git branch -D` instead of the safe `-d` |
| *digits* | jump the cursor to a branch by **number** (then `Space`/`Enter` to check it) |
| `j`/`k`, `g`/`G`, `h`/`l`, `/`, `Tab`, `q` | move, fold, filter, toggle remotes, quit — exactly as in `git-switch.py` |

`Enter` deliberately keeps its `git-switch.py` meaning so muscle memory never
triggers a delete; deletion lives on its own key, `d`. (While you're typing a
filter, `d` is part of the expression — press `Esc` first, then `d`; your checks
are kept.)

Checking a folder checks every branch beneath it; a `[~]` box means only *some*
of a folder's branches are checked. The branch you're currently on is
**protected** — it has no checkbox, since Git won't delete the branch you're
standing on.

### Safety

- **Nothing is deleted from the TUI.** When you press `d` the picker closes
  and prints exactly what will go — **local** deletions and **remote** ones
  (`git push <remote> --delete`, which updates the shared remote for everyone)
  listed separately — then asks for a single `y/N`.
- **Unmerged branches are refused by default.** Local deletes use `git branch
  -d`, which won't drop a branch whose commits aren't merged; press `F` to force
  (`-D`) up front when you already know you want to discard them. Remote
  deletions are always forced — that's how `git push --delete` works.
- **If `-d` refuses some branches, you're offered to force just those.** Rather
  than make you restart and re-tick everything, any branch held back as "not
  fully merged" is listed and you get a second `y/N` to force-delete exactly
  that set with `git branch -D` (git's `hint:` chatter is suppressed).

### Usage

Run it from inside the repository:

```sh
delete-branch [options]
```

or invoke the script directly:

```sh
python delete-branch.py [options]
```

| Option | Effect |
| ------ | ------ |
| `-r`, `--remotes` | Start with remote branches already included. |
| `-f`, `--force` | Start in force mode (`git branch -D`). |
| `--no-color` | Disable colored output (also honors `NO_COLOR`). |

Run `python delete-branch.py --help` for the full key reference.

Exit status: `0` deletions ran (including deliberately keeping unmerged
branches), or you quit / aborted without deleting · `1` not inside a Git
repository, not an interactive terminal, or a deletion failed unexpectedly.

**Requirements:** Python 3.6+ (standard library only; no dependencies), Git on
`PATH`, and the `branch_tui.py` module beside it (shared with `git-switch.py`).

---

## `clang-query-run.py`

Runs **any** `clang-query` AST matcher across every translation unit in a
`compile_commands.json`, in parallel, and aggregates the matches into an
AI-ready **investigation packet**. The query is not hardcoded — pick one from a
small library, point at a `.query` file, or pass one inline.

The point is that it uses Clang's AST matchers, **not grep**. Only the compiler
knows whether `x.string()` is a `std::filesystem::path` method or some unrelated
`string()` / `to_string()` / `optional_string()` — and the same is true for any
type- or overload-aware query you want to run.

This is the reusable scaffolding around a query: compile-DB auto-detection,
parallel fan-out over all TUs, deduping hits seen through multiple TUs, the
"compiled-but-failed-to-parse" detection that `clang-query` hides behind exit 0,
and (for location queries) source-context enrichment plus JSON / Markdown output.

### Choosing a query

```sh
clang-query-run                       # pick from the library via a menu
clang-query-run --list                # list library queries and exit
clang-query-run -f my.query           # run a .query file
clang-query-run -q 'match cxxThrowExpr().bind("t")'   # run an inline query
```

The library lives in **`clang-queries/`** beside the script. Each `.query` file
can carry `# title:` / `# description:` header comments, which the menu and
`--list` display. Running with no `-f`/`-q` shows the menu; pick by number.

Ships with `path-string.query` — every `.string()`/`.generic_string()` call
whose receiver is genuinely a `std::filesystem::path` (an encoding-hazard audit).
To add your own, drop a `.query` file in `clang-queries/`; see
[`clang-queries/README.md`](clang-queries/README.md) for the header format,
output modes, and rubric sidecars.

### Output modes

The runner adapts to the query's `set output` mode:

- **`diag`** (default) — locations. Each `.bind(...)` match yields a
  `file:line:col`, which the runner dedups, enriches with source context, and
  can render as `--json` or a `--report` packet.
- **`dump` / `print` / `detailed-ast`** — AST text. The runner streams
  `clang-query`'s raw output per TU (still parallelised, still flagging parse
  failures), since there are no source locations to aggregate. `--json` /
  `--report` aren't available for these.

### The investigation packet

`--report` emits a self-contained Markdown file you hand to an AI: a **rubric**
(the task framing) followed by every match with `file:line:col` and source
context (`>>` marks the hit line). The rubric comes from a
**`<query-stem>.rubric.md`** sidecar next to the query if one exists, else a
generic "analyse these findings" header. So `path-string.query` pairs with
`path-string.rubric.md`, which buckets each site (display/logging, URI
construction, external API, test/comparison) and explains the C++23 `char8_t`
encoding hazard.

### Usage

```sh
clang-query-run [options]            # or: uv run clang-query-run.py [options]
```

First generate a compile DB for the target repo:

```sh
# CMake
cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

# Non-CMake builds (brew install bear)
bear -- make
```

Then, from anywhere in the project (the build DB is **auto-detected** — see
below):

```sh
# AI investigation packet (Markdown) — the path-string audit
clang-query-run -f clang-queries/path-string.query --report > investigation.md

# Plain list of match locations
clang-query-run -f clang-queries/path-string.query

# point at the build dir explicitly when it's somewhere unusual
clang-query-run -f clang-queries/path-string.query -p out --json > hits.json
```

By default it finds the `compile_commands.json` itself. First it probes the
common build-dir names (`build`, `out`, `cmake-build-debug`, …) while walking up
from the current directory to the repo root (so it works from a subdirectory
too); if that misses, it falls back to a bounded recursive scan down from the
root, which catches DBs buried in deep, oddly named build trees like
`.build/core/darwin/arm64/release/`. When several exist the **newest** wins (and
the rest are noted). The chosen DB is printed to stderr. Use `-p` to point at a
specific build dir — or straight at the JSON file — to override.

| Option | Effect |
| ------ | ------ |
| `-f`, `--query-file FILE` | Run a `.query` file. |
| `-q`, `--query 'QUERY'` | Run an inline query string. |
| `--list` | List library queries and exit. |
| `-p`, `--build-dir DIR` | Build dir containing `compile_commands.json`, or a path straight to the file (default: auto-detect). |
| `--report` | Emit the Markdown investigation packet for an AI (diag queries). |
| `--json` | Emit structured records (location + source context) instead of a plain list (diag queries). |
| `-c`, `--context N` | Source context lines on each side of a hit (default 6). |
| `-j`, `--jobs N` | Parallel `clang-query` workers (default: CPU count). |
| `--clang-query PATH` | Path to the `clang-query` binary. |
| `--extra-arg ARG` | Extra clang arg, repeatable (see the macOS note below). |
| `--no-auto-sdk` | Don't auto-add macOS `-isysroot`/`-resource-dir` (on by default on macOS). |
| `--show-errors` | Print the TUs `clang-query` failed to parse. |

Run `uv run clang-query-run.py --help` for the full reference.

### macOS gotcha (read this)

`clang-query` parses each translation unit with **its own** headers. If the
`compile_commands.json` was produced by **Apple `clang++`** but you run
**homebrew `clang-query`**, the standard-library headers won't match and the TU
fails with `fatal error: 'filesystem' file not found`. A silent parse failure
means *missed* findings, not false ones — so don't trust a clean run that also
reports parse errors (**rerun with `--show-errors`** to see which TUs failed).

**The runner handles this for you on macOS.** Before scanning it probes the SDK
(`xcrun --show-sdk-path`) and `clang-query`'s own resource dir, and injects the
matching `-isysroot` / `-resource-dir` — exactly the flags an Apple-clang DB
omits because Apple clang has them baked in. The resource dir is taken from the
`clang` *next to* `clang-query`, so the builtin headers always match the tool's
version, never Apple's. It backs off if you already passed those flags yourself,
and you can turn it off with `--no-auto-sdk`.

If TUs *still* fail to parse after that, either the SDK probe failed (is `xcrun`
working?) or the build genuinely needs a specific toolchain. Then pick one:

- **Build the compile DB with the matching toolchain** (simplest) —
  `-DCMAKE_CXX_COMPILER=/opt/homebrew/opt/llvm/bin/clang++`; or
- **Pass the headers yourself** — `--extra-arg=-isysroot --extra-arg="$(xcrun
  --show-sdk-path)"` (and similar for `-resource-dir`), which also disables the
  auto-injection for that flag.

Also note `clang-query` is often a shell **alias**, which a Python subprocess
can't see; the script falls back to `/opt/homebrew/opt/llvm/bin/clang-query`, or
override with `--clang-query`.

### Writing a query

A `.query` file is plain `clang-query` script. To collect locations, set `diag`
output and **bind** a node — the bound node's location is what gets reported:

```
# title: throw-expression audit
# description: every throw site in the project
set output diag
match cxxThrowExpr().bind("throw")
```

[`clang-queries/README.md`](clang-queries/README.md) is the full contributor
guide — header format, output modes, rubric sidecars, and `path-string.query`
as a template for type-aware matchers.

**Requirements:** Python 3.8+ (standard library only; no dependencies),
`clang-query` (`brew install llvm`), a `compile_commands.json` for the target
repo, and the `clang-queries/` directory beside the script.

---

## `git-open.py`

A full-screen, **interactive file finder** for a Git repo: type a regular
expression or a **glob**, watch the matching tracked files arrange themselves
into a collapsible **folder tree**, and hit `Enter` to open the one you want in
your editor. Open as many as you like — the picker stays put until you quit. The
UI is **modal**, like vim.

**PATTERN mode** (where you land with no argument — the "insert" mode):

| Key | Action |
| --- | ------ |
| *type* | a **regex** or **glob**; the file list filters live (case-insensitive) |
| `↑` / `↓` | move the highlight through the matches |
| `Enter` | open the highlighted file |
| `Esc` or `Tab` | switch to BROWSE mode to navigate with `j`/`k` (an empty prompt just switches — nothing quits here) |
| `Backspace` | edit the expression |

**BROWSE mode** (where you land when you pass a pattern, or after `Esc`/`Tab`):

| Key | Action |
| --- | ------ |
| `j` / `k` or `↑` / `↓` | move the highlight cursor |
| `g` / `G` | jump to the top / bottom |
| `h` / `←` | hop up to the parent folder |
| `l` / `→` | expand / step into a folder |
| *digits* | jump the cursor to a file by its **number** |
| `Enter` | open the file under the cursor (on a folder, fold it) |
| `/` or `Tab` | return to PATTERN mode to search for something else |
| `q` | quit (`Esc` only navigates — it never quits) |

### Regexes and globs

Queries are regular expressions, except that a query *written as a glob* is read
as one — so `*.props` finds what you'd expect rather than nothing. A query
counts as a glob when it uses glob syntax (`*`, `?`, `[...]`) and steers clear of
regex-only syntax (`\ ( ) | ^ $ + { }`); the footer shows `(glob)` when that's
how your query was taken. A glob has to match to the **end** of the path and
start at a **folder boundary**, and its `*` spans `/`, as in `git ls-files`:

| Query | Read as | Matches |
| ----- | ------- | ------- |
| `*.props` | glob | every `.props` file, in any folder |
| `build/*.props` | glob | `.props` files under any `build/` folder, at any depth |
| `test_*.py` | glob | files named `test_*.py` in any folder — but not `mytest_x.py` |
| `\.props$` | regex | the same as `*.props`, the long way round |
| `.*\.props$` | regex | a `*`, but the `\` and `$` keep it a regex |
| `props` | regex | any path with "props" anywhere in it |

A query that is neither valid regex nor glob-shaped (`main(c`) is matched
**literally**, and the footer says `(literal)`.

Tracked files come from `git ls-files` run at the **repository root**, so the
whole repo is searchable no matter which subdirectory you launch from, and the
file opens by its full path. Paths are split on `/` into the folder tree, so
`src/app/main.c` and `src/app/util.c` tuck under a `src/ › app/` folder; while a
pattern is active, every folder that holds a match is shown expanded.

### Configuring the editor

Both git-open and git-grep read the same TOML file, **`.git-open-config`**, kept
beside the scripts and **gitignored** so each clone sets its own. The first run
creates it for you, pre-filled with `vim`:

```toml
editor = "vim"
line   = "+{line} {file}"
```

- **`editor`** is run as a shell-style command. git-open appends the file path.
  With no `editor` key (or no config file) it falls back to `$VISUAL`, then
  `$EDITOR`, then a platform default. git-open ignores `line` — it opens whole
  files.
- **`line`** tells git-grep how to open a file **at a line**: it is split
  shell-style and `{file}`, `{line}`, `{column}` are substituted into the pieces
  (so a path with spaces stays a single argument).

| Editor | `editor` | `line` |
| ------ | -------- | ------ |
| Vim / Neovim | `"vim"` | `"+{line} {file}"` |
| VS Code | `"code -g"` | `"{file}:{line}:{column}"` |
| Sublime Text | `"subl"` | `"{file}:{line}:{column}"` |
| Emacs (client) | `"emacsclient -nw"` | `"+{line} {file}"` |

**Opening inside an editor's terminal.** When you launch git-open, git-grep, or
git-diff from the **integrated terminal** of VS Code, a JetBrains IDE (CLion and
friends), or Zed, the file is handed to that *already-running* editor instead of
the configured one — through a `vscode://` URL or a `clion`/`zed` launcher — so
it lands in a new tab there. These hand-offs are fire-and-forget, so the picker
stays up rather than stepping aside. Detection keys off the terminal's
environment variables (`TERM_PROGRAM`, `TERMINAL_EMULATOR`, `ZED_TERM`) and
lives in the shared `editor_ide.py` module; everywhere else, the `editor`/`line`
settings above are used.

### Usage

Run it from inside the repository:

```sh
# start at an empty prompt, then type a regex or glob
git-open

# open straight onto the matches for a pattern
git-open '\.py$'

# ...or a glob (quote it, or the shell expands it first)
git-open '*.props'
```

or invoke the script directly:

```sh
python git-open.py [pattern]
```

### Notes & caveats

- The first match is **not** auto-opened — even a single hit shows in the
  picker, so you can keep searching and opening without relaunching.
- It draws on the **alternate screen** over `stderr` and reads keys in raw mode
  from `stdin`; both must be a terminal (piping in or out prints an error). While
  your editor runs, the picker steps off the screen and restores itself when the
  editor exits.
- **No third-party dependencies** — the TUI is hand-rolled with raw terminal
  mode and ANSI escapes (no `curses`), so it runs on **macOS, Linux, and
  Windows** (Windows 10+ console, VT mode enabled automatically).

Exit status: `0` you quit normally (whether or not you opened anything) ·
`1` not inside a Git repository, or not an interactive terminal.

**Requirements:** Python 3.11+ (standard library only — `tomllib` reads the
config), Git on `PATH`, and the `branch_tui.py`, `editor_config.py`, and
`editor_ide.py` modules beside it (the latter two shared with `git-grep.py` and
`git-diff.py`).

---

## `git-grep.py`

A full-screen, **interactive front end for `git grep`**: type a pattern, see
every matching line gathered into a collapsible tree of files, then hit `Enter`
to jump straight to that line in your editor. Open as many hits as you like —
the browser stays put until you quit. **Modal**, like vim.

**PATTERN mode** (where you land with no argument):

| Key | Action |
| --- | ------ |
| *type* | the `git grep` pattern (a basic regex, as `git grep` takes it) |
| `Enter` | run `git grep` and drop into BROWSE mode on the hits |
| `↑` / `↓` | move the highlight through the current results |
| `Esc` | switch to BROWSE mode to navigate with `j`/`k` (clears a no-match pattern; an empty prompt just switches) |
| `Tab` | toggle **case-insensitive** (`-i`) matching |
| `Backspace` | edit the pattern |

**BROWSE mode** (where you land when you pass a pattern, or after `Enter`):

| Key | Action |
| --- | ------ |
| `j` / `k` or `↑` / `↓` | move the highlight cursor |
| `g` / `G` | jump to the top / bottom |
| `h` / `←` | hop up to the parent folder / file |
| `l` / `→` | expand / step into a folder or file |
| `Enter` | open the match under the cursor **at its line** (on a folder/file row, fold it) |
| `r` | **re-run** the whole stack and refresh the results (handy after editing), keeping your place |
| `/` | **refine**: filter the current hits with a sub-grep (push a level onto the stack) |
| `<` | **back up** one level (pop the last filter) |
| `\` | **start fresh**: clear the whole stack and return to an empty prompt |
| `0`–`9` | set the **context window** to N lines around each hit (`0` = none) |
| `+` / `-` | widen / narrow the context (`+` goes past 9; can't narrow below the parent level) |
| `:N` | jump the cursor to line number **N** — the number shown at the start of each row; `:` again starts a new number, `Enter`/`Esc`/any move closes the prompt |
| `Tab` | toggle case-insensitive (`-i`) and re-run |
| `q` | quit (`Esc` only navigates — it never quits) |

**FILTER mode** (a sub-grep over the current hits; reached with `/` from BROWSE):

| Key | Action |
| --- | ------ |
| *type* | a pattern that narrows the visible lines **live**, matched against the file path **and** the line text |
| `!pattern` | **exclude**: keep the lines that do *not* match |
| `Enter` | push this filter onto the stack |
| `Esc` | cancel without pushing |
| `↑` / `↓` | move through the filtered hits |

### Stacking greps

Because filters **stack**, you can drill down in steps that a single regex can't
express. Grep `error`, then `/` `handler` to keep only those lines, then `/`
`!_test.` to drop the test files — the header shows the whole stack as a
breadcrumb (`error  +handler  -_test.`). `<` pops back up a level at a time and
`\` wipes the stack to start over. Each filter is matched against the **path and
the line text** together, so you can narrow by either content or filename. `r`
re-runs the base `git grep` and re-applies every filter, so after editing you
see the same view, refreshed.

### Context windows

Each level of the stack carries a **context window** — press `0`–`9` (or `+` /
`-`) to pull in that many lines around every hit, read straight from the working
tree and shown **dimmed**. Crucially, those context lines join the **searchable
set**: the next `/` filter can match on a line that merely sits *near* an earlier
hit, and that line then becomes the new anchor. A deeper level **inherits** the
window and can only widen it — never narrow below its parent — so you can grep
`ALPHA`, widen to `±2` to see what surrounds it, then filter on `BRAVO` that only
appears two lines down. The header breadcrumb tags each level with its width
(`ALPHA ±2  +BRAVO ±2`).

Matching lines are grouped under their file, and files nest in a **folder tree**
split on `/`, so hits in `src/app/main.c` and `src/lib/parse.c` sit under a
`src/` folder you can collapse. Everything starts expanded, with each file
showing its match count. `git grep` is run at the **repository root**, so it
works from any subdirectory and opens files by their full path.

The editor — and **how to open it at a line** — come from the same
`.git-open-config` that git-open uses; see
[Configuring the editor](#configuring-the-editor) above.

### Usage

Run it from inside the repository:

```sh
# start at an empty prompt, then type a pattern and press Enter
git-grep

# grep immediately for a pattern
git-grep 'def main'

# start case-insensitive (toggle later with Tab)
git-grep -i todo
```

or invoke the script directly:

```sh
python git-grep.py [-i] [pattern]
```

| Option | Effect |
| ------ | ------ |
| `-i`, `--ignore-case` | Start with case-insensitive matching. |

### Notes & caveats

- Results are **not** live — `git grep` runs when you press `Enter`, so it stays
  snappy on big repos. Re-run anytime by editing the pattern.
- Uses `git grep -n -I -z`, so binary files are skipped and the output parses
  cleanly even when paths contain colons. "No matches" (`git grep` exit `1`) is
  normal; a real error (e.g. an invalid regex) is reported in the footer.
- Same terminal behavior as git-open: alternate screen over `stderr`, raw-mode
  `stdin`, and it steps aside while your editor runs.

Exit status: `0` you quit normally (whether or not you opened anything) ·
`1` not inside a Git repository, or not an interactive terminal.

**Requirements:** Python 3.11+ (standard library only), Git on `PATH`, and the
`branch_tui.py`, `editor_config.py`, and `editor_ide.py` modules beside it (the
latter two shared with `git-open.py` and `git-diff.py`).

---

## `git-diff.py`

A full-screen, **interactive front end for `git diff`**: every changed line
gathered into a collapsible tree of files, then hit `Enter` to jump straight to
that line in your editor. Open as many as you like — the browser stays put until
you quit. **Modal**, like vim, and a sibling of git-grep: where git-grep starts
from a pattern you type, git-diff starts from the diff itself and lets you
**search within it**.

Added lines show in green with a leading `+`, removed lines in red with a `-`,
and the surrounding context **dimmed**; every row is tagged with the line it
opens at and a `:N` jump number. You land in **BROWSE** mode on the full diff.

Whatever you put after the command is passed **straight through to `git diff`**,
so the usual selectors work:

```sh
git-diff                 # unstaged changes (plain `git diff`)
git-diff --staged        # what's staged for the next commit
git-diff HEAD            # everything uncommitted (staged + unstaged)
git-diff main            # working tree vs the `main` branch
git-diff v1.0 v1.1       # between two commits
git-diff -- src/         # limit to a path
```

**BROWSE mode:**

| Key | Action |
| --- | ------ |
| `j` / `k` or `↑` / `↓` | move the highlight cursor |
| `g` / `G` | jump to the top / bottom |
| `h` / `←` | hop up to the parent folder / file |
| `l` / `→` | expand / step into a folder or file |
| `Enter` | open the changed line under the cursor **at its position** (on a folder/file row, fold it) |
| `r` | **re-run** `git diff` and refresh (handy after editing or staging), keeping your place |
| `/` | **refine**: filter the current lines with a sub-grep (push a level onto the stack) |
| `<` | **back up** one level (pop the last filter) |
| `\` | **start fresh**: drop every filter, show the whole diff again |
| `0`–`9` | set the diff **context** to N lines (re-runs `git diff -U N`; `0` = only the changed lines) |
| `+` / `-` | widen / narrow that context (`+` goes past 9) |
| `:N` | jump the cursor to line number **N** — the number at the start of each row; `:` again starts a new number, `Enter`/`Esc`/any move closes the prompt |
| `Tab` | toggle **case-insensitive** filter matching |
| `q` | quit (`Esc` only navigates — it never quits) |

**FILTER mode** (a sub-grep over the current lines; reached with `/` from BROWSE):

| Key | Action |
| --- | ------ |
| *type* | a pattern that narrows the visible lines **live**, matched against the file path **and** the line text |
| `!pattern` | **exclude**: keep the lines that do *not* match |
| `Enter` | push this filter onto the stack |
| `Esc` | cancel without pushing |
| `↑` / `↓` | move through the filtered lines |

### Searching the diff

Filters **stack**, just like git-grep, so you can drill down in steps a single
regex can't express: `/` `TODO` to keep the lines mentioning it, then `/`
`!_test.` to drop the test files — the header shows the whole stack as a
breadcrumb (`working tree  +TODO  -_test.`). `<` pops a level, `\` clears them
all. Each filter matches the **path and the line text** together.

The diff's own **context lines are part of the searchable set**, so a filter can
match on something that merely sits *near* a change. Press `0`–`9` (or `+` / `-`)
to control how many context lines `git diff` hands over: this re-runs `git diff
-U N`, so widening genuinely pulls *more* surrounding lines in for searching. The
header shows the current width as `·U N`. `r` re-runs the diff and re-applies
every filter, so after editing or staging you see the same view, refreshed.

Changed lines are grouped under their file, and files nest in a **folder tree**
split on `/`, so changes in `src/app/main.c` and `src/lib/parse.c` sit under a
`src/` folder you can collapse. Everything starts expanded, with each file
showing its line count. `git diff` is run at the **repository root**, so it works
from any subdirectory and opens files by their full path.

The editor — and **how to open it at a line** — come from the same
`.git-open-config` that git-open and git-grep use; see
[Configuring the editor](#configuring-the-editor) above. A changed line opens at
its **new-side position**; a *removed* line, which no longer exists, opens at the
nearest surviving line.

### Usage

Run it from inside the repository:

```sh
# browse the unstaged diff
git-diff

# browse what's staged, or a diff against a branch
git-diff --staged
git-diff main
```

or invoke the script directly:

```sh
python git-diff.py [git diff args...]
```

### Notes & caveats

- Arguments are forwarded verbatim to `git diff`, so anything it accepts works;
  an invalid revision is reported in the footer rather than crashing.
- **Deleted files** and **binary** changes are shown but have nowhere to open to,
  so `Enter` reports "nothing to open here" on those rows.
- When diffing **two arbitrary commits** (e.g. `git-diff v1.0 v1.1`), the new
  side isn't your working tree, so the opened file may not line up exactly — the
  common cases (working tree, `--staged`, `HEAD`, a branch) all open cleanly.
- Same terminal behavior as git-open / git-grep: alternate screen over `stderr`,
  raw-mode `stdin`, and it steps aside while a terminal editor runs (but not for
  the IDE-terminal hand-offs, which return at once).

Exit status: `0` you quit normally (whether or not you opened anything) ·
`1` not inside a Git repository, or not an interactive terminal.

**Requirements:** Python 3.11+ (standard library only), Git on `PATH`, and the
`branch_tui.py`, `editor_config.py`, and `editor_ide.py` modules beside it (all
shared with `git-open.py` and `git-grep.py`).

---

## `update-scripts.py`

Updates **these scripts in place**, by pulling from git. Since the scripts run
straight out of their checkout — the folder holding them is on your `PATH`, with
no separate install step — updating them is just bringing that checkout up to
date:

```sh
update-scripts
```

It finds the repository from **its own location** rather than a hardcoded path,
so it updates whichever checkout you ran it from, wherever that lives on
whichever machine. Afterwards it reports the commits it applied and a diffstat,
so you can see what actually changed.

### Usage

```sh
update-scripts [options]
```

or invoke the script directly:

```sh
python update-scripts.py [options]
```

| Option | Effect |
| ------ | ------ |
| `-n`, `--dry-run` | Fetch and show what *would* be applied, changing nothing. |
| `-s`, `--stash` | Set uncommitted changes aside for the update, then restore them. |
| `-q`, `--quiet` | Print only the summary line. |
| `-V`, `--version` | Print the version and exit. |

```sh
update-scripts             # fast-forward and report what changed
update-scripts --dry-run   # peek at what's waiting, change nothing
update-scripts --stash     # update even with local edits in progress
```

### Notes & caveats

- **It's an update button, not a merge tool.** The update is **fast-forward
  only**; nothing is ever pushed and no branch is ever switched. If your branch
  has diverged — local commits *and* upstream commits — it stops and prints the
  `git rebase` you'd need, rather than quietly rebasing or writing a merge
  commit on your behalf.
- **Uncommitted changes stop it before anything is touched**, and are listed so
  you can deal with them. `--stash` overrides that; the stash includes untracked
  files, so an incoming commit can't collide with one mid-update.
- **A conflicting restore never loses work.** If `--stash` updates cleanly but
  the changes won't reapply, it says so, names the conflict, and leaves them in
  the stash — `git stash pop` recovers them. The exit status is `1` in that case
  even though the update itself succeeded.
- **Updating the running script is safe.** Python reads the source fully before
  executing, so `update-scripts` replacing its own file mid-run can't affect the
  run in progress; the new version takes effect next time.
- Gitignored files (`__pycache__/`, `.git-open-config`, …) don't count as
  uncommitted changes, so build artifacts and local editor config never block an
  update.

Exit status: `0` updated, or already up to date · `1` uncommitted changes,
diverged branch, no upstream, not a repo, or git failed · `2` usage error (bad
or missing arguments).

**Requirements:** Python 3.6+ (standard library only; no dependencies) and Git
on `PATH`.
