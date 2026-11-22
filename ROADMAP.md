# Roadmap

Plans, not promises. Order is rough priority. Ideas and help are welcome — open an issue first.
Issues labelled
[good first issue](https://github.com/kdenischenko/open-agent-router/labels/good%20first%20issue)
and [help wanted](https://github.com/kdenischenko/open-agent-router/labels/help%20wanted) are a good
place to start.

## Platforms

- **Linux and macOS.** The test suite runs in CI on `ubuntu-latest` and `macos-latest`, and
  the gateway home is created with `0700`. Still needed: a live check on a real system,
  autostart with `systemd --user` and `launchd`, a bash variant of the skill install steps.

## Harnesses

- **Live check of the Claude Code adapter.** It is covered by tests with recorded output only.
- **More harnesses**, such as Codex CLI, and more pools, such as DeepSeek.
- **Automatic usage collection** for Claude, Kimi Code and Cline, so that pools need less manual
  calibration. The OpenCode Go plan is already read from its usage endpoint.

## Routing

- **OpenAI-compatible proxy.** Let harnesses that speak the OpenAI API send requests through the
  gateway, so that direct API keys and pay-as-you-go providers become routes too, with spend
  limits in dollars.
- **Provider integrations** for OpenCode and Cline on top of the proxy.

## Security

- **Restrict harness writes to the job directory** where the harness allows it.

## Code

- Keep splitting the largest modules (`dispatcher.py`, `config.py`, `cli.py`) into
  smaller ones along their responsibilities.
