# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-21

Alpha for Windows.

- Job gateway: `agent-router run` sends a coding job to one of your AI coding subscriptions
  (OpenCode, Kimi Code, Cline, Claude Code) and returns the result.
- Routing by tier, priority and remaining limits, with pacing across usage windows.
- Usage tracking across limit windows, with manual calibration.
- Optional tier classification with local or cloud models.
- Web dashboard, Claude Code status line and JSONL/CSV export.
- Delegation skills for Claude Code, OpenCode, Kimi Code and Cline.
- MCP server for orchestrator agents to submit and track jobs.
- Guided setup with `agent-router init`, ready-made configs and calibration guidance.
- Installable from PyPI as `open-agent-router`; the command is `agent-router`.
- Experimental Linux and macOS support; Windows-only autostart.
- Troubleshooting guide for common gateway, harness, limit and approval failures.
