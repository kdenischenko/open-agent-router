# agent-router — design

This document describes how the code works today. It reflects the implementation in
`src/agent_router/`; where behavior is planned but not implemented, it says so explicitly.

## Overview

agent-router is a local gateway that distributes coding jobs between AI subscription harnesses
(Claude Code, Kimi Code, OpenCode, Cline). An orchestrator (typically one of these same harnesses
running the `agent-router-delegate` skill, or a human using the CLI) submits a job: a prompt, a
working directory, a mode, and optional hints. The gateway classifies the job's difficulty,
picks a route by a deterministic policy over the remaining subscription quotas, asks for human
confirmation when the route requires it, runs the harness CLI as a child process, records the
spend, and returns the result. It listens on `127.0.0.1` only and authenticates callers with
bearer tokens. The gateway is not an agent itself: anything that touches tools or files is
delegated to a harness.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/architecture-dark.svg">
  <img src="assets/architecture-light.svg" alt="Architecture: an orchestrator agent reaches the gateway through the CLI or the MCP server and you through the dashboard; inside the gateway the HTTP API checks tokens, the dispatcher keeps the queue and asks the tier classifier and the route policy, the ledger feeds pace and blocks to the policy, and the runner starts a coding agent CLI; usage figures reach the ledger from calibration, the Claude Code status line, scheduled Claude Code /usage reads, the OpenCode Go usage API, the opencode stats counter and the harness journal">
</picture>

## Concepts

- **Pool** — one subscription's quota: a set of windows, a unit (`percent` or `usd`), optional
  model weights, a reserve, and levers (`priority`, `weight`). Several routes may spend one pool.
- **Window** — a quota window inside a pool: `duration` (for example `5h`, `7d`, `1mo`), a kind
  (`fixed` — resets at a known or anchored moment, or `rolling`), a role (`pace` — drives
  scheduling; `guard` — only blocks; rolling windows cannot be `pace`), and `block_at`, the used
  fraction at which the pool stops serving.
- **Route** — a `pool` + `backend` + `model` triple with `tiers`, `quality` (1–5), `modes`,
  `confirm`, `enabled`, and optional per-route levers. The route `kind` is `harness` (a coding
  agent CLI runs the job) or `api` (an `[upstreams]` entry serves model requests through the
  proxy; an api route has an `upstream` instead of a `backend` and no `modes`).
- **Tier** — job difficulty: `cheap`, `standard`, or `strong` (in that order).
- **Job** — a unit of work: prompt, working directory, mode, caller, and resulting lifecycle.
- **Harness** (also **executor**) — the agent CLI that runs a job: `claude`, `opencode`,
  `cline` or `kimi`.
- **Attempt** — one execution of a job on one route. A job may have several attempts (fallback).
- **Caller** — who submitted the job; `[callers]` maps a caller name to a pool, and that pool is
  excluded from routing (a caller uses its own subscription directly).
- **Mode** — `read` (the harness must not modify files) or `write` (it may).
- **Confirmation** — routes with `confirm = true` pause the job until a human approves or denies
  it with the admin token.

## Job lifecycle

Statuses: `queued`, `awaiting_approval`, `running`, and terminal `completed`, `partial`,
`failed`, `declined`, `cancelled`. Legal transitions: `queued` → any other; `awaiting_approval`
→ `queued`, `declined` or `cancelled`; `running` → `completed`, `partial`, `failed`, `queued`,
`cancelled`.
All transitions go through the dispatcher; worker threads never change status directly.

Flow per dispatcher tick:

1. Classifier votes and finished attempts are collected; expired pool blocks are lifted.
2. Queued jobs are considered in creation order while slots are free
   (`tasks.max_parallel`, default 3). A job waiting on the classifier is skipped until
   `classifier.wait_s` elapses, then its tier is resolved (see Classifier). A write job waits
   if another job holds the single-writer lock on its repository.
3. The request is re-validated; rejections (for example `cwd_not_allowed`, `queue_full`)
   finish the job as `failed`. The job directory and its repository root are resolved once at
   intake and reused for the writer lock, the before snapshot, the harness launch and the after
   measurement. If either resolves elsewhere later, the job fails with `cwd_changed`; an attempt
   in progress ends with an error and unknown changes. After `tasks.max_attempts` (default 3) it
   fails with `attempts_exhausted`.
4. The policy chooses a route. With no route: `declined` if a confirmation was refused earlier,
   otherwise `failed` with code `no_route` and per-route exclusion reasons.
5. A `confirm` route moves the job to `awaiting_approval` with a deadline
   (`tasks.approval_timeout_s`, default 600 s). Approval pins the route and requeues; denial
   excludes that route, sets `had_refusal`, and requeues; expiry declines the job with
   `approval_expired` and does not move it to another route.
6. Otherwise an attempt starts: the prompt is written to `jobs/<id>/prompt.md`, a git snapshot
   (`HEAD` + working tree) is taken, and the runner is submitted to the thread pool.

Attempts and fallback: an attempt ends with outcome `ok`, `limit`, `error`, `timeout`, or
`cancelled`. On `limit` the pool is blocked (see below) and, unless the attempt changed files or
left a process behind, the pool is added to the job's `excluded_pools` and the job is requeued —
the next attempt goes to another route. On `error` (an executor failure) the same happens by
route: unless the attempt changed files or left a process behind, the route is added to the job's
`excluded_routes` and the job is requeued, so the next attempt goes to another route. A job pinned
to a route (`request.route`) is not moved: its error ends as `failed`, as does a route whose
executable is missing. A write attempt that changed files and then failed ends as `partial` (no
automatic retry); a clean failure that is not moved to another route (a timeout, a pinned
route, no route left) ends as `failed`.

Route health: a route with `ROUTE_ERROR_STREAK` (2) error attempts in a row — errors finished
after the route's last `ok` attempt, a later `ok` resets the streak — is marked unavailable for
`ROUTE_ERROR_COOLDOWN_S` (1800 s). The policy excludes such a route and `advice` shows
`harness unavailable: N errors in a row, retry after <local time>`; after the cooldown the route
is available again. A route the caller pinned still runs, and a job already approved for a route
keeps it.

Limit detection is adapter-side (error patterns and reset times parsed from CLI output). The
dispatcher blocks the pool until the reported reset time, or by exponential backoff
(`tasks.limit_backoff_s` doubling per consecutive limit hit, capped at
`tasks.limit_backoff_max_s`). Blocks expire on tick or via manual `unblock` (admin).

Recovery after restart: queued jobs are simply picked up again. Stored `running` jobs whose last
attempt finished are finalized from the stored result; otherwise the dispatcher reaps the recorded
harness process if its PID, image name and start time still match, re-measures the working tree,
and finishes the job as `partial` or `failed` with `gateway_restarted`. A Windows job object's
handle is deliberately not persisted: `KILL_ON_JOB_CLOSE` has already stopped its tree when the
old gateway exited. Without a job object, recovery still checks only the harness PID; a descendant
left behind by an already-exited harness is not discoverable and can escape both reaping and the
`orphan_process` violation.

Violations recorded on the job: `head_moved` (git HEAD changed during the attempt),
`read_mode_files_changed`, `changes_unknown` (post-attempt tree measurement failed),
`orphan_process` (the harness or a member of its Windows job survived its stop, or the recorded
harness could not be reaped).

## Route choice

The policy is a pure function of the request and the quota snapshot. Exclusion filters, in order
(first match wins, with the reason logged): route `enabled = false`; harness executable
unavailable; `tier` not in `route.tiers`; `mode` not in `route.modes`; route or pool excluded for
this job; caller's own pool; pool blocked until a known time; a window exhausted past its
`block_at`; and the reserve rule — if the pool's remaining headroom is at or below `reserve`,
jobs below `reserve_tier` are excluded. Explicitly requested routes skip the tier, caller, and
reserve filters and ignore `priority`/`weight`. A model request has no tier, so for api routes
the tier and reserve filters do not apply; the other filters work the same.

Survivors are ordered by a key built from: `prefer` rank (when several routes share a
`model_key`, `[models."<key>"] prefer = [pools...]` orders their pools), `priority` (lower
first), pacing score, `quality` (higher first), and route id. The score is
`lag * 100 / max(1 - expected, 0.02) + weight` — the window's `urgency` in percentage points,
plus a `weight` nudge. Levers resolve route → backend → pool default. Routes with no usage
data get score 0 and sort after routes with data at equal priority.

Pacing compares expected and actual usage per `pace` window: `expected` is the elapsed share of
the current window, `used` is the consumed share, `lag = expected - used`. Positive lag means the
pool is under-consuming and should be preferred. Lag alone ignores how little time may be left
before the reset, so the score divides it by the remaining share of the window (with a floor of
`0.02` to avoid division by zero): `urgency = lag / (1 - expected)
= (1 - used) / (1 - expected) - 1` — how many times faster than an even pace the pool must
spend to use up its window. The pool's urgency, like its lag, is the minimum across its pace
windows (the window closest to exhaustion dominates), so a pool whose reset is near and whose
allowance is still unspent outranks a pool with the same lag but weeks of time left. The same
function backs the `advice` API and CLI, so the shown recommendation is exactly what a job with
the same tier, mode and caller would get.

## Usage tracking

The ledger computes each window's state from observations and spend records:

- **Observations** (`pool`, `window`, `fraction` 0..1, `observed_at`, optional `resets_at`,
  `source`) come from manual calibration (`calibrate` command / `POST /api/v1/observations`),
  from the Claude Code status line (a small script posts the `rate_limits` block —
  `five_hour` and `seven_day` percentages with reset times — to
  `/api/v1/signals/claude-statusline`), or from synthetic scheduled resets of fixed windows.
- **Counters**: for pools with `counter = "opencode_stats"` the collector periodically runs
  `opencode stats --json --cost --models --all` and stores cumulative USD samples; growth
  between samples is attributed to the window. A counter declares the harnesses it can see in its
  `sees` set: a route whose spend source is `counter` is invisible to a counter that does not list
  its harness, so `doctor` suggests `spend = "journal"` for it. A route that inherits `counter` on
  a pool with no counter has nothing to read, so `doctor` suggests `spend = "journal"` for it too.
  The baseline is the sample nearest to the observation time, or to the window start after a
  scheduled reset, within the tolerance; after a reset with no such sample the first later sample
  is used and the window is marked as a lower bound. A change in model weights (the pool's `scale_hash`)
  invalidates the base and asks for recalibration.
- **Spend records** are written per attempt (and per proxied model request, source `proxy`):
  pool, route, tokens in/out, `cost_usd`, and an `estimated` flag. For `usd` pools the spend
  since the window base is added on top of the observed fraction.

**Usage sources** (entry point group `agent_router.usage_sources`) are the seam for vendors that
report a window's used share with a reset time instead of cumulative spend. A source's `read()`
returns points — a source field name, a fraction 0..1 and an optional `resets_at` — or a reason
code when there is no data. Each pool maps source fields to its window ids with
`[pools.usage] windows`, and the mapped points become observations with
`source = spec.observation_source` (e.g. `opencode_go_usage`); the newest observation of a window
wins, and for `usd` windows journal spend after it is added on top, as above. A rolling window
gets no reset time, and a fixed window's reset is dropped when it is not in the future or lies
more than one window ahead, the same bounds manual calibration enforces. `[usage].mode = "auto"`
reads the sources on every `[usage].interval`; `"manual"` keeps calibration-only behavior. A pool
may override the mode.
The shipped `opencode-go` source reads the OpenCode Go plan meters (`rolling`, `weekly`,
`monthly`) from the vendor usage endpoint; a failed read yields a reason code (`missing_api_key`,
`unauthorized`, `forbidden`, `http_error`, `timeout`, `unavailable`, `malformed_response`) instead
of an exception, and the key from `api_key_env` never reaches a reason or a log.
The shipped `claude-code` source reads the Claude plan meters (`session`, `weekly_all`) by
running `claude -p /usage --setting-sources "" --strict-mcp-config --output-format stream-json
--verbose --no-session-persistence` (an argument list, no shell, a timeout, the process tree
killed on Windows and POSIX) and taking `usage_report.rate_limits.limits[]` from the synthetic
message whose `local_command_run.command` is `usage`: `kind` is the field, `percent / 100` the
fraction, `resets_at` the reset. Rows with another `kind`, a percent outside 0..100 or a
malformed reset are skipped. Its reason codes are `not_found` (no CLI), `timeout`,
`unauthorized` (the CLI is not signed in, checked with `claude auth status --json` when the
message has no `usage_report`, or `rate_limits_available` is false), `rate_limited` (the CLI
says the usage endpoint is rate limited) and `malformed_response` (no `usage_report` or no usable
row). A non-synthetic assistant message means the prompt reached the model: the source stops the
process and reports `malformed_response`. The source never reads the Claude Code credentials
and never passes a token; the CLI uses the user's own `claude auth login`. The CLI path is
`claude` from `PATH` unless the pool sets `[pools.usage] executable`.

**The usage reader** builds one source instance per pool with `usage.source` and reads it on a
schedule and on demand. In auto mode a background thread (`usage-reader`) calls
`refresh(scheduled=True)` on a short tick of `min(60 s, interval)`; the reader itself gates per
pool: a scheduled pass skips a pool whose last attempt is younger than `[usage].interval_s`, any
pass skips a pool attempted within the last 60 seconds (`MIN_REFRESH_S`, reason `too_soon`), and a
pool whose effective mode is `manual` is never read. The check and the new attempt time are taken
in one step, so a refresh arriving while another one reads the same pool is skipped. A forced read is available through
`POST /api/v1/usage/refresh` (admin token, optional `{"pool": "<id>"}` body), the
`agent-router usage refresh [POOL]` command, and the **Refresh limits** buttons of the dashboard.
Each read writes its observations through the same path as a calibration and runs the same
out-of-schedule counter check; a source exception becomes the reason `error`, and a read in which
no point maps onto a window becomes `no_data`. Each pass publishes one event per pool on the bus: `usage.read` with
`{pool, source, windows}` on success, `usage.read_failed` with `{pool, source, reason}` on
failure. Events, results and reasons carry ids only: never keys or response bodies. Per pool the
reader keeps `last_attempt_at`, `last_success_at` and `last_reason` in memory for display.

The snapshot reports both where a figure comes from and whether the reads behind it are fresh.
Each window carries `figure_source`: `"auto"` when its latest observation came from the pool's
usage source (the observation `source` equals the source spec's `observation_source`),
`"manual"` for `source == "manual"`, and `"journal"` otherwise — no observation in the current
window, a scheduled reset, or a collector that is neither. Each pool with `usage.source` carries
a `usage` object — `{source, mode, last_attempt_at, last_success_at, last_error, age_s, stale}` —
and pools without a source have `usage: null`. `stale` is true when the pool's effective mode is
`auto` and no successful read is fresher than `2 * [usage].interval_s`, including never; the
threshold lives in one helper (`agent_router.usage.is_stale`) so the dashboard, `status` and
`doctor` agree. `doctor` additionally warns when the variable named by `api_key_env` (or the
source spec's `default_api_key_env`) is missing from the environment, prints the warning of the
spec's own `check` (for `claude-code`: a missing CLI or a CLI that is not signed in), and when a
pool is in
auto mode without a `source` — but only when the pool's own `[pools.usage]` sets
`mode = "auto"` or the config has a `[usage]` table, so configs that never mention usage stay
quiet.

Cost is exact when the harness reports it (`cost_estimated = false`). Otherwise it is estimated
from the pool's `price_file` — a TOML table `"<model>" = [in, out]` in USD per 1M tokens —
multiplied by the model weight `limit_base_usd / model_limits[model]`, and tagged with the price
file's content version. Rolling-window observations older than the window are treated as "no
data"; exhausted-at-`block_at` windows block the pool. A fixed-window observation without
`resets_at` keeps the latest reset time an earlier observation of that window reported, moved
forward by whole windows; without one it falls back to the window `anchor`.

## Classifier

The classifier predicts the required tier (`cheap` / `standard` / `strong`) from the prompt.
Three kinds exist: `systemone` (POST to `{url}/v1/systemone`, a score + confidence answer taken
from the class probabilities), `ollama` (POST to `{url}/api/chat` with a JSON-schema answer;
confidence recomputed from token logprobs) and `openai` (POST to `{url}/chat/completions` with a
JSON-schema `response_format`; confidence from token logprobs when the server returns them,
otherwise the model's own). A URL is either loopback or `https` with `api_key_env`: the key is
read from that environment variable at every request and sent as a bearer token; a missing key
ends the vote with `no_api_key` without a request. A loopback URL is reached directly, without
the environment proxy; an `https` URL keeps using it. The classifier receives the prompt, cut to
its first 3000 and last 1000 characters when longer than 4000. Examples and measurements are in
[CLASSIFIER.md](CLASSIFIER.md).

Config `[classifier]`: `mode` (`shadow` — votes are recorded but never applied — or `active`),
`primary` (the deciding model id), `min_confidence` (default 0.6, per-model override possible),
`write_floor` (default `standard`: write jobs are never classified below this tier),
`wait_s`, `vram_budget_mb`, and `[[classifier.models]]` entries (`id`, `kind`, `model`, `url`,
`timeout_s`, `api_key_env`, `live`). In active mode a missing or low-confidence vote falls back to `standard`;
an unavailable classifier never stops the gateway. In shadow mode the caller's hint or the
default decides, and agreement is measured from stored votes.

Votes run through a single priority queue (primary model first; a bounded backlog drops
secondary votes under load). `classifier eval` replays past job prompts through selected models
offline: it unloads live local Ollama models from VRAM and marks the votes `offline`; live
voting resumes afterwards. `classifier report` computes, per classifier and split by
live/offline: accuracy against ground truth (manual `should_tier` ratings, then caller tiers of
jobs rated `good`), the tier confusion matrix, the underestimation rate, latency p50/p95, error
rate, and a before/after split around the last `mode` change.

## Runner and harness adapters

The runner starts the harness without a shell: `Popen(argv, cwd, env, stdout/stderr to pipes)`.
The environment is inherited plus `NO_COLOR=1` and `PWD` set to the working directory (opencode
reads it). On Windows the child gets its own process group and no console window. Each attempt
also gets a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: the harness is created suspended,
assigned to the job, and only then resumed, closing the launch/assignment race. Descendants join
the same job. Stopping or finishing an attempt terminates the job and queries its process list;
anything left sets `survived` and produces `orphan_process`. Job, process and thread handles are
closed on every exit, and closing the last job handle also stops the tree if the gateway crashes.
Windows 8 and later support nested jobs, but an enclosing job or policy can reject assignment. If
job creation or assignment fails, the harness is resumed and the legacy
`taskkill /PID <pid> /T /F` stop remains in use; in that mode a descendant of a dead harness is not
visible to the survivor check. On POSIX, stopping still uses `SIGTERM` then `SIGKILL` to the process
group. Output is streamed to per-attempt log files, capped at 20 MiB per stream with a truncation
marker.
Timeouts: per-job `timeout_s`, else `route.timeout_s`, else `tasks.default_timeout_s`
(14400 s, a hard cap). A harness that produces no stdout or stderr for `tasks.idle_timeout_s`
(600 s) is stopped as idle; `0` disables the check. An explicit per-job or route `timeout_s`
replaces the hard cap, but the idle limit still applies. Each adapter declares
`reports_progress`: Claude Code, kimi, opencode and cline stream their output (`True`). An adapter
may also declare
which stdout lines count as progress; without that declaration, all output counts. OpenCode JSON
events with type `retry` do not count as progress, while its other output does. After an
adapter-recognized final line a lingering process is killed within `tasks.exit_grace_s` (60 s).
Long prompts or shim (`.cmd`/`.bat`) executables switch the prompt from argv to a prompt file with
a "read the task from the file" instruction; shim arguments are validated against a safe
character set.

A harness that stays silent while it works would otherwise be stopped as idle. An adapter may
declare `quiet_while_working` (False when absent): the runner then samples two more signs of life
every 30 s and treats a positive sample as activity. The CPU time of the attempt's process tree
grew by at least 1% of one core over the time between samples (on Windows from the job object's accounting information,
which includes the processes that already exited; on Linux from `/proc/<pid>/stat` of the process
group), or a file under the working directory changed (the newest mtime of the tree, skipping
`.git`, `.venv`, `node_modules`, `__pycache__` and the attempt's own log directory; the walk
stops after 20000 files). A signal that cannot be read is absent, not an error. The idle error
then names the last sign of life (`executor showed no progress for 600 s; last activity: files
640 s ago`). Only kimi declares it: it prints one JSON line per finished message, so a long answer
or a running subagent looks like silence. The streaming adapters keep the old behavior — output
is their sign of life. The hard cap applies either way.

OpenCode runs with `--standalone` so its session uses a private server in the child process tree;
without it, stopping the `opencode run` client would leave the session running in the background
service.

Adapters (`adapters/`) build argv/stdin and parse output:

- **kimi** — `kimi -p <text> -m <model> --output-format stream-json`; prompt in argv (or a prompt
  file); read mode is enforced by the preamble only; NDJSON events, last assistant message is
  the answer; spend source `journal`; `quiet_while_working`, so a silent answer is not idle.
- **claude** — `claude -p --output-format stream-json --verbose --model <m> --permission-mode
  <mode> --setting-sources project --strict-mcp-config`, prompt via stdin; read maps to
  `plan`, write to `bypassPermissions`. The isolation flags keep the user's settings, hooks,
  plugins and MCP servers out of the job. The NDJSON event with type `result` carries `result`,
  `usage` (including cache tokens) and `total_cost_usd`; spend source `journal`.
- **opencode** — `opencode run --format json --archived --standalone --model <m>`, prompt via
  stdin; read maps to `--agent plan`, write to `--auto`; NDJSON `step_finish` events carry tokens
  and cost; spend source `counter`.
- **cline** — `cline --json -P <provider> -m <model> [--plan | --auto-approve true] -- <prompt>`;
  read maps to `--plan`, write to `--auto-approve`; the `run_result` event carries tokens and
  cost; spend source `journal`.

Every adapter prepends a preamble to the prompt: work only in the given directory, no
`git commit` / `git push` / history rewriting, no long-running background processes, end with a
concise report; read mode adds "do not create, modify, or delete any files". Each adapter module
exposes `HARNESS = HarnessSpec(...)` and registers via the `agent_router.harnesses` entry-point
group; counters use `agent_router.counters`.

## Model proxy

With `[upstreams]` configured, the gateway serves an OpenAI-compatible endpoint of its own:
`POST /v1/chat/completions` and `GET /v1/models` (client token as the bearer key, the same
`/api/` body limit). A tool points its base URL at the gateway; the requested model is matched
against the api routes' `model` and `model_key`, the route policy picks one by the pools' quota
state (a blocked or exhausted pool is excluded like for jobs), and the request is forwarded to
the route's upstream with `model` rewritten to the id the upstream serves. When the upstream
cannot be reached (connect error or connect timeout), the next chosen route is tried; an
upstream that answers with an HTTP error speaks for itself and its status and body pass through
unchanged. Streaming requests are proxied chunk by chunk; with the upstream's `stream_usage`
(the default) the gateway adds `stream_options.include_usage` and reads the token counts from
the terminal event. The upstream's key comes from `key_env` or `key_file`, read at request time
and never logged; a loopback upstream (LM Studio, Strata) may take no key, a remote one must use
https and exactly one key source. `pass_headers` forwards the listed request headers, minus
hop-by-hop and credential headers.

Each answered request writes a spend record with source `proxy` (pool, route, tokens, request id
as the reference; a write failure does not fail the answered request). The cost is estimated
from the pool's `price_file` and model weight, like a job without a reported cost; for local
models without prices the record carries tokens only. `upstream.enabled = false` marks its
routes unavailable (`upstream disabled` in the exclusion reason). The upstream timeout is
`route.timeout_s`, else 600 s, with a 10 s connect budget so a dead server fails over fast.
`agent-router doctor` reports each upstream's key state and the reachability of its `/models`
endpoint.

## Storage and API

One SQLite database (`agent-router.sqlite` in the gateway home; WAL, schema versioning via
`user_version`, pluggable through the `agent_router.storage` entry point). Tables: `observations`,
`counter_samples`, `jobs` (JSON blob per job, including the request with its prompt; `prompt_sha256`
and `prompt_chars` let exports omit the text), `attempts` (one row per attempt with result, git
snapshots, log tail, spend source, decision snapshot), `pool_blocks`, `spend`, `ratings` (note kept
as hash + length only), `classifier_votes`, and `settings` (atomic flags, panel epoch, export salt).

Events: an in-process `EventBus` with named subscribers, each on its own daemon thread with a
bounded queue (overflow drops, never blocks). Event payloads pass a JSON round-trip plus a key
blacklist (no prompts, paths, tokens). Sinks are file/plugin based — a built-in rotating JSONL
sink; events are not stored in SQLite.

HTTP API under `/api/v1` (token in `Authorization: Bearer`; the admin token satisfies client
routes; only `127.0.0.1`/`localhost` Host headers are accepted):

- Client: `GET /snapshot`, `POST /signals/claude-statusline`, `GET /advice`, `GET /events/stats`,
  job operations — `POST /jobs`, `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/wait`
  (long-poll, capped), `POST /jobs/{id}/cancel`, `POST /jobs/{id}/rating`. With `[upstreams]`
  configured: the OpenAI-compatible `POST /v1/chat/completions` and `GET /v1/models` (see
  Model proxy).
- Admin: `POST /observations` (calibration), `POST /jobs/{id}/approve`, `POST /jobs/{id}/deny`,
  `POST /pools/{id}/unblock`.
- `GET /healthz` is unauthenticated.

Request bodies are limited before parsing, since the token is checked only after the body is read:
under `/api/` the limit is room for the longest accepted prompt (`tasks.max_prompt_chars` × 12 bytes,
the worst case of a JSON-escaped character, plus 64 KiB), dashboard forms get 64 KiB. A declared
`Content-Length` over the limit is rejected at once, a streamed body as soon as it passes the limit;
both answer `413` with the code `body_too_large`.

Tokens are files `client.token` / `admin.token` in the gateway home, created by `init` (or the first
`serve`) with owner-only permissions where the OS supports them. The dashboard (`web.py`, Jinja2, no
frontend build) logs in with the admin token and sets a signed `ar_session` cookie (HMAC keyed from
the admin token, revocable via a settings epoch); every POST form carries a per-session CSRF token.
Pages: an approvals inbox on the home page; limits cards with usage bars, pacing, reset
countdowns, data age, stale and blocked states, pool unblock forms and a collapsible table;
advice; jobs with approve/deny/cancel; calibration form. Calibration re-renders submitted
values with field errors, enhances the form with inline validation and a usage/reset preview,
and highlights the recorded pool after the redirect and subsequent limits-fragment refreshes.
Deny and Cancel ask for confirmation: a dialog with JavaScript, a `GET /jobs/{id}/deny` or
`/cancel` confirmation page without it;
both post to the same routes. `GET /jobs` and its refresh fragment `/fragments/jobs` take
`status`, `route`, `caller`, `q` (job id, or a substring of the prompt, case-insensitive for
ASCII letters), `sort` (`newest`, `oldest`, `duration`) and `page`; unknown or malformed values
are ignored, and a page past the end shows the last one. The search is `JobStore.search` in
SQL over the stored job data. The job page refreshes itself, shows the error and lets you rate
a finished job.
It shows the attempt, token, cost and duration totals, a per-attempt timeline, and the prompt,
final text and per-attempt output in expandable blocks; copy buttons appear when clipboard
scripting is available.

## Module map

| Module | Responsibility |
|---|---|
| `api.py` | FastAPI app: routes, token checks, host guard, lifespan wiring |
| `auth.py` | Token files, dashboard session cookies, CSRF tokens |
| `classifier.py` | facade re-exporting the classifier modules; tier resolution and dashboard snapshot |
| `classifier_backends.py` | systemone/ollama/openai backend clients, prompts and answer parsing |
| `classifier_eval.py` | offline evaluation and live-model unloading |
| `classifier_report.py` | classifier accuracy/latency report |
| `classifier_votes.py` | vote queue and flag coordination |
| `cli.py` | `agent-router` root app and the `advice`, `calibrate`, `purge`, `export`, `classifier`, `doctor` and `mcp` commands |
| `cli_common.py` | Helpers shared by the CLI command modules (config loading, gateway client, quoting) |
| `cli_gateway.py` | CLI commands `init`, `serve` and `status` with their helpers |
| `cli_jobs.py` | CLI job commands: `run`, `wait`, `result`, `rate`, `cancel`, `jobs`, `approve`, `deny`, `unblock` |
| `client.py` | Minimal stdlib HTTP client used by the CLI |
| `clock.py` | Clock protocol (system/fake), UTC helpers, local-time formatting |
| `config.py` | TOML parsing and validation: `load_config`, config template, re-exports of the config model |
| `config_classifier.py` | Parsing of the `[classifier]` section: single-model form, `[[classifier.models]]`, endpoint address checks |
| `config_model.py` | Immutable configuration data model: tier/mode/spend literals, settings dataclasses, `Config`, `ConfigError` |
| `config_values.py` | Generic TOML value checks (keys, tables, scalars) used by the config parsers |
| `dispatcher.py` | Job state machine: scheduling, attempts, fallback, blocks |
| `dispatcher_recovery.py` | Startup recovery: applies recorded outcomes, finishes interrupted attempts as emergencies, reaps orphan processes |
| `dispatcher_support.py` | Dispatcher helpers without dispatcher state: writer lock keys, output tails, route-choice snapshots |
| `events.py` | In-process event bus with per-subscriber threads |
| `export.py` | Redacted JSONL/CSV export of jobs, spend, observations |
| `jobs.py` | Job/attempt/rating/vote data model, statuses and transitions |
| `jobstore.py` | SQLite storage for jobs, attempts, ratings, votes, blocks, spend |
| `ledger.py` | Window state from observations, counters, and spend |
| `lifecycle.py` | One `serve` process per gateway home: an OS lock on `serve.lock` |
| `liveness.py` | Liveness of a running harness: the idle limit on silent output and the hard time cap |
| `mcp_server.py` | MCP tools over stdio for orchestrators; gateway client API, no admin actions |
| `pacing.py` | Expected vs used usage, lag, reserve and block computation |
| `periods.py` | Window durations and boundary arithmetic |
| `plugins.py` | Entry-point loading for harnesses, counters, storage, sinks |
| `policy.py` | Deterministic route filters and ordering |
| `prices.py` | Price-file parsing and token-based cost estimation |
| `proxy.py` | OpenAI-compatible `/v1` endpoints: model-to-route matching, upstream forwarding, spend |
| `registry.py` | Harness/counter specs, backend resolution, config validation |
| `runner.py` | Child-process execution, log capture, timeouts, process-tree kill |
| `service.py` | Gateway composition: snapshot cache, waiters, background loops |
| `settings_store.py` | Atomic key-value flags over the `settings` table |
| `sinks.py` | Event sink protocol and the rotating JSONL sink |
| `spend.py` | Spend records and attempt cost estimation |
| `statusline.py` | Claude Code status line script (posts rate limits to the gateway) |
| `store.py` | SQLite storage for observations and counter samples |
| `usage.py` | Usage source protocol and spec, mapping of source points to window observations, staleness rule (`is_stale`) |
| `usage_reader.py` | Scheduled and on-demand reads of usage sources into observations, per-pool read health |
| `web.py` | Dashboard pages and fragments |
| `winjob.py` | Windows job objects: every process an attempt starts stops with the attempt |
| `worktree.py` | Git snapshots, changed-file detection, path sanitizing |
| `adapters/base.py` | Adapter protocol, preamble, outcome settling, limit hints |
| `adapters/{kimi,claude,opencode,cline}.py` | Per-harness argv and output parsing |
| `collectors/claude_statusline.py` | Status-line JSON to observations |
| `collectors/claude_code_usage.py` | `claude-code` usage source: `/usage` of the Claude Code CLI |
| `collectors/opencode_go_usage.py` | `opencode-go` usage source: OpenCode Go usage endpoint |
| `collectors/opencode_stats.py` | `opencode stats` counter sampler |

## Planned

- **More upstream kinds**: the proxy speaks the OpenAI chat completions API; other API shapes
  (for example Anthropic's) may follow.
- **Linux and macOS support**: the process, git, and CLI layers have POSIX branches and the test
  suite runs on Linux and macOS in CI (experimental, allowed to fail); the gateway has been run
  live only on Windows, and autostart exists only there.
