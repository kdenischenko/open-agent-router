# Security

agent-router is a **single-user, local tool**, built and tested on Windows (Linux and macOS
are experimental). It runs AI coding harnesses (OpenCode,
Kimi Code, Cline, Claude Code) on your machine with your accounts. This page describes what it
protects against, what it does not, and how to report a vulnerability.

## Threat model

### What it is

- One person, one machine. The gateway (`agent-router serve`) listens on `127.0.0.1` only; the
  port comes from the config or `serve --port`, the address is not configurable. Requests with a
  foreign `Host` header are rejected, which blocks DNS rebinding from web pages.
- The API needs a bearer token; only `/healthz`, the login page and static files are open.
  `init` (or the first `serve`) creates two random tokens in the gateway home directory:
  - `client.token` — submit, read, wait for, rate and cancel jobs, send status line signals.
    **This includes write-mode jobs**: whoever holds the client token can make a harness edit
    files (see below). Give it only to tools you would let run code on your machine.
  - `admin.token` — everything else: approve and deny confirmations, unblock pools, calibrate,
    dashboard login.

  Tokens are compared in constant time. There is no rate limit on failed attempts; the tokens are
  long enough for that not to matter on loopback.
- The dashboard uses a signed `SameSite=Strict` session cookie and a CSRF token on every form
  that changes state.
- Plain HTTP, no TLS: traffic never leaves the machine, but any local process that can read
  loopback traffic can see tokens and prompts. Do not expose the port through a proxy or tunnel.
- Jobs may only run inside `[tasks] allowed_roots` and never inside `denied_roots`; an empty
  `allowed_roots` refuses every job. This check is about where a job starts: the harness can still
  write outside the job directory by an absolute path. Use the harness's own permissions for that
  (for example `external_directory` in OpenCode).
- A classifier model must use a loopback address, or `https` with a key in an environment
  variable named by `api_key_env`; the config is rejected otherwise. A remote classifier receives
  the job prompt (up to its first 3000 and last 1000 characters). The key is read at every
  request and never stored or logged.

### What it is not

- **Not a sandbox.** An executor is the harness itself, started as your user, with your
  environment variables, your files and your network. agent-router chooses *where* a job runs; it
  does not limit *what* the harness does there.
- **`--mode write` auto-approves tools.** To run unattended, the harness is started with its
  auto-approve option: `opencode --auto`, `cline --auto-approve true`,
  `claude --permission-mode acceptEdits`; Kimi Code runs non-interactively (`kimi -p`) with no one
  to ask. The harness can edit any file your user can edit and run commands, not only inside the
  job directory.
- **`--mode read` is a request, not a guarantee.** Where the harness has a read-only or plan mode,
  it is used; otherwise the prompt tells the executor not to change files. In a git repository the
  gateway compares the working tree before and after the job; if something changed, the job gets
  the violation `read_mode_files_changed` and the list of changed files. This detects mistakes; it
  does not prevent or undo them. Outside a git repository nothing is checked. If another process
  writes to the same repository during the job, the violation is reported too, even though the
  executor did not cause it.
- **The executor can read the gateway home directory**, including `admin.token`, the database and
  job logs. Do not delegate jobs to a harness or model you do not trust with your machine.
- **`confirm = true` is a guard against accidents**, not a security boundary: it stops a route
  until a human runs `agent-router approve`, but anyone with `admin.token` can approve. The
  `agent-router init` template sets it on every route that allows write mode; turning it off is
  your choice.
- **Prompts may be visible in the process list.** Short prompts are passed to some harnesses
  (Kimi Code, Cline) as a command-line argument; other local users can see them.
- **Plugins run inside the gateway.** Harnesses, counters, storage backends and sinks are loaded
  from Python entry points named in the config. Any installed package can provide one; install
  only packages you trust.
- **`AGENT_ROUTER_URL`** tells the CLI where the gateway is. The CLI sends the token there, so do
  not point it anywhere but your own gateway.
- **Executor output is data.** The result of a job is text produced by a model. An orchestrator
  that reads it must not follow instructions from it without checking, and must not approve
  `confirm` routes on its own.
- **Other local users.** Token files are created with owner-only permissions where the OS supports
  it; the rest of the gateway home directory is not. On a shared machine, protect the directory
  with file system permissions.

### Data at rest

The gateway home directory (`%LOCALAPPDATA%\agent-router\agent-router` by default, or `AGENT_ROUTER_HOME`) holds:

- `agent-router.sqlite` — jobs (including prompts and results), attempts, spend, limit
  calibrations, ratings and classifier votes;
- `jobs/<job id>/` — the prompt file and the raw output (`stdout.log`, `stderr.log`) of every
  attempt;
- `client.token`, `admin.token`, `config.toml`.

Nothing is deleted automatically. Prompts and outputs may contain code and secrets from your
projects. Set `[storage] retention_days` and run `agent-router purge` to delete old terminal jobs;
use `--dry-run` to check the count first. Usage and spend records contain no prompts or outputs and
are kept for pool window accounting.

## Reporting a vulnerability

Please use **GitHub Security Advisories** of this repository
(*Security → Report a vulnerability*). Do not open a public issue for a security problem.

Include the version (`uv tool list` shows it) or the commit, what you did, what happened and what
you expected. You will get an answer as soon as possible; this is a hobby project maintained by one
person, so please allow some time.

## Supported versions

Only the latest release receives fixes.
