# scripts

A small collection of standalone utility scripts. Each is self-contained — grab
the one you need and run it. Details for each are below.

| Script | What it does |
| ------ | ------------ |
| [`baseconv.py`](#baseconvpy) | Convert a value between binary, decimal, octal, hex, and base64. |
| [`prune-branches.py`](#prune-branchespy) | Delete local Git branches that no longer exist on a remote. |

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

## `prune-branches.py`

Deletes local Git branches that no longer exist on a remote — handy after remote
branches have been merged and deleted (e.g. squash-merged PRs), leaving stale
local copies behind.

It runs `git fetch --prune`, compares your local branches against the branches
still present on the remote, lists any that have no remote counterpart, and
deletes them **only after you confirm** with `y`. The branch you currently have
checked out is always skipped.

### Usage

Run it from inside the repository you want to clean up:

```sh
python prune-branches.py
```

You'll see the list of branches to be deleted and a `y/n` prompt; anything other
than `y` cancels without changing anything.

| Option | Effect |
| ------ | ------ |
| `--remote <name>` | Compare against this remote instead of `origin`. |
| `--force` | Delete with `git branch -D` (force) instead of the default `-d` (merged-only). |
| `--yes` | Skip the confirmation prompt (non-interactive; use with care). |

```sh
python prune-branches.py --remote upstream
python prune-branches.py --force --yes
```

Run `python prune-branches.py --help` for the full reference.

### Notes & caveats

- By default deletion uses `git branch -d`, which **refuses to delete branches
  that aren't fully merged** — so it won't discard unmerged work. A branch whose
  remote was squash- or rebase-merged then deleted looks unmerged locally and
  will be kept; pass `--force` to delete those (this discards their commits, so
  review the printed list first).
- "Exists on remote" is matched by branch name against `<remote>/*` (the
  `<remote>/HEAD` symref is ignored), so a local branch is kept as long as a
  same-named branch exists on the remote.
- If a deletion fails, the remaining branches are still processed and the script
  exits with a non-zero status.
- **Requirements:** Python 3.6+ (standard library only) and Git on `PATH`.
