# Contributing

Thanks for your interest. agent-router is a small project maintained by one person, so please
open an issue before a large change — it saves both of us time.

## Setup

Windows, Python 3.12 and [uv](https://docs.astral.sh/uv/):

```
git clone https://github.com/kdenischenko/open-agent-router.git
cd open-agent-router
uv sync
```

## Gates

Every commit must pass:

```
uv run ruff check
uv run ruff format --check
uv run pytest -q
```

CI runs the same commands on Windows, Linux and macOS. Windows is required; Linux and
macOS are experimental and allowed to fail.

## Browser smoke tests

The Playwright suite is opt-in and offline. It uses a temporary gateway home and a fake clock,
never the real gateway home or clock. Install Chromium once, then run the smoke marker:

```
uv run --group smoke playwright install chromium
uv run --group smoke pytest -m smoke tests/test_web_smoke.py
```

Set `AGENT_ROUTER_SMOKE_SHOTS=<dir>` to keep the theme and mobile screenshots in that directory.
Without it, screenshots go to pytest's temporary directory.

## Guidelines

- **Tests first** for bug fixes: a test that fails for the stated reason, then the fix.
- **No new** `noqa`, `skip` or `xfail` without a reason in the pull request.
- **Tests do not depend on the real clock or the real gateway home directory**: use the clock
  fixtures and `AGENT_ROUTER_HOME` in a temporary directory.
- **Stable interfaces.** Error codes, event names, status values and JSON fields of the API are
  never renamed; new ones may be added.
- **Messages** are in English, start with a lowercase letter and have no trailing period:
  `unknown route: x`.
- **Docs follow behavior.** A change in behavior updates `README.md`, `docs/INSTALL.md` or
  `docs/DESIGN.md` in the same pull request if they describe it.
- **Comments explain what and why**, not the history of the change.
- **Nothing personal or secret** in the repository: no e-mail addresses, tokens, `.env` files, real
  paths with user names. `tests/test_hygiene.py` checks the obvious cases.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/) style: `fix(cli): ...`,
`feat(config): ...`, `docs: ...`, `test: ...`, `chore: ...`. One logical change per commit.

## Coding agents

Agents working in this repository follow [`AGENTS.md`](AGENTS.md). The same rules apply to their
pull requests as to human ones.

## Security

Please do not report vulnerabilities in public issues — see [`SECURITY.md`](SECURITY.md).
