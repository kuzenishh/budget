# budget

Your coding agent session ate a few million tokens. You have no idea where.

`budget` reads the `.jsonl` transcript Claude Code already writes to disk and
tells you: how much of that was cache reads vs fresh input, which tool ate
the most output, which file got read five times in one session, and where
the agent got stuck retrying the same thing.

No estimates guessed from character counts — the token numbers come straight
from the API usage the transcript already logs.

```
$ budget scan session.jsonl

== tokens (from real API usage, not estimated) ==
  input (fresh):              900
  input (cache read): 108 463 385
  input (cache write):  2 390 322
  output:                 577 218
  total input:        110 854 607

== by tool (output size, not counting the model's own tokens) ==
  Bash             93 calls  ~  49 816 tok
  Read             21 calls  ~  11 599 tok  (1 errors)
  ...

== files read more than once ==
  6x  window_fsm.py
  5x  strategy.py
  4x  main.py

== possible retry loops (same tool + same input, repeated) ==
  PowerShell  x3  (first at line 812)  {'command': 'Get-Content ...'}
```

## Install

```bash
curl -O https://raw.githubusercontent.com/kuzenishh/budget/main/budget.py
chmod +x budget.py
```

Python 3.8+. No packages to install.

## Use

```bash
budget scan session.jsonl              # full breakdown
budget scan session.jsonl --last 50    # only the last 50 assistant turns
budget top session.jsonl -n 15         # the 15 biggest individual tool outputs
```

Find your session files under `~/.claude/projects/<project>/*.jsonl`
(Windows: `%USERPROFILE%\.claude\projects\...`).

## What it's actually telling you

- **cache read** is the number that usually dwarfs everything else — it's the
  full context getting re-sent every turn. If that's most of your total,
  the fix is a shorter session, not a smaller prompt.
- **by tool** is which tool's *output* filled the context, not which tool
  the model spent time thinking about. A `Bash` command that dumps 40KB of
  log output costs more than ten short `Edit` calls combined.
- **files read more than once** usually means the agent forgot it already
  read that file, or kept re-reading it to re-check something instead of
  trusting its own memory of it.
- **retry loops** are the same tool called with the same input three or more
  times in a short window — almost always a command that kept failing the
  same way.

## What it doesn't do

No daemon, no API calls, no upload anywhere. It reads a file on your disk
and prints numbers. That's it.

## Why

Session budgets got a lot harder to reason about once cache read started
making up 90%+ of total input on a long session. I wanted a plain answer to
"where did it go" before the next session, not a guess.

## License

MIT
