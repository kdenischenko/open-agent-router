# Troubleshooting

Start with `agent-router doctor`: it checks the configuration, finds every harness and lists the
enabled routes. `agent-router doctor --live` also probes each enabled harness once on a read route
without `confirm`; disabled and write-only routes are skipped. `agent-router advice --tier standard`
shows why each route is or is not chosen right now.

## The gateway is unavailable (exit code 5)

`gateway unavailable: check that agent-router serve is running`

- Start the gateway: `agent-router serve`. Client commands, the dashboard and the MCP server all
  need it.
- The gateway listens on `127.0.0.1` at `server.port` from `config.toml` (default `8787`). If it
  runs on another port, set `AGENT_ROUTER_URL` in PowerShell:

  ```powershell
  $env:AGENT_ROUTER_URL = "http://127.0.0.1:9123"
  ```

- Client and gateway must use the same home. If you set `AGENT_ROUTER_HOME` for `serve`, set it
  for client commands and for the harness that starts `agent-router mcp` too.

`gateway rejected the token` also exits `5`: the client read tokens from another home than the
gateway, or the tokens were recreated after the gateway started. Use one home and restart
`agent-router serve`.

`port 8787 on 127.0.0.1 is in use` means `serve` could not start because another program holds
the port (often a gateway started from another home). Stop that program, or start this one on a
free port with `agent-router serve --port 9123` and set `AGENT_ROUTER_URL` as above. A second
`serve` on the same home stops earlier with a message that the gateway is already running.

## A harness is not found

`doctor` shows `no` in the `Found` column and warns
`route <id> is enabled, but harness <name> not found`; `advice` excludes the route with
`harness unavailable: …`.

- Check that the harness runs from the same shell: `kimi --version`, `opencode --version`,
  `codex --version`, `cline --version` or `cline-cli --version`, and `claude --version`.
- The gateway looks the harness up on its own `PATH`. If you installed the harness after starting
  `agent-router serve`, or the gateway starts from autostart with a shorter `PATH`, restart it.
- Or set the full path in `config.toml`:

  ```toml
  [backends.kimi]
  executable = "C:/Users/<user>/.local/bin/kimi.exe"
  ```

## A harness is not signed in

The harness is found, but jobs on its routes fail and `doctor --live` reports `failed` for them.

- Run the harness by hand once with the same model and sign in the way it asks. Each harness keeps
  its own sign-in; the gateway never sees your credentials.
- Run `agent-router doctor --live` again: every probe should complete.
- Harness output is in `jobs/<id>/attempt-<n>/stdout.log` and `stderr.log` under the gateway home.
  `run`, `wait` and `result` print that directory for a failed or partial job, and the last lines
  of the log for a failed one.

## A limit is reached and the pool is blocked

When a harness reports a usage limit, the gateway closes its pool until the reported reset time,
or for a backoff period when the time is unknown (`tasks.limit_backoff_s`, doubling per
consecutive hit up to `tasks.limit_backoff_max_s`). A clean attempt moves to another route if one
matches. `advice` shows the closed pool as `pool closed until <time>: <reason>`.

- Wait for the reset; the block is lifted on its own.
- If the limit was reset early (a new plan, a top-up), lift the block:
  `agent-router unblock <pool>` (admin token). It removes only the block set after the limit
  hit. If a window of the pool is still used past its `block_at`, the command says so and the
  pool stays excluded until the window resets or a calibration records the real usage.
- A window that is used past its `block_at` shows as `window <id> exhausted until <time>`.
  Record the real usage with `agent-router calibrate <pool> <window> <percent>` if the stored
  value is out of date.
- `no data, calibrate` means the gateway has no usage data for the pool yet; jobs still run on
  it. Record the usage your subscription shows with `agent-router calibrate`.

## A job is waiting for approval

A route with `confirm = true` pauses the job in `awaiting_approval` until a human decides.
`wait` returns at once with exit code `4` and the message
`job awaits approval: agent-router approve <id> before <time>, then agent-router wait <id>`.

- List waiting jobs: `agent-router jobs --status awaiting_approval`.
- Approve or deny: `agent-router approve <job-id>` or `agent-router deny <job-id>` (admin token),
  or use the buttons on the dashboard.
- Without a decision the approval expires after `tasks.approval_timeout_s` (default 600 s): the
  job ends as `declined` with error `approval_expired` and does not move to another route.
- Deny excludes that route for the job and lets it look for another one; if no other route
  matches, the job ends as `declined`.
- Approvals are never done by an agent: neither the delegation skills nor the MCP server have an
  approve action. To skip approval for a route you trust, set `confirm = false` on it.

## A job is denied (exit code 6)

`request denied: …`

- The working directory is outside `tasks.allowed_roots` or inside `tasks.denied_roots`. Allow it
  with `agent-router init --allowed-root <directory>` and restart `agent-router serve`.
- `queue is full, try again later`: wait for running jobs to finish.

## Usage sources do not refresh

`doctor` prints these warnings for pools with a usage source; the dashboard pool card and
`agent-router status` show the same facts (`Source: …`, `read … ago`, `stale`).

**`Warning: pool <id>: usage source <name> needs environment variable <VAR>, which is not set`**

- The variable named by the pool's `api_key_env` (or the source's default, `OPENCODE_GO_API_KEY`
  for `opencode-go`) is missing from the environment of the user that runs `agent-router serve`.
  `doctor` checks presence only, never the value.
- Set it for that user — for an autostarted gateway it must be visible to the process started
  by `start-hidden.vbs`, not only to your terminal. In PowerShell:
  `[Environment]::SetEnvironmentVariable("OPENCODE_GO_API_KEY", "<key>", "User")`, then sign out
  and back in, and restart `agent-router serve`.

**`Warning: pool <id>: usage source claude-code: the Claude Code CLI is not signed in; run claude
auth login`** (or `cannot find the Claude Code CLI …`)

- Sign in once as the user that runs `agent-router serve`: `claude auth login`, then check with
  `claude auth status`. If `claude` is not on `PATH` for that user, set
  `[pools.usage] executable` to the full path of the CLI.

**Codex (`source = "codex"`)**

- If `doctor` says the Codex CLI is not signed in, run `codex login` as the user that runs
  `agent-router serve`; `codex login status` should then report a signed-in account. For
  `not_found` or a doctor warning that the CLI is missing, install it or set `[pools.usage]
  executable` to its full path.
- OpenAI marks the app-server protocol as experimental. If that protocol changes, reads report a
  reason such as `malformed_response` or `no_data`, and the affected windows fall back to the
  journal and manual calibration. Switch the pool to `mode = "manual"` in `[pools.usage]` and
  calibrate it until agent-router supports the new response.

**`Warning: pool <id>: last usage read failed: <reason>`**

- The source itself explains why: `missing_api_key` — the key variable was not set for the
  gateway process (see above); `unauthorized` / `forbidden` — the key is wrong or has no access
  to the plan's usage endpoint, create a new key in the vendor console; `timeout` /
  `unavailable` / `http_error` — the vendor endpoint did not answer, the next scheduled read
  retries on its own; `malformed_response` — the endpoint answered in a shape the source does
  not understand. For `claude-code`: `not_found` — no CLI at `executable` or on `PATH`;
  `unauthorized` — the CLI is not signed in (see above); `rate_limited` — the Claude usage
  endpoint refused the read, the next scheduled read retries; `malformed_response` — the CLI
  printed no plan usage, for example an API-key sign-in that has no plan limits.
- Force a retry once the cause is fixed: `agent-router usage refresh <pool>` or the pool card's
  **Refresh limits** button. A failed read writes nothing; the last successful observation stays
  in place.

**`Warning: pool <id>: usage data is stale, no successful read for <age>` (or `no successful read
yet`)**

- In auto mode no read has succeeded within twice `[usage].interval` (a pool that was never read
  counts as stale). The stored figures may be behind the plan.
- Check the line above it for a failed read and fix that first. With a working source, wait for
  the next scheduled read or force one. For a plan you calibrate by hand, switch the pool to
  `mode = "manual"` in `[pools.usage]` (or `[usage]` for all pools): manual mode never goes
  stale.

**`Warning: pool <id>: usage mode is auto but no source is set`**

- The pool's effective mode is auto — the pool's own `[pools.usage]` sets `mode = "auto"` or the
  config has a `[usage]` table — but the pool has no `usage.source`, so nothing refreshes its
  windows automatically.
- Give it a source (`source = "opencode-go"` with the `windows` mapping), or set
  `mode = "manual"` on the pool, or remove the `[usage]` table when no pool uses a source.

**`Warning: pool <id>: usage source <source> no longer reports <field>; window <window> falls
back to the journal; update agent-router or switch the pool to mode = "manual" and calibrate`**

- The source answered, but the read no longer contains a field the pool's `[pools.usage] windows`
  mapping expects. The mapped window keeps its last observation or journal figure until a
  calibration or a read that reports the field again refreshes it. Fields the source returns that
  the mapping does not use are ignored and never warn.
- The vendor renamed or removed a meter (for example after a plan change). Update agent-router to
  a version that understands the new fields.
- Or switch the pool to `mode = "manual"` in `[pools.usage]` and record the usage yourself with
  `agent-router calibrate <pool> <window> <percent>` until you update.

**`Warning: pool <id>: usage source <source> response format may have changed; update
agent-router or switch the pool to mode = "manual" and calibrate`**

- Three reads in a row failed with `malformed_response` or `no_data`, the pattern the reader
  interprets as a changed response format; a single failed read does not set it, and a successful
  read resets the count.
- Try `agent-router usage refresh <pool>` once: if the read succeeds, the flag was a transient
  outage and clears on its own. If it keeps failing, the vendor's endpoint answered in a shape
  this agent-router version does not understand — update agent-router, or switch the pool to
  `mode = "manual"` and calibrate until then.

## Still stuck

Open an issue with the output of `agent-router doctor` and the failing command. Remove paths,
tokens and anything personal from it first.
