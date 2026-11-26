# Install agent-router from scratch

Follow this guide if you downloaded the repository and want to install the gateway. When you finish,
you will have a running gateway, verified harnesses, and the delegation skill in each orchestrator.
If you installed the released package with `uv tool install open-agent-router`, run
`uv tool update-shell` once if `agent-router` is not found, skip steps 1 and 2 and start at
step 3.
Windows is supported; the commands use PowerShell. Linux and macOS are experimental:
CI runs the tests on Linux and macOS as experimental jobs that may fail, but the gateway has not
been checked live there.

## 0. What you need first

- Python 3.12, [uv](https://docs.astral.sh/uv/), and git.
- At least one harness, installed and signed in using its own method:

| Harness | Executable | Authentication | Pool in config |
|---|---|---|---|
| Kimi Code | `kimi` | `kimi login` | `kimi-code` |
| OpenCode | `opencode` | `opencode auth login`, OpenCode Go provider | `opencode-go` |
| Cline CLI | `cline` or `cline-cli` | `cline auth <provider>` sets the provider (OpenCode Go here), key and model; see the [official Cline CLI documentation](https://docs.cline.bot/usage/cli-overview) | `opencode-go` |
| Claude Code | `claude` | Sign in with a subscription | `claude-pro` |

Check that each command responds to `--version`. Harness jobs need no provider keys in the
gateway: it starts harnesses, and they access models using their own authentication. Optional
usage sources and remote classifiers read their own keys from configured environment variables.

## 1. Download and build

```powershell
git clone https://github.com/kdenischenko/open-agent-router.git agent-router
cd agent-router
uv sync
```

## 2. Add the `agent-router` command to PATH

Delegation skills call `agent-router` by name, so the command must be in PATH, not only in `.venv`.
The package is named `open-agent-router` and `uv tool list` shows it under that name, while the
command is `agent-router`.

```powershell
uv tool install --editable .
agent-router --help
```

To install with dependency versions pinned to `uv.lock`, export them as constraints first:

```powershell
uv export --no-hashes --no-dev --no-emit-project -o constraints.txt
uv tool install --constraints constraints.txt <source>
```

Use `.` as `<source>` to install the cloned repository, or `open-agent-router` to install the
released version from PyPI. The `--editable` option makes `git pull`
update the installed version immediately. If `agent-router` is not found, run `uv tool update-shell`
and open a new terminal. `agent-router <command> --help` describes a command and its options; the
“Commands” section in [README](../README.md) has examples.

## 3. Create the config and tokens

```powershell
agent-router init --allowed-root C:/work/projects
```

Install and sign in to at least one harness CLI (Kimi Code, OpenCode, Cline or Claude Code)
before running `init`. The `opencode-go` limits in the template are placeholders; replace them
with your plan limits. After `agent-router serve`, run `agent-router advice --tier standard` to
see which routes can take a job. `agent-router wait <job-id>` exits 4 while the job is still
running; run it again or use `--timeout`.

Pass `--allowed-root` for every directory the gateway may run jobs in. It is added to
`tasks.allowed_roots` in `config.toml`: on the first run `init` creates the file from the
template, on later runs it updates the existing config — nothing is overwritten. A running
gateway reads `config.toml` only at start, so restart `agent-router serve` after adding a root. Without the
option, an empty `allowed_roots` denies all jobs and `init` prints the command with your
current directory. Ready-made configs for typical setups are in [`docs/examples/`](examples/). To use one,
run `agent-router init` once, copy the example over the `config.toml` at the path printed by
`init`, and adjust its limits. Then run `agent-router init --allowed-root <dir>` again for each
of your directories: an example starts with an empty `allowed_roots`.

The command prints the gateway home directory (on Windows,
`%LOCALAPPDATA%\agent-router\agent-router`), the path to `config.toml`, the paths to two tokens, the
harnesses it found in PATH or in `[backends]`, hints about routes and limits to fix, and the next
steps with the dashboard address. The commands for a first job appear once a route that takes
read jobs is enabled and a directory is allowed. If `config.toml` is invalid, `init` prints the error and exits
with code 1.
Tokens are not printed, and CLI commands read them automatically. The dashboard asks for the
admin token once: paste the contents of `admin.token`, for example the output of
`Get-Content <path to admin.token>`. Do not publish the tokens.

Set `AGENT_ROUTER_HOME` to use a different gateway home directory. This is useful for a trial
installation alongside your working one. In that case, set a different `server.port` in its config
and set the same variable in every terminal where you run `agent-router`.

## 4. Configure `config.toml`

Open the file whose path was printed by `init`. At minimum, configure the following:

1. **Job directories.** An empty `allowed_roots` denies all jobs. Instead of editing the file,
   `agent-router init --allowed-root C:/work/projects` adds a directory — also to an existing
   config:

   ```toml
   [tasks]
   allowed_roots = ["C:/work/projects"]
   denied_roots = ["C:/work/projects/secret-repo"]
   ```

   Each entry must be a non-empty absolute path after `~` expansion: `~/work` is valid
   and portable, while a relative or empty entry is a config error. The lists themselves
   may stay empty.

2. **Routes.** `init` leaves active only the `[[routes]]` whose harness it found in PATH; the
   others are commented out. Uncomment a route after you install its harness, and set
   `enabled = true` on the routes you want to use (`cline/glm-5.3` starts disabled, while
   `claude-cli/sonnet` starts enabled). Write the route model as the harness understands it:
   `kimi-code/k3.1-pro` for Kimi, `opencode-go/<model>` for OpenCode and Cline. Every route that allows `write` has
   `confirm = true`: its jobs wait for `agent-router approve`. Turn it off per route once you trust
   the setup; see [SECURITY](../SECURITY.md). Every harness route needs a non-empty `pool`,
   `backend` and `model`; a route with any of them missing or empty is a config error.

3. **Harness not in PATH.** Specify the path explicitly:

   ```toml
   [backends.claude]
   executable = "C:/Users/<you>/AppData/Roaming/npm/claude.cmd"
   ```

4. **Limits.** The `[[pools.windows]]` windows in the template are examples. Every
   `limit_usd = 10  # set your plan limit` is a placeholder: replace it with the limit of your
   `opencode-go` plan. Leave any extra pools in place: they are not used without routes. A fixed
   window may set `max_age = "2h30m"` — a span like `75m`, `3h` or `2d`. A calibration or usage
   read older than `max_age` is shown as `no data: observation older than max age` until a newer
   one replaces it; when `max_age` is absent, half the window duration applies, capped at 72 h
   (`5h` → `2h30m`, `7d` → `72h`, `1mo` → `72h`).

5. **Prices for Cline.** Cline does not report job costs, so without a price table its spending is
   not counted toward the `opencode-go` limit share. Create `prices-opencode-go.toml` next to
   `config.toml` (USD per 1 million tokens, `[input, output]`; get prices from your provider).
   Replace the zeros with your actual prices:

   ```toml
   "glm-5.3" = [0.0, 0.0]  # put your prices per 1M tokens: [input, output]
   "glm-5.3-flash" = [0.0, 0.0]  # put your prices per 1M tokens: [input, output]
   ```

   The key is the model name without the provider prefix and the `#effort` suffix
   (`opencode-go/glm-5.3#high` → `glm-5.3`); if it is missing, the route's `model_key` is tried.

   Go has one shared limit, but models consume it at different rates (price / model monthly limit).
   `price_file` contains raw provider prices, while model weights are set in the pool:
   `limit_base_usd` is the base (the model limit used to calculate the windows), and monthly limits
   are set in `[pools.model_limits]` by `model_key`. `model_limits_date` records when the limits were
   checked against the console. `doctor` shows its age and models without a limit.

   Add these keys to the existing `opencode-go` pool in `config.toml`; a second `[[pools]]` block
   with the same id fails with a duplicate id error. Put `[pools.model_limits]` after the pool's
   windows:

   ```toml
   [[pools]]
   id = "opencode-go"
   price_file = "prices-opencode-go.toml"
   limit_base_usd = 180
   model_limits_date = "2026-11-16"
   # the other keys and [[pools.windows]] of the pool stay as they are

   [pools.model_limits]
   "glm-5.3-flash" = 180
   "glm-5.3" = 120
   ```

6. **Dashboard sign-in.** The dashboard (`http://127.0.0.1:8787/`) uses the administrative token
   and sets a signed cookie that survives gateway restarts. `server.panel_session_days` controls the
   session length (default 30, from 1 to 365). Signing out of the dashboard or changing `admin.token`
   immediately signs out all browsers.

   Each pool card on the Limits page includes spend history from the journal: hourly buckets for
   the last 24 hours and local-calendar-day buckets for the last seven days. USD pools show cost;
   other pools show input plus output tokens. Estimated buckets are marked, and every inline SVG
   chart has an adjacent table with the same values. A pool without journal entries shows
   `no spend recorded`.

   The **Settings** page at `/settings` shows the loaded pools and windows, all routes including
   disabled ones, caller mappings, and configuration-related `doctor` findings with hints. It is
   read-only and works without JavaScript. Only environment variable names are displayed, never
   their values, gateway tokens or other secrets.

   You can also submit a job from the dashboard: the **New job** page at `/jobs/new`, linked from
   the Jobs page, takes a working directory, a `read` or `write` mode, an optional tier (leave it
   empty to let the gateway choose), the prompt and an optional timeout in seconds. The form works
   without JavaScript, shows validation errors beside the fields while keeping the entered values,
   and redirects to the job page on success; with `confirm = true` on the chosen route, the job
   still waits for your approval.

   ```toml
   [server]
   panel_session_days = 30
   ```

7. **Classifier (optional).** A model assesses the job tier (`cheap|standard|strong`). It can be
   local — [Ollama](https://ollama.com) with downloaded models (`ollama pull <model>`) or any
   OpenAI-compatible server — or a cloud provider over `https` with a key from an environment
   variable. Multi-model form:

   ```toml
   [classifier]
   mode = "shadow"          # only record votes; "active" lets the classifier choose the tier
   primary = "tev1"         # the vote that decides in active mode and is recorded in the job
   min_confidence = 0.6     # below this, use the default standard tier
   write_floor = "standard" # writing jobs must not be below this tier
   vram_budget_mb = 5300    # total size_vram of live local Ollama models; doctor warns if exceeded

   [[classifier.models]]
   id = "tev1"
   kind = "systemone"       # decision model via /v1/systemone
   model = "tev1:4b-q4_K_M"
   timeout_s = 3

   [[classifier.models]]
   id = "lfm3"
   kind = "ollama"          # chat model via /api/chat
   model = "lfm3:1.7b-q4_K_M"
   timeout_s = 5
   ```

   Both local kinds (`systemone` and `ollama`) ask Ollama to keep the model loaded for 30 minutes.
   Set `timeout_s` high enough to cover a cold model load, which can happen after a longer idle
   period. A model can have its own `min_confidence` and `live = false` (only for `classifier eval`).
   The third kind, `openai`, talks to any OpenAI-compatible chat completions server. A model outside
   loopback needs `url = "https://..."` and `api_key_env = "<VARIABLE>"`. Examples for every kind,
   the privacy note and measured results are in [CLASSIFIER.md](CLASSIFIER.md). The old
   single-model form (`kind`, `url`, `model`, and `timeout_s` directly in `[classifier]`) still
   works; do not mix it with `[[classifier.models]]`. `kind = "none"` disables the classifier.
   Enable `active` mode after a week of `shadow` data and checking
   `agent-router classifier report`. If the classifier does not respond within `wait_s` (by
   default, the `primary` timeout plus 0.5 seconds), the job starts with `standard`.

8. **Job timeouts.** A job is stopped at `tasks.default_timeout_s` (default 14400 s, a hard cap),
   or when its harness prints nothing to stdout or stderr for `tasks.idle_timeout_s`
   (default 600 s; `0` disables the check). `--timeout` on `run`, or a route's `timeout_s`,
   replaces the hard cap, but the idle limit still applies. Claude Code emits JSON events while
   it runs, so the idle limit applies to its jobs too.

   When a harness reports the route's model as unavailable (OpenCode prints
   `provider.no-route: Model unavailable: <provider>/<model>`), that route alone is taken out of
   rotation for `tasks.model_unavailable_cooldown_s` (default 1800 s); the other routes of the
   same pool keep serving. Until the cooldown ends, `advice` and the dashboard show the route as
   excluded with `model unavailable, retry after <local time>`, and a route the caller pinned
   still runs.

   Kimi Code is silent while it writes a long answer or waits for a subagent, so for kimi jobs
   the CPU time of the harness process tree and the files changed in the working directory count
   as activity as well; the job is then not stopped as idle while it is still doing something.
   The hard cap applies either way.

   ```toml
   [tasks]
   default_timeout_s = 14400
   idle_timeout_s = 600
   ```

9. **Retention (optional).** Jobs, prompts, results and attempt logs are kept forever by default.
   Set `storage.retention_days` to a positive number of days to make them eligible for manual
   cleanup:

   ```toml
   [storage]
   retention_days = 30
   ```

   Run `agent-router purge --dry-run` to see how many job rows and leftover directories would be
   removed, then `agent-router purge` to delete their database records and `jobs/<job-id>/`
   directories. Only terminal jobs whose finish time is older than the limit are removed; queued,
   running and awaiting-approval jobs are always kept. Purge also removes orphaned
   `jobs/<job-id>/` directories left by crashes or older versions once none of their files has
   been modified for longer than the retention period, and it retries directory removals that
   failed during an earlier purge. `--dry-run` reports these orphans and retries separately from
   job rows in its counts but deletes nothing. Usage and spend records are kept for pool window
   accounting. Purge never runs automatically when the gateway starts.

10. **Usage sources (optional).** A usage source reads the used share of a plan's windows from
    the vendor (for example a usage endpoint) instead of manual calibration. The root table sets
    the defaults for all pools:

    ```toml
    [usage]
    mode = "auto"    # "auto" reads usage sources; "manual" is calibration only
    interval = "1h"  # how often sources are read; a span like 1h or 30m, at least 1m
    ```

    Each pool says which source feeds it, how source fields map onto its window ids, and, for
    sources that need a key, which environment variable holds it (`api_key_env`, a variable
    name — never the key itself). The sub-table goes after the pool's `[[pools.windows]]`:

    ```toml
    [pools.usage]
    source = "opencode-go"
    mode = "manual"  # optional; omit to inherit [usage].mode
    api_key_env = "OPENCODE_GO_API_KEY"
    windows = { rolling = "5h", weekly = "week", monthly = "month" }
    ```

    Every key of `windows` is a field the source reports; its value is a window id of that pool.
    An unmapped field is ignored, two fields cannot map to the same window, and a `source` needs
    at least one mapped window. An unknown `source` still fails with `unknown usage source`. The
    package ships two usage sources: `opencode-go` and `claude-code`.

    To enable it for the OpenCode Go pool:

    1. Create an API key in the [OpenCode console](https://opencode.ai).
    2. Set the environment variable named by `api_key_env` (`OPENCODE_GO_API_KEY` by default)
       for the user that runs the gateway, so the process started with `agent-router serve`
       sees it.
    3. Add the `[pools.usage]` sub-table to the OpenCode Go pool — the example above shows
       exactly this table.

    The gateway reads the key from the environment on every request; it never stores the key and
    never writes it to a log, a reason code or an error message.

    The `claude-code` source reads the Claude plan's session and weekly figures from the
    `/usage` command of the Claude Code CLI, so the Claude pool gets fresh `5h` and `week`
    figures even when no interactive session feeds the status line. It needs no API key:

    1. Install the Claude Code CLI and sign in once, as the user that runs the gateway:
       `claude auth login`. `claude auth status` should show `"loggedIn": true`.
    2. Add the sub-table to the Claude pool:

       ```toml
       [pools.usage]
       source = "claude-code"
       windows = { session = "5h", weekly_all = "week" }
       # executable = "C:/path/to/claude.exe"  # optional; default: claude from PATH
       ```

    Each read starts the CLI in print mode without your settings, hooks or MCP servers; the
    command runs locally and does not call the model, so a read costs no tokens. The gateway
    never reads the CLI's credentials and never passes a token. The usage endpoint behind
    `/usage` is rate limited, so keep `[usage].interval` at its default of one hour or longer.
    A failed read reports `not_found` (no CLI), `timeout` (no valid report was parsed),
    `unauthorized` (the CLI is not signed in), `rate_limited` or `malformed_response`. A process
    failure after a valid report keeps the points and is shown as a warning by `agent-router
    status`, `doctor` and the dashboard pool card instead.

    **When sources are read.** With `mode = "auto"` the gateway reads every pool's source on its
    own schedule: about once per `interval`, and never more than once a minute per pool. You can
    also force a read at any time:

    - `agent-router usage refresh [POOL]` — one pool, or every pool with a source;
    - the **Refresh limits** button on each pool card of the dashboard, and one global button on
      the limits page;
    - `POST /api/v1/usage/refresh` with the admin token and an optional `{"pool": "<id>"}` body.

    A pool whose last read (successful or not) is younger than 60 seconds is skipped with the
    reason `too_soon`; a pool in manual mode is never read and shows no button. A successful read
    is recorded the way a manual calibration is: the newest observation of a window wins.
    A failed read reports a reason (`unauthorized`, `timeout`, `error`, `no_data` when no point
    maps onto a window, …) and writes nothing. A Claude Code process failure after a valid report
    is instead a successful read with a warning, so its points are kept; `timeout` is a reason only
    when no valid report was parsed.

    **Where a figure comes from, and whether it is fresh.** Every pool card on the limits page
    shows a line like `Source: opencode-go (auto) · read 12 min ago` under its title when it has a
    usage source: the source, its mode, the age of the last successful read (or `last read failed:
    <reason>`), any `warning: <warning>` from a read that kept usable points, and a `stale` badge.
    Each window shows its `figure` next to its data age: `auto` (the pool's usage source), `manual`
    (a calibration) or `journal` (no observation in the current window — the figure comes from
    spend tracking or a scheduled reset). A pool in `auto` mode is **stale** when no successful
    read is fresher than twice `[usage].interval` (never read counts as stale); manual mode never
    goes stale. `agent-router status` prints the same facts on one usage line per pool with a
    source, and the figure of every window.

    `doctor` adds its own warnings for usage sources: a pool whose `api_key_env` (or the source's
    default variable, `OPENCODE_GO_API_KEY` for `opencode-go`) is not set in the environment of
    the terminal that runs `doctor`; for `claude-code`, a CLI that cannot be found or is not
    signed in; a pool whose last read failed or returned a non-fatal warning; stale data; and
    `auto` mode without a `source` on a pool that has one. Two more warnings cover usage-source
    drift: one per mapped field the source stopped returning (`usage source <source> no longer
    reports <field>; window <window> falls back to the journal; update agent-router or switch the
    pool to mode = "manual" and calibrate`), and one after three consecutive
    `malformed_response`/`no_data` reads (`usage source <source> response format may have changed;
    update agent-router or switch the pool to mode = "manual" and calibrate`); the dashboard pool
    card shows the same texts. Both mean the vendor's answers no longer match what this agent-router
    version expects: update agent-router to a version that understands the new shape, or switch the
    pool to `mode = "manual"` in `[pools.usage]` and calibrate its windows by hand until then. See
    [TROUBLESHOOTING.md](TROUBLESHOOTING.md#usage-sources-do-not-refresh) for the fixes.

For more about pools, model groups, `priority`, and `weight`, see “How to add a subscription or
harness” in [README](../README.md).

### Keeping writes inside the job directory

`allowed_roots` controls where a job starts, not where its harness can write. The gateway asks every
harness in the prompt to work only inside that directory, but this is not a security boundary.

**Kimi Code.** The gateway runs `kimi -p` non-interactively, with no separate write-mode approval
flag. When the prompt is passed by file, it also passes `--add-dir` for the prompt file's directory
(`jobs/<job id>/` under the gateway home); this adds a workspace directory rather than restricting
access to it. Kimi Code has no documented setting the gateway knows of that restricts or asks before
writes outside the working directory; see the harness's documentation.

**OpenCode.** Write jobs pass `--auto`, which auto-approves permissions that are not explicitly
denied. Add this rule to `opencode.json` to deny access outside its working directories:

```json
{
  "permission": {
    "external_directory": "deny"
  }
}
```

Use `deny`, not `ask`, for unattended gateway write jobs: `--auto` can approve a permission that is
not explicitly denied.

**Cline.** Write jobs pass `--auto-approve true`, which enables tool auto-approval for all tools.
Cline has no documented setting the gateway knows of that restricts or asks before writes outside
the working directory; see the harness's documentation.

**Claude Code.** Write jobs pass `--permission-mode bypassPermissions` and load only project
settings. Claude Code's `--restricted` option confines file tools to its working directories, but
it refuses `bypassPermissions`, so it cannot be combined with the gateway's current write jobs.
The gateway knows no documented setting that confines writes while `bypassPermissions` is in use;
see the harness's documentation.

## 5. Start the gateway

In a separate terminal that remains open:

```powershell
agent-router serve
```

The gateway listens only on `127.0.0.1:8787` (the port is `server.port`).

**Start automatically when you sign in to Windows without a console window.** Administrator rights
are not required. Create `start-hidden.vbs` in the gateway home directory:

```vb
Set sh = CreateObject("WScript.Shell")
' change if `uv tool dir --bin` prints another directory
exePath = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.local\bin\agent-router.exe"
home = sh.ExpandEnvironmentStrings("%AGENT_ROUTER_HOME%")
If home = "%AGENT_ROUTER_HOME%" Then home = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\agent-router\agent-router"
sh.Environment("Process")("AGENT_ROUTER_HOME") = home
sh.Run "cmd /c """"" & exePath & """ serve >> """ & home & "\serve.log"" 2>&1""", 0, False
```

Then put it in your Startup folder and run it now (`<gateway home directory>` is the path printed by
`init`):

```powershell
$src = "<gateway home directory>\start-hidden.vbs"
Copy-Item $src "$([Environment]::GetFolderPath('Startup'))\agent-router.vbs" -Force
wscript.exe $src
```

> **If you install through an agent in a desktop app** (Claude Desktop and other MSIX packages):
> Windows redirects everything the agent creates in `AppData` to
> `%LOCALAPPDATA%\Packages\<package>\LocalCache\…`. This directory is not visible from outside, and
> autostart will not find it. Before `init`, choose a directory outside `AppData` and set it
> permanently: `setx AGENT_ROUTER_HOME "$HOME\.agent-router"`, then open a new terminal.

Gateway output is written to `serve.log` next to the config. To stop the gateway, end the
`agent-router.exe` process. To remove autostart, delete `agent-router.vbs` from the Startup folder.

## 6. Verify

Run `doctor` first. `doctor --live` is optional: a fresh setup has `confirm = true` on every route,
so it has nothing to probe until a route has `confirm = false`.

```powershell
agent-router doctor
agent-router doctor --live
```

`doctor` shows whether the config loaded, the database is accessible, which harnesses were found,
which routes are enabled, and whether the gateway responds. The warning “route … is enabled, but
harness … not found” means you need `[backends.<name>] executable`, or you should disable the route.
The warning “route …: counter … does not see harness …” means the pool's counter cannot account for
that route's spending, so the pool's `usd` window stays invisible; set `spend = "journal"` on the
route to record its jobs in the journal. The warning “route …: pool … has no counter, so its spend
is invisible; set `spend = "journal"`” means the route inherits the `counter` source on a pool that
has no counter at all.

`--live` runs one short read job per harness in the first directory from `allowed_roots` and uses
a small amount of the limit. It probes only enabled routes that allow `read` and have no
`confirm`. If no routes qualify, it prints
`no routes to probe (disabled, confirm or write-only routes are skipped)` and exits `1`.
To probe a harness, set `confirm = false` on one of its routes and limit that route to
`modes = ["read"]`. The expected result is then `Live <route>: completed` for each harness.

## 7. Calibrate limits

The gateway chooses a route based on usage in each window. Until a window is calibrated, `status`
shows “no data”, and the route choice is blind. Check the percentages in your subscription interface
and record each pool window:

```powershell
agent-router calibrate kimi-code 5h 20
agent-router calibrate kimi-code month 35 --resets-in 12d
agent-router calibrate opencode-go 5h 10
agent-router calibrate opencode-go week 25
agent-router calibrate opencode-go month 40 --resets-in 20d
agent-router status
```

- A `kind = "fixed"` window (month, Claude 5h and week) takes a reset time no further than one
  window ahead: `--resets-in 12d` or `--resets-at 2026-12-01` (a date alone means midnight; add a
  local time as `"2026-12-01 14:00"`; `01.12.2026` also works). A reset more than five minutes
  past that is rejected. Specify it once: later calibrations without a reset time keep the last
  one, moved forward by the window length. With no reset time ever recorded, `status` shows
  `reset: unknown`, and the router ranks that pool after pools with a known pace. The
  dashboard's Calibrate page asks for the provider first, then its window and the reset date.
- A `kind = "rolling"` window (`5h` and `week` for OpenCode Go, `5h` for Kimi Code) has no reset
  time: calibrate it without a reset, and record it again once the window length has passed.
- After recording, the CLI prints `Recorded: <pool> / <window>, <percent>%, resets <local time>`
  (the reset kept from an earlier calibration when none was given; without the reset tail when
  no reset is known), and the dashboard opens with a `Recorded ...` line above the limits.
- For `opencode-go`, after calibration the gateway automatically adds spending from the OpenCode
  counter and the Cline job log.

Repeat calibration from time to time. Between calibrations the stored usage moves only where the
gateway can see it:

- a USD pool (`opencode-go`) adds the cost of gateway jobs and the counters above;
- `claude-pro` follows the Claude Code status line while it is connected (section 10);
- a percent pool without a feed (`kimi-code`) keeps the last recorded value.

`status` shows `via gateway since the last observation: <n> jobs, <min> min, <tokens> tokens`
under a pool that ran gateway jobs after its last observation. That line means the stored
percentage is behind; calibrate the pool again once it grows.

## 8. First job

```powershell
agent-router advice --tier standard
agent-router run --no-wait --cwd C:/work/projects/my-repo --mode read "Describe the structure of this repository"
agent-router approve <job-id>
agent-router wait <job-id>
```

`advice` shows which routes are suitable and why the others are excluded. `run --no-wait`
prints the job id. The template routes have `confirm = true`, so the job waits until you
approve it with `agent-router approve` or on the dashboard; a route with `confirm = false`
starts at once. Without `--no-wait`, `run` waits up to 100 seconds and prints
`job awaits approval: agent-router approve <id> before <time>, then agent-router wait <id>`
when the job awaits approval; for longer jobs, use `agent-router wait <id> --timeout 1500`.
Without a decision the approval expires after `tasks.approval_timeout_s` (default 600 s): the
job ends as `declined` with error `approval_expired` and does not move to another route.
A writing job uses `--mode write`; check its changes yourself with `git diff`. Rate the result
with `agent-router rate <id> good|bad|partial`. Ratings are used by `classifier report` and
`agent-router export`. The dashboard job page refreshes itself, shows the error, and lets
you rate a finished job. Attempt, token, cost and duration totals, a per-attempt timeline,
and expandable prompt, result and output blocks keep the details together; copy buttons
appear when clipboard scripting is available.

## 9. Delegation skill for orchestrators

To let an agent delegate jobs through the gateway, copy your harness's skill into its skills
directory. The skills are not part of the PyPI package: run this from the root of a clone of the
repository (remove unused lines for harnesses you do not use). With PowerShell on Windows:

```powershell
$targets = @{
  "claude-code" = "$HOME/.claude/skills"
  "kimi-code"   = "$HOME/.kimi-code/skills"
  "opencode"    = "$HOME/.config/opencode/skills"
  "cline"       = "$HOME/.cline/skills"
}
foreach ($h in $targets.Keys) {
  New-Item -ItemType Directory -Force $targets[$h] | Out-Null
  Copy-Item -Recurse -Force "integrations/$h/agent-router-delegate" $targets[$h]
}
```

With bash or zsh (Linux, macOS, Git Bash):

```bash
while read -r harness dir; do
  mkdir -p "$dir"
  cp -R "integrations/$harness/agent-router-delegate" "$dir"
done <<EOF
claude-code $HOME/.claude/skills
kimi-code $HOME/.kimi-code/skills
opencode $HOME/.config/opencode/skills
cline $HOME/.cline/skills
EOF
```

The directory is created first: without it, `Copy-Item` and `cp -R` would copy the skill under
the name `skills`.
The skill is visible from the next harness session. Each skill passes `--caller <harness>`, and the
gateway uses `[callers]` to map the caller to its own pool, so that the job is not routed back
to the caller's subscription. When the gateway picks the route itself, it never sends a
delegated job back to the caller's own pool (an explicit `--route` is not checked); in a
one-pool setup leave the callers unmapped (`""`), or the jobs have no route.

As an alternative, orchestrators that support MCP can use the built-in MCP server
instead of a skill: `agent-router mcp` serves the job operations over stdio and needs
the running gateway. Registration for each harness is in [MCP.md](MCP.md).

## 10. Claude Code status line (optional)

The status line sends Claude Pro limit percentages to the gateway, so you do not need to calibrate
the `claude-pro` pool manually:

```powershell
agent-router doctor --statusline-snippet
```

Insert the printed `statusLine` key into `~/.claude/settings.json`. If you already have a status line,
it will be replaced. After this, `doctor` shows the age of the last signal. The signal reports Claude
limits only, so `[statusline] pool` must name the pool of your Claude Code routes (`claude-pro` in
the template); the examples without Claude Code leave `[statusline]` out.

## Not available yet

- Routing from within Cline and OpenCode (as a model provider) is planned.
- Cline runs with the owner's data directory: every run through the gateway adds a job to Cline's
  history.

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for more failure cases and detailed recovery steps.

| Symptom | What to do |
|---|---|
| `no config at …: run agent-router init first` | `init` was not run for this gateway home directory (`AGENT_ROUTER_HOME`) |
| `config error: …` | A TOML typo or an invalid value; the error text names the field |
| Code `5`, “gateway unavailable” | `agent-router serve` is not running, or another port is used (`AGENT_ROUTER_URL`) |
| Code `6`, “request denied” | The directory is outside `allowed_roots`, the route is disabled, or the queue is full |
| `Live …: failed` | Run the harness manually with the same model: authentication usually has not been completed |
| `status`: “no data” | The window is not calibrated (step 7) |
| `shim argument contains an unsafe character '#'` | OpenCode is installed as `opencode.cmd` (npm), and a model with effort `#variant` is not passed through `.cmd`. Specify the `.exe` directly: `[backends.opencode] executable = ".../npm/node_modules/@opencode/cli/bin/opencode.exe"` (the path is in the last line of `opencode.cmd`) |
| `variant unavailable for opencode-go/<model>: <variant>` | The model does not have this effort. See the model's `reasoning_options` in `~/.cache/opencode/models.json`; if empty, do not add a `#…` suffix |
