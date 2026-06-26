# scripts

A small collection of standalone utility scripts. Each is self-contained — grab
the one you need and run it. (The one exception: [`switch-branch.py`](#switch-branchpy)
and [`delete-branch.py`](#delete-branchpy) share their TUI engine through a
neighbouring `branch_tui.py` module — keep the three files together.) Details
for each are below.

| Script | What it does |
| ------ | ------------ |
| [`backport.py`](#backportpy) | Cherry-pick one author's commits from a source branch onto a target branch. |
| [`baseconv.py`](#baseconvpy) | Convert a value between binary, decimal, octal, hex, and base64. |
| [`bedrock-copilot.py`](#bedrock-copilotpy) | Launch the GitHub Copilot CLI against a model on AWS Bedrock, with model + effort pickers. |
| [`configure-vscode-bedrock.py`](#configure-vscode-bedrockpy) | Point the Claude Code VS Code extension at AWS Bedrock, safely. |
| [`cpp-unicode-escapes.py`](#cpp-unicode-escapespy) | Rewrite misused `\xNNNN` escapes as proper `\uNNNN` in C++ string/char literals. |
| [`cpp-unicode-literals.py`](#cpp-unicode-literalspy) | Classify C++ string literals by encoding type and migrate narrow/wide literals to `u8`/`u`. |
| [`delete-branch.py`](#delete-branchpy) | Interactively check off Git branches (local and remote) — even whole folders — and delete them. |
| [`docx-runs.py`](#docx-runspy) | Resolve and report the language of every text run in a `.docx`, with per-character script classification. |
| [`html-info.py`](#html-infopy) | Print useful basic information about an HTML, XML, or XHTML document. |
| [`prune-branches.py`](#prune-branchespy) | Delete local Git branches that no longer exist on a remote. |
| [`rapid-mlx-copilot.py`](#rapid-mlx-copilotpy) | Pick a local MLX model your Mac can run and launch the GitHub Copilot CLI against it. |
| [`rtf-runs.py`](#rtf-runspy) | Segment RTF body text into runs and report the language/character set of each. |
| [`switch-branch.py`](#switch-branchpy) | Interactive, vim-style Git branch switcher with a collapsible folder tree and remote branches. |
| [`unicode-clipboard.py`](#unicode-clipboardpy) | Copy Unicode characters to the clipboard by codepoint, so you can paste the untypeable. |
| [`unicode-info.py`](#unicode-infopy) | Fetch and display Unicode character information for a codepoint. |

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
prune-branches
```

or invoke the script directly:

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

## `cpp-unicode-literals.py`

Classifies every C++ **string literal** by its encoding type and migrates the
narrow and wide ones to their Unicode-typed spellings:

```
"..."      ->  u8"..."      (narrow  char[]    ->  char8_t[] in C++20)
L"..."     ->   u"..."      (wide    wchar_t[] ->  char16_t[])
R"(...)"   ->  u8R"(...)"   (the matching raw-string forms)
LR"(...)"  ->   uR"(...)"
```

Rather than a regex, it parses each translation unit with **tree-sitter**'s C++
grammar and rewrites only the opening prefix of real literal nodes — so a `"`
inside a comment, inside a raw-string body, or after an escaped backslash is
left alone *by construction*. Already-Unicode literals (`u8`, `u`, `U` and their
raw forms) and character literals (`'x'`, `L'x'`) are classified but never
touched.

### A note on soundness

tree-sitter is a **parser, not a type checker**: it knows exactly *what kind* of
literal each token is, but not how the literal is *used*. The rewrites above are
**type-changing, not value-preserving**:

- `narrow -> u8` turns `char[]` into `char8_t[]` (C++20), so anything passing the
  literal where a `const char*` is expected stops compiling;
- `L -> u` turns `wchar_t[]` into `char16_t[]` — a different type, and a
  different width on Linux/macOS where `wchar_t` is 32-bit.

So treat every rewrite as a **reviewed suggestion**, not a guaranteed-safe edit.
Run `--dry-run` first, use `--report` to inventory every literal by type (the
plain narrow ones especially) and decide which truly belong in `char8_t`, and
lean on your version-control diff as the final safety net.

### Usage

```sh
cpp-unicode-literals PATH [PATH ...] [options]
```

or invoke the script directly:

```sh
uv run cpp-unicode-literals.py PATH [PATH ...] [options]
```

- Each `PATH` may be a file or a directory; directories are walked recursively
  (with `.git`/`.svn`/`.hg` pruned), and output is sorted by path.
- By default it **edits files in place**; `--dry-run` previews and `--report`
  only classifies (neither writes anything).

| Option | Effect |
| ------ | ------ |
| `--dry-run` | Preview the rewrites without writing any files. |
| `--report` | Don't rewrite; inventory every literal grouped by encoding type. |
| `--json` | With `--report`, emit JSON (one record per literal) instead of a listing. |
| `--ext <list>` | Comma-separated extensions to scan when given a directory (default: common C/C++ extensions). |
| `-q`, `--quiet` | Suppress the per-literal lines; show only the summaries. |
| `-j`, `--jobs <N>` | Number of worker threads (default: scales with CPU count; `1` disables parallelism). |

```sh
# rewrite one file in place
uv run cpp-unicode-literals.py src/foo.cpp

# recurse directories, in place
uv run cpp-unicode-literals.py src/ include/

# preview only, write nothing
uv run cpp-unicode-literals.py src/ --dry-run

# inventory every literal by type, for review
uv run cpp-unicode-literals.py src/ --report

# machine-readable inventory for scripting
uv run cpp-unicode-literals.py src/ --report --json
```

Run `uv run cpp-unicode-literals.py --help` for the full reference.

### Notes & caveats

- **The default rewrites your files.** Since the conversions are type-changing
  (see *A note on soundness*), your **version-control diff is the safety net** —
  review it before committing, and prefer `--dry-run` / `--report` first.
- **Adjacent literals are each rewritten independently**, so `"ab" "cd"` becomes
  `u8"ab" u8"cd"`. Mixed-prefix concatenation (e.g. `L"a" "b"`) is ill-formed
  C++ regardless.
- The scan is **idempotent**: re-running finds the already-`u8`/`u` literals and
  makes no further changes.
- **Requirements:** Python 3.9+ and the `tree-sitter` + `tree-sitter-cpp`
  packages, declared as inline script dependencies and installed automatically
  when run via [`uv`](https://docs.astral.sh/uv) (`uv run cpp-unicode-literals.py`,
  or the bare `cpp-unicode-literals` wrapper).

---

## `switch-branch.py`

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
switch-branch [options]
```

or invoke the script directly:

```sh
python switch-branch.py [options]
```

| Option | Effect |
| ------ | ------ |
| `-r`, `--remotes` | Start with remote branches already included. |
| `--no-color` | Disable colored output (also honors `NO_COLOR`). |

Run `python switch-branch.py --help` for the full key reference.

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

The same vim-style picker as [`switch-branch.py`](#switch-branchpy), but instead
of switching you **check off** as many branches as you like — local *and*
remote, or whole folders — and delete them in one pass. It shares its entire
navigation engine with `switch-branch.py` (the folder tree, regex filter,
remotes toggle, and all the keys behave identically).

The differences are the checkboxes and what `Enter` does:

| Key | Action |
| --- | ------ |
| `Space` | check / uncheck the branch — or the **whole folder** — under the cursor |
| `Enter` | delete everything that's checked (after a confirmation) |
| `F` | toggle **force**: `git branch -D` instead of the safe `-d` |
| `j`/`k`, `g`/`G`, `h`/`l`, `/`, `Tab`, `q` | exactly as in `switch-branch.py` |

Checking a folder checks every branch beneath it; a `[~]` box means only *some*
of a folder's branches are checked. The branch you're currently on is
**protected** — it has no checkbox, since Git won't delete the branch you're
standing on.

### Safety

- **Nothing is deleted from the TUI.** When you press `Enter` the picker closes
  and prints exactly what will go — **local** deletions and **remote** ones
  (`git push <remote> --delete`, which updates the shared remote for everyone)
  listed separately — then asks for a single `y/N`.
- **Unmerged branches are refused by default.** Local deletes use `git branch
  -d`, which won't drop a branch whose commits aren't merged; press `F` to force
  (`-D`) when you really mean to discard them. Remote deletions are always
  forced — that's how `git push --delete` works.

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

Exit status: `0` deletions ran, or you quit / aborted without deleting ·
`1` not inside a Git repository, not an interactive terminal, or one or more
deletions failed (e.g. an unmerged branch refused without `--force`).

**Requirements:** Python 3.6+ (standard library only; no dependencies), Git on
`PATH`, and the `branch_tui.py` module beside it (shared with `switch-branch.py`).
