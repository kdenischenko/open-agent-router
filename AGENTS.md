# agent-router — rules for coding agents

## What goes into the repository

Commit only code, tests and user-facing documentation (`README.md`, `docs/`, `integrations/`).
Working notes, plans, drafts and generated reports are not part of the repository.

Never commit personal data (e-mail addresses, private remotes, subscription details), tokens or
`.env` files.

## Gates before every commit

```
uv run ruff check
uv run ruff format --check
uv run pytest -q
```

## How to change code

Follow the guidelines in [`CONTRIBUTING.md`](CONTRIBUTING.md). The ones agents miss most often:

- A bug fix starts with a test that fails for the stated reason.
- Error codes, event names, status values and JSON fields of the API are never renamed.
- Messages are in English, start lowercase and have no trailing period.
- Comments explain what and why, never the history of the change.
- A change in behavior updates `README.md`, `docs/INSTALL.md` or `docs/DESIGN.md` in the same
  commit if they describe it.
- Tests never touch the real clock or the real gateway home directory.

The project is tested on Windows; keep paths and process handling working there.
