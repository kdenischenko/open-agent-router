# Glossary

**provider** — the vendor that owns a subscription's limits (Anthropic, Moonshot, OpenAI, OpenCode).
A pool is one subscription with a provider: its windows track that subscription's limits.

**harness** — the runtime a job runs in (Claude Code, Kimi CLI, OpenCode CLI, Codex, Cline). A
harness adds spend records to the journal; it never limits anything itself — limits belong to pools.

**usage source** — where a pool reads the used share of its windows from, for example a vendor
usage endpoint. A source reports fields with a fraction and a reset time; the pool maps those
fields onto its window ids (see `[pools.usage]` in [docs/INSTALL.md](docs/INSTALL.md)).
