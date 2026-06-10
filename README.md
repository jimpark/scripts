# scripts

A small collection of standalone utility scripts. Each is self-contained — grab
the one you need and run it. Details for each are below.

| Script | What it does |
| ------ | ------------ |
| [`backport.py`](#backportpy) | Cherry-pick one author's commits from a source branch onto a target branch. |
| [`baseconv.py`](#baseconvpy) | Convert a value between binary, decimal, octal, hex, and base64. |
| [`configure-vscode-bedrock.py`](#configure-vscode-bedrockpy) | Point the Claude Code VS Code extension at AWS Bedrock, safely. |
| [`html-info.py`](#html-infopy) | Print useful basic information about an HTML, XML, or XHTML document. |
| [`prune-branches.py`](#prune-branchespy) | Delete local Git branches that no longer exist on a remote. |
| [`rapid-mlx-copilot.py`](#rapid-mlx-copilotpy) | Pick a local MLX model your Mac can run and launch the GitHub Copilot CLI against it. |

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

## `prune-branches.py`

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
python prune-branches.py
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
python prune-branches.py --into main
python prune-branches.py --remote upstream
python prune-branches.py --force --yes
```

Run `python prune-branches.py --help` for the full reference.

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

A small bash wrapper, [`rapid-mlx-copilot`](#rapid-mlx-copilotpy), runs the
script via `uv` from any directory (the macOS counterpart to the `.cmd`
wrappers; no Windows wrapper since this is Mac-only). Put the `scripts/` folder
on your `PATH` and just run:

```sh
rapid-mlx-copilot                 # pick a model, then launch Copilot
```

or invoke the script directly:

```sh
uv run rapid-mlx-copilot.py [options] [-- copilot args…]
```

| Option | Effect |
| ------ | ------ |
| `-s`, `--serve-only` | Start/reuse the chosen model and wait — don't launch Copilot (attach from another terminal). |
| `--context <N>` | Context length used to size the KV cache in the RAM estimate (e.g. `16384` or `16k`). Default: `16384`. |
| `--budget <X>` | Cap usable RAM for the "runnable" filter: a number in GB (e.g. `24`) or a percentage of total (e.g. `80%`). Default: all of this machine's RAM. |
| `-h`, `--help` | Show help. |

Any other arguments (or anything after `--`) are forwarded to `copilot`.

```sh
# size the runnable filter for a longer context and a conservative RAM budget
rapid-mlx-copilot --context 32k --budget 80%

# just start/keep a model serving and attach Copilot from elsewhere later
rapid-mlx-copilot --serve-only
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
