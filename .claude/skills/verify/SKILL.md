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

- Output is line-diffed (branch_tui.FramePainter): only the FIRST paint, a
  resize, post-editor resume, and Ctrl-L emit a full frame (HOME `\x1b[H` …
  `\x1b[J`). Everything else is per-line updates addressed with `\x1b[row;1H`
  + text + `\x1b[K`, and an unchanged frame writes ZERO bytes (e.g. a move
  that clamps at the top/bottom). To assert on what the user sees, replay the
  byte stream through a small ANSI screen emulator (cells + reverse flag,
  handling CUP/EL/ED/SGR/CR/LF) — a past session's `ansi_screen.py` +
  `drive_diffpaint.py` in its scratchpad did exactly this.
- The cursor row is the only line containing REVERSE (`\x1b[7m`).
- The run loops coalesce buffered input: a burst of keys written in one
  `os.write` yields ONE repaint reflecting the final state — expected, not
  a bug. Ctrl-L forces a full repaint.
- Safe keys: j/k/g/G/n/p, arrows, `/pattern\r` (filter), `q` quits.
  AVOID Enter in git-switch/delete-branch (switches/deletes branches!) and
  Enter on a match row in git-grep/git-diff/git-open (spawns the editor).
- Exit status 0 on `q`; scripts print notices after leaving the alt screen.

Non-TUI surfaces: git-prune.py and the others are plain CLIs; run them with
`--help` / dry paths directly. Unit tests live at `python3 tests/test_splice.py`
(pure logic, no tty needed) — but that's CI's job, not verification.
