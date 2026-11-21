# Roadmap

Plans, not promises. Order is rough priority. Ideas and help are welcome — open an issue first.

## Platforms

- **Linux and macOS.** The test suite runs in CI on `ubuntu-latest` and `macos-latest`, and
  the gateway home is created with `0700`. Still needed: a live check on a real system,
  case-insensitive path checks on macOS, autostart with `systemd --user` and `launchd`,
  a bash variant of the skill install steps.

## Reliability

- **Liveness checks instead of a fixed job timeout.** A long job that still makes progress should
  not be killed; a silent, stuck harness should be stopped early.
- **Orphaned child processes on Windows.** Run each attempt in a Job Object so that children that
  outlive the harness are stopped and reported.
- **Live check of the Claude Code adapter.** It is covered by tests with recorded output only.

## Data

- **Safer CSV export** for spreadsheet apps (no formula injection).

## Routing

- **OpenAI-compatible proxy.** Let harnesses that speak the OpenAI API send requests through the
  gateway, so that direct API keys and pay-as-you-go providers become routes too, with spend
  limits in dollars.
- **Provider integrations** for OpenCode and Cline on top of the proxy.

## Code

- Split the largest modules (`dispatcher.py`, `config.py`, `classifier.py`, `cli.py`) into
  smaller ones along their responsibilities.
