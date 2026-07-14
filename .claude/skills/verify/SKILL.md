---
name: verify
description: How to run and drive this repo's TUI scripts (git-grep, git-diff, git-open, git-switch, delete-branch) for end-to-end verification.
---

# Verifying the TUI scripts

The interactive scripts (git-grep.py, git-diff.py, git-open.py, git-switch.py,
delete-branch.py) are raw-mode ANSI TUIs on stderr — they refuse to start
without a tty, so drive them through a pty (no tmux on this machine):

```python
import fcntl, os, pty, struct, termios
pid, fd = pty.fork()
if pid == 0:
    os.chdir("/Users/jim/scripts")
    os.execvp("python3", ["python3", "git-grep.py", "def "])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
# os.write(fd, b"j"/b"k"/b"q"...), read with select until quiet
```

Key facts that make captures easy:

- Every full repaint starts with HOME (`\x1b[H`) and ends with `\x1b[J`
  (CLEAR_EOS); split on `\x1b[H` to count frames or grab the last screen.
- The cursor row is the only line containing REVERSE (`\x1b[7m`).
- The run loops coalesce buffered input: a burst of keys written in one
  `os.write` yields ONE frame reflecting the final state — expected, not a bug.
- Safe keys: j/k/g/G/n/p, arrows, `/pattern\r` (filter), `q` quits.
  AVOID Enter in git-switch/delete-branch (switches/deletes branches!) and
  Enter on a match row in git-grep/git-diff/git-open (spawns the editor).
- Exit status 0 on `q`; scripts print notices after leaving the alt screen.

A reusable driver from a past session (frame counting, cursor tracking,
key bursts): scratchpad `drive_tui.py` — recreate from this recipe if gone.

Non-TUI surfaces: git-prune.py and the others are plain CLIs; run them with
`--help` / dry paths directly. Unit tests live at `python3 tests/test_splice.py`
(pure logic, no tty needed) — but that's CI's job, not verification.
