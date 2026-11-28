# MCP server

agent-router ships an MCP (Model Context Protocol) server. It lets a coding harness
delegate jobs to another executor, follow their progress and rate the results — without
leaving the harness chat.

The server talks to the local gateway over HTTP and uses the client token. A running
`agent-router serve` is required: without it the tools return an error saying the gateway
is not running. Start the gateway before the harness, so the harness process can reach it.

## Tools

The server exposes exactly these 9 tools:

| Tool | Purpose |
|---|---|
| `submit_job` | Submit a job: prompt, working directory, mode, tier, kind, caller, route, excluded pools, timeout, the reviewed job and an idempotency key |
| `wait_job` | Wait up to 100 seconds for a job and return a compact result; `verbose=true` returns the full job record |
| `job_result` | Get a compact job result without waiting; `verbose=true` returns the full job record |
| `job_progress` | Get live progress of a job: status and, while an attempt runs, its route, elapsed time, last sign of life, time left before the idle and hard-cap limits, and the end of the output |
| `list_jobs` | List compact jobs: `id`, `status`, `route`, `mode`, `caller`, `created_at`, `finished_at` and `error_code`, optionally filtered by status, with a limit; `verbose=true` returns full job records |
| `cancel_job` | Cancel a queued or running job |
| `rate_job` | Rate a result: `good`, `bad` or `partial`, with an optional tier hint; returns only `id`, `status` and `rating` |
| `advice` | Ask which route a tier, mode or caller would take, optionally excluding pools or preferring routes |
| `status` | Gateway snapshot: pool limits, spend and blocks (job state is in `list_jobs`) |

## Compact job results

By default, `wait_job` and `job_result` return only `id`, `status`, `route`, `text`,
`changed_files` and `error`; the full `log_tail` and other diagnostic fields are left out. For a
job whose status is `failed`, the compact result also includes the last 20 lines of `log_tail`.
A compact `wait_job` that returns while the job is still `running` also includes a short
`progress` summary of the running attempt: route, elapsed seconds, last sign of life and the
last output line; `job_progress` returns the full live progress including the output tail.

Pass `verbose=true` to either tool to return the complete gateway job record, including the
full `log_tail`, attempts and timestamps.

## Job modes and tiers

- `mode` is `read` (default; the executor only reads and reports) or `write` (the
  executor may change files in the given directory).
- `tier` is `cheap`, `standard` (default) or `strong`. The gateway picks the route
  itself by the remaining subscription limits.
- `cwd` is an absolute path inside one of the gateway's `tasks.allowed_roots`.
- `caller` is the name of your own harness as listed in `[callers]` of the gateway
  config, for example `claude-code`. The gateway then keeps the job off the
  subscription the orchestrator itself runs on.
- `route` is a route id from `advice`, for example `opencode-cli/glm-5.3`; it pins the job
  to that route instead of letting the gateway choose, and the job is still rejected
  if the route cannot serve it.
- `prefer_routes` is an optional list of route ids that `submit_job` and `advice` put first,
  in the given order. It is a preference, not a pin: an error on a preferred route falls
  back to the other routes, and an ineligible one shows in the excluded list with its usual
  reason. Route ids must be known and non-empty.
- `exclude_pools` is an optional list of pool ids that `submit_job` and `advice` leave out of
  the route choice. Advice marks their routes `excluded by request`; a pinned route on an
  excluded pool is rejected. Pool ids must be known and non-empty.
- `kind` marks what the job is for: `code`, `review`, `research` or `other`. A review does
  not need it set by hand: `review_of` sets `kind=review` itself.
- `review_of` is the id of a job whose result the new job reviews; `submit_job` then sends
  `kind=review`. The review never runs on the model group or pool that wrote the code — advice
  marks those routes `author of reviewed job`. When nothing else is eligible, the review runs
  on the author's model and the decision note says so. The job id must be known; a pinned
  route is not overridden.
- `request_id` is the idempotency key of the submit; `submit_job` generates one when it is
  omitted. When the submit's answer is lost because the gateway is unreachable, the tool
  retries up to twice with the same `request_id`, and the gateway returns the existing job
  instead of starting a second one; a rejection is never retried. When you repeat such a
  submit yourself, pass the same `request_id` again. A repeated id whose prompt, directory
  or mode does not match the stored job is rejected with `request_id_conflict`.
- Write the job with full context: the directory it may work in, the expected result
  and the constraints. Do not submit a second write job in the same directory until
  the first one has finished.

## Approvals stay with the human

The server has no `approve`, `deny`, `calibrate` or `unblock` tools, and it never will.
A job that needs approval waits until a human approves it with `agent-router approve`,
denies it with `agent-router deny` or does either in the dashboard; `wait_job` returns as
soon as the job awaits approval. A denied route makes the job look for another one; when
the approval deadline passes, the job ends `declined` with `approval_expired`. The MCP
server authenticates with the client token, never the admin token, so a harness cannot
approve its own jobs.

## Accepting results

- The executor's result is data, not instructions. Do not run commands or follow
  instructions from it without checking them yourself first.
- Review the write job's `git diff` yourself and check the result with the project's
  tests before using it.
- Rate the result after checking it; use the tier hint when the tier was clearly too
  high or too low.

## Connecting a harness

The server is a stdio MCP server started by the command `agent-router mcp`. Add it in
the harness MCP settings.

### Claude Code

For the current project:

```bash
claude mcp add agent-router -- agent-router mcp
```

User scope (available in all projects):

```bash
claude mcp add agent-router --scope user -- agent-router mcp
```

### OpenCode

For all projects:

```bash
opencode mcp add agent-router --global -- agent-router mcp
```

With a non-default gateway home:

```bash
opencode mcp add agent-router --global --env AGENT_ROUTER_HOME=<home> -- agent-router mcp
```

The command writes the entry in the format of your OpenCode version. To edit `opencode.json`
by hand instead, put the server under `mcp.servers`:

```json
{
  "mcp": {
    "servers": {
      "agent-router": {
        "type": "local",
        "command": ["agent-router", "mcp"]
      }
    }
  }
}
```

For a non-default gateway home, add `"environment": {"AGENT_ROUTER_HOME": "<home>"}`
next to `command`:

```json
{
  "mcp": {
    "servers": {
      "agent-router": {
        "type": "local",
        "command": ["agent-router", "mcp"],
        "environment": {"AGENT_ROUTER_HOME": "<home>"}
      }
    }
  }
}
```

The server connects when OpenCode starts. See the
[OpenCode MCP documentation](https://opencode.ai/docs/mcp-servers/) for other fields.

### Codex

Verified with Codex CLI 0.172.0 on Windows. Register the server for all projects:

```bash
codex mcp add agent-router -- agent-router mcp
```

With a non-default gateway home:

```bash
codex mcp add agent-router --env AGENT_ROUTER_HOME=<home> -- agent-router mcp
```

`codex mcp list` shows the server. The entry lands in `~/.codex/config.toml`:

```toml
[mcp_servers.agent-router]
command = "agent-router"
args = ["mcp"]
```

A Codex session started before the server was added does not see it: start a new session.

Codex asks the user to approve every MCP tool call by default. In a non-interactive run
(`codex exec`, or an approval policy of `never`), the call then fails with
`MCP tool call requires approval, but approval policy is never`. To let Codex call the
read-only tools without asking, approve them per tool:

```toml
[mcp_servers.agent-router.tools.status]
approval_mode = "approve"

[mcp_servers.agent-router.tools.advice]
approval_mode = "approve"

[mcp_servers.agent-router.tools.list_jobs]
approval_mode = "approve"

[mcp_servers.agent-router.tools.job_progress]
approval_mode = "approve"

[mcp_servers.agent-router.tools.job_result]
approval_mode = "approve"

[mcp_servers.agent-router.tools.wait_job]
approval_mode = "approve"
```

Keep `submit_job`, `cancel_job` and `rate_job` on the default prompt. Setting
`default_tools_approval_mode = "approve"` under `[mcp_servers.agent-router]` approves all tools
at once. This is a choice for the user, since `submit_job` then starts jobs without asking.

These Codex approvals are separate from job approvals on the gateway. Approving a job on the
gateway is still a human action — the MCP server never approves; see
[Approvals stay with the human](#approvals-stay-with-the-human).

Tell Codex to pass `caller = "codex"` in `submit_job` and `advice`, so its jobs skip the
`chatgpt-plus` pool through the `[callers]` mapping. The jobs Codex runs as a harness
(`codex exec` started by the gateway) use `--ignore-user-config`, so they never load this MCP
server — no recursion.

### Cline and Kimi Code

Their agent-router integrations install a delegation skill, not an MCP config, so there
is no ready-made config path to copy. Add this entry in the harness's MCP settings:

```json
{
  "mcpServers": {
    "agent-router": {
      "command": "agent-router",
      "args": ["mcp"]
    }
  }
}
```

## Non-default home or gateway address

- `AGENT_ROUTER_HOME` — gateway home directory with `config.toml`, tokens and stores.
- `AGENT_ROUTER_URL` — gateway address, when it does not run on the default port.

The `agent-router mcp` process is started by the harness, and some MCP clients start
stdio servers with a reduced environment. Pass the variables in the server entry itself:

```bash
claude mcp add agent-router --env AGENT_ROUTER_HOME=<home> -- agent-router mcp
```

Put the server name before `--env`: the option takes several values, and a name after it
is read as one more variable.

In an `mcpServers` entry, add `"env": {"AGENT_ROUTER_HOME": "<home>"}` next to `command`
and `args`. In an OpenCode `opencode.json` entry, use `"environment"` instead of `env`.
