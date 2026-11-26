# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- A route whose harness reports its model as unavailable (OpenCode's
  `provider.no-route: Model unavailable: <provider>/<model>`) is taken out of rotation after a
  single failure for `tasks.model_unavailable_cooldown_s` (default 1800 s, new config key);
  `advice` and the dashboard show `model unavailable, retry after <local time>`, a
  gateway-chosen job is retried on the next route, and a caller-pinned route fails without a
  retry.
- A usage read that stops returning a mapped field, or fails with `malformed_response`/`no_data`
  three times in a row, is surfaced as usage-source drift: the snapshot `usage` object gains
  `missing_fields` and `format_changed`, the reader publishes `usage.fields_missing` when the
  missing set changes, and the dashboard and `doctor` warn with the remediation
  (`update agent-router or switch the pool to mode = "manual" and calibrate`).
- The Limits page charts spend per pool over the last 24 hours and seven days from the journal,
  with USD or token units, estimate markers, accessible tables and responsive inline SVG.
- The dashboard's read-only Settings page shows pools, windows, routes, caller mappings and
  configuration-related doctor findings with hints, without exposing tokens or environment values.
- The dashboard has a New job page at `/jobs/new`, linked from the Jobs page: it submits a job
  with working directory, mode, optional tier (empty picks the gateway default), prompt and
  optional timeout. The form works without JavaScript, keeps the entered values next to per-field
  validation errors, redirects to the job page on success and leaves the normal approval flow
  untouched.
- Kimi Code jobs count the CPU time of the harness process tree and the files changed in the
  working directory as activity, so a harness that stays silent while it works is no longer
  stopped as idle. A job stopped as idle names the last sign of life it saw
  (`executor showed no progress for 600 s; last activity: files 640 s ago`).

### Changed

- The test suite runs without the harness CLIs installed, and the Ubuntu CI job verifies this;
  tests that genuinely need `git` declare that dependency.
- The MCP `list_jobs` tool returns compact job summaries by default; `verbose=true` returns the
  full job records.
- The installation guide documents the write-mode flags and outside-directory controls of each
  supported harness, including OpenCode's `external_directory` permission.
- The `claude-cli/sonnet` route is enabled by default when Claude Code is installed.
- Claude Code jobs use `stream-json`, so their output resets the idle timeout while the job runs.
  Write jobs use `bypassPermissions` instead of silently denying commands, and both modes load
  only project settings with strict MCP configuration instead of the user's Claude setup.

### Fixed

- Approval cards on the Limits page keep Approve and Deny side by side in one responsive row.
- The result text of an OpenCode job keeps every answer that ended a turn, so a short follow-up
  after a background command no longer replaces the main answer.
- SystemOne classifier requests keep the Ollama model loaded for 30 minutes, matching Ollama chat
  requests.
- The New job page shows the Jobs disabled notice and omits the submit form when the job
  dispatcher is not running.
- Claude Code plan usage remains available when the CLI fails after a valid report; the warning is
  shown by `agent-router status`, `doctor` and the dashboard pool card.
- `agent-router purge` retries directory removals that failed during an earlier run and also
  sweeps orphaned `jobs/<job-id>/` directories older than the retention cutoff.
- `tasks.allowed_roots` and `tasks.denied_roots` entries must be non-empty absolute paths after
  `~` expansion, so `~/...` stays valid and portable while relative or empty entries are config
  errors. Harness routes without a non-empty `pool`, `backend` and `model` are also rejected.
- Kimi Code jobs record input and output tokens from the session logs, including subagent usage.
- Claude Code plan limits are detected from result errors and rejected rate-limit events, including
  the reset time. Input token usage now includes cache creation and cache reads.
- Fixed windows accept an optional `max_age` span (default: half the window duration, capped at
  72 h); an observation older than `max_age` — a manual calibration as much as a usage read —
  is reported as `no data: observation older than max age`, while a passed scheduled reset still
  yields a fresh zero-usage window.
- The MCP `rate_job` tool returns only the compact fields `id`, `status` and `rating` instead of
  the full job.

## [0.3.0] - 2026-11-22

### Changed

- Route choice ranks a pool by its urgency, `100 * lag / max(1 - expected, 0.02)`: a pool
  with an unspent allowance and a near reset now outranks a pool with a larger lag and more
  time left. `weight` is an offset on this score, which is twice the lag halfway through a
  window, so weights tuned for the old score may need doubling. Status and decision JSON
  gain an `urgency` field next to `lag`.

### Added

- Each release tag also creates a GitHub release with the changelog notes, the wheel and the
  source archive, and a signed build provenance attestation that `gh attestation verify` checks.
- `[usage]` and `[pools.usage]` config tables and the `agent_router.usage_sources` entry point
  group for sources that read a pool's usage from the vendor.
- `opencode-go` usage source: reads the OpenCode Go plan's rolling, weekly and monthly meters
  from the vendor usage endpoint; the API key comes from the environment variable named by
  `api_key_env` and is never stored or logged.
- `claude-code` usage source: reads the Claude plan's session and weekly figures from the
  `/usage` command of the signed-in Claude Code CLI, without calling the model or touching its
  credentials; `[pools.usage] executable` sets the CLI path, and `doctor` warns when the CLI is
  missing or not signed in.
- Usage sources are read every `[usage].interval` in `auto` mode and on demand with
  `agent-router usage refresh`, `POST /api/v1/usage/refresh` or the "Refresh limits" button;
  each read publishes `usage.read` or `usage.read_failed`.
- The snapshot reports where each window's figure comes from (`figure_source`) and the read
  health of a pool's usage source (`usage`); the dashboard, `agent-router status` and
  `agent-router doctor` show the source and warn when reads are stale or failing.

## [0.2.0] - 2026-11-21

### Added

- `storage.retention_days` and `agent-router purge`: delete terminal jobs older than the limit
  with their prompts, results and attempt logs; spend records are kept, `--dry-run` only
  reports the count.
- `doctor` warns about a route whose `counter` spend the pool cannot see — the pool counter does
  not list the route's harness, or the pool has no counter — and suggests `spend = "journal"`.

### Changed

- On Windows each attempt runs in its own job object: every process the harness starts stops
  with the attempt and with the gateway. When the job object cannot be used, the `taskkill`
  fallback applies.
- OpenCode jobs run with `opencode run --standalone`, so a stopped job does not keep running in
  the OpenCode background service.
- Client commands connect with a short timeout, so an unreachable gateway is reported at once.
- Job timeouts: `tasks.default_timeout_s` is now a hard cap (default 14400 s), and a job is
  stopped when the harness prints nothing to stdout or stderr for `tasks.idle_timeout_s`
  (default 600 s; `0` disables the check). An explicit job or route `timeout_s` replaces the
  hard cap, but the idle limit still applies. Claude Code prints its JSON only at the end, so
  its jobs get only the hard cap.

### Fixed

- Security: on filesystems that ignore case or Unicode normalization (the macOS default), a job
  directory spelled differently from a `tasks.denied_roots` entry was accepted, and one spelled
  differently from a `tasks.allowed_roots` entry was rejected. Roots are now also matched by the
  directory's identity on the volume, and write jobs in one repository spelled two ways no longer
  run at the same time.
- The gateway rejects a request body over the size limit with `413 body_too_large` before
  parsing it.
- `AGENT_ROUTER_URL` with a scheme other than `http` or `https` is a configuration error.
- A token with line breaks or characters outside latin-1 is reported as an error instead of a
  traceback.
- Classifier requests to loopback models no longer go through the environment proxy.
- A job whose directory is replaced by a link after intake fails with `cwd_changed` instead of
  running elsewhere.
- Log rotation recovers from flush, close and replace failures.
- CSV export: a text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return now
  gets a leading `'`, so spreadsheet apps do not run it as a formula.

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

[Unreleased]: https://github.com/kdenischenko/open-agent-router/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/kdenischenko/open-agent-router/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kdenischenko/open-agent-router/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kdenischenko/open-agent-router/releases/tag/v0.1.0
