# Tier classifier

The classifier reads a job's prompt and votes for its tier: `cheap`, `standard` or `strong`.
It is optional. Without it, the tier comes from the caller (`--tier`) or defaults to `standard`.

A classifier is a model behind an HTTP endpoint. It can run on your machine (Ollama, LM Studio,
a llama.cpp server) or at a cloud provider. You can configure several models at once. All of them
vote on every job, one of them (`primary`) decides, and `agent-router classifier report` compares
them against the tiers you confirm.

## Modes

- `shadow` (start here): votes are recorded, but the tier still comes from the caller or the
  default. Use it to measure the classifiers on your own jobs.
- `active`: the `primary` vote sets the tier of a job submitted without `--tier`. The job waits
  for the vote up to `wait_s` (by default the primary's `timeout_s` plus 0.5 seconds). A missing
  vote, an error or a confidence below `min_confidence` gives `standard`. A write job is never
  classified below `write_floor` (`standard` by default).

An unavailable classifier never stops the gateway or rejects a job.

## What the classifier receives

The job prompt, or for a prompt longer than 4000 characters its first 3000 and last 1000
characters. It does not receive the working directory, file contents or tokens. A cloud
classifier therefore sends that text to the provider: use one only for jobs you would send there
anyway.

## Kinds

| `kind` | Request | Answer | Confidence |
|---|---|---|---|
| `systemone` | `POST {url}/v1/systemone` | score of a tier question | the class probabilities of the answer |
| `ollama` | `POST {url}/api/chat` with a JSON schema | `{"tier", "confidence"}` | token log-probabilities of the tier value |
| `openai` | `POST {url}/chat/completions` with `response_format` | `{"tier", "confidence"}` | token log-probabilities if the server returns them, otherwise the model's own estimate |

- `systemone` is for decision models that answer scoring questions instead of chat (for
  example `tev1` in Ollama, or `jev` in OpenCode Zen). Such models usually do not accept chat.
- `ollama` is for an ordinary chat model in Ollama.
- `openai` is for any server with the OpenAI chat completions API. The `url` includes the
  version path (`.../v1`). The server must support `response_format` with a strict JSON
  schema. A server that rejects it answers with an HTTP error, and every vote ends with
  `http_<status>` (for example `http_400`); `bad_answer` means the server answered, but not
  with a valid tier.

## Addresses and keys

- A loopback address (`127.0.0.1`, `::1`, `localhost`) is always allowed.
- Any other address must use `https` and name an environment variable with the key in
  `api_key_env`. Otherwise the config is rejected.
- The key is read from that variable at every request and sent as `Authorization: Bearer`. It is
  never written to the config, the store, logs or votes. If the variable is missing or empty, the
  vote ends with the error `no_api_key` and no request is made.
- `agent-router doctor` shows for a remote model only whether the variable is set.

Set the variable for the process that runs the gateway (for the Windows autostart task, in the
user environment), then restart the gateway.

## Examples

All examples go into `config.toml`. The `[classifier]` table holds the common settings; each
model is a `[[classifier.models]]` entry with `id`, `kind`, `model`, `url`, `timeout_s` and
optional `api_key_env`, `min_confidence` and `live` (`false` — only for `classifier eval`).

```toml
[classifier]
mode = "shadow"
primary = "tev1"
min_confidence = 0.6
write_floor = "standard"
vram_budget_mb = 5300   # total size_vram of live local Ollama models; doctor warns above it
```

**Local decision model in Ollama.** `ollama pull tev1:4b-q4_K_M` first.

```toml
[[classifier.models]]
id = "tev1"
kind = "systemone"
url = "http://127.0.0.1:11434"
model = "tev1:4b-q4_K_M"
timeout_s = 3
min_confidence = 0.4
```

**Local chat model in Ollama.**

```toml
[[classifier.models]]
id = "lfm3"
kind = "ollama"
url = "http://127.0.0.1:11434"
model = "lfm3:1.7b-q4_K_M"
timeout_s = 5
```

**Local OpenAI-compatible server** (LM Studio, llama.cpp `llama-server`). The model name is the
one the server lists in `/v1/models`.

```toml
[[classifier.models]]
id = "studio"
kind = "openai"
url = "http://127.0.0.1:1234/v1"
model = "<model name>"
timeout_s = 5
```

**Cloud decision model** (`jev` in OpenCode Zen, billed by the provider).

```toml
[[classifier.models]]
id = "jev"
kind = "systemone"
url = "https://opencode.ai/zen"
model = "jev-1.13"
api_key_env = "OPENCODE_API_KEY"
timeout_s = 5
```

**Cloud OpenAI-compatible provider** (for example OpenRouter or Groq).

```toml
[[classifier.models]]
id = "router"
kind = "openai"
url = "https://openrouter.ai/api/v1"
model = "<provider>/<model>"
api_key_env = "OPENROUTER_API_KEY"
timeout_s = 8
```

A common setup is a local `primary` and one or two others in the same `shadow` run: after a week
`classifier report` shows which of them agrees with your tiers, and you switch `primary` or
`mode` accordingly.

The single-model form (`kind`, `url`, `model`, `timeout_s`, `api_key_env` directly in
`[classifier]`) still works; do not mix it with `[[classifier.models]]`. `kind = "none"`
disables the classifier.

## Measuring

- `agent-router classifier report` — per classifier: votes, errors, latency p50/p95, accuracy
  and the share of underestimated tiers. Ground truth is the tier you set with
  `agent-router rate <id> ... --should-tier`, then the caller's tier of jobs rated `good`. The
  caller's tier of unrated jobs is reported separately as a weak label. `--json` adds the tier
  matrix and a split before and after the last `mode` change.
- `agent-router classifier eval --models ID,...` replays past prompts through the chosen models,
  including `live = false` ones, and stores the votes as offline. Live local Ollama models are
  unloaded from VRAM for the run; cloud models are simply called, so each replayed prompt is a
  paid request.
- Pick `min_confidence` per model from the report: models report confidence on different
  scales (see below).

## Results so far

These are small samples from one machine. Treat them as a starting point for your own
`shadow` run, not as a ranking.

**Offline, 20 synthetic prompts.** Each prompt is 4000 characters: a short task and generated
context, labelled 7 `cheap`, 7 `standard` and 6 `strong`. Windows 11, RTX 4060 Laptop (8 GB),
Ollama 0.40.16. Memory is the growth of `nvidia-smi` usage after loading the model; latency is a
full request to a loaded model.

| Model | Kind | VRAM, MiB | p50 / p95, s | Matched | Under | Confidence, median (right / wrong) |
|---|---|---|---|---|---|---|
| `tev1:4b-q4_K_M` | `systemone` | 3866 | 0.38 / 0.41 | 15 / 20 | 3 | 0.41 (0.50 / 0.26) |
| `lfm3:1.7b-q4_K_M` | `ollama` | 1270 | 0.41 / 0.45 | 12 / 20 | 2 | 0.85 (0.85 / 0.85) |
| `tev1:0.8b-q8_0` | `systemone` | 1298 | 0.11 / 0.14 | 12 / 20 | 0 | 0.36 (0.38 / 0.33) |
| `d1-omni:600m` | `systemone` | 1410 | 0.10 / 0.13 | 10 / 20 | 7 | 0.28 (0.32 / 0.11) |
| `gemma4:e2b` | `ollama` | ~2980 | 0.28 / 0.35 | 10 / 20 | 0 | 0.95 for every answer |

"Under" counts prompts given a lower tier than the label: those are the costly mistakes, since
the job goes to a weaker model.

- `tev1:4b` was the most accurate, and its confidence separates right from wrong answers, but it
  rarely exceeds 0.6 (maximum 0.66). With the default `min_confidence = 0.6` almost every job
  in `active` mode would fall back to `standard`; use about 0.4 for it.
- The self-reported confidence of the chat models does not depend on the answer, which is why
  the `ollama` kind recomputes it from token log-probabilities.
- `tev1:0.8b` never answered `cheap`; `d1-omni` underestimated 7 of 20.
- Decision models ignore `num_ctx` in the request; their context size changes only through a
  Modelfile (`PARAMETER num_ctx 4096`). `size_vram` from `/api/ps` can be far below the real
  usage (273 MiB for `gemma4:e2b`), so keep `vram_budget_mb` with a margin.

**Live, `shadow` mode, real delegated jobs.** `tev1:4b-q4_K_M` (`primary`) and
`lfm3:1.7b-q4_K_M`, 27 jobs each, over two days.

| Model | Errors | p50 / p95, ms | Confirmed tiers: matched | Weak labels: matched | Weak labels: under |
|---|---|---|---|---|---|
| `tev1:4b-q4_K_M` | 2 | 516 / 5360 | 0 of 4 | 16 of 20 | 4 |
| `lfm3:1.7b-q4_K_M` | 0 | 312 / 4671 | 3 of 5 | 12 of 20 | 0 |

- On real jobs `tev1:4b` answered `standard` every time, so its weak-label score only reflects
  that most jobs were submitted as `standard`. `lfm3` never underestimated but put 8 of 16
  `standard` jobs into `strong`.
- p95 includes the first request after a model was unloaded.
- The cloud kinds (`jev` through `systemone`, `openai` providers) have not been measured yet.
