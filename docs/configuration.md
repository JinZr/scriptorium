# Configuration

Scriptorium separates reviewable manuscript configuration from machine-local provider routing.

## Manuscript configuration

Commit `scriptorium.toml` with the paper:

```toml
[manuscript]
main = "main.tex"
engine = "pdflatex"

[profiles.quick]
roles = ["substantive_review", "copyedit"]

[profiles.full]
roles = [
  "substantive_review",
  "copyedit",
  "consistency",
  "figure_review",
]
```

Stable role keys are `visual_transcription`, `substantive_review`, `copyedit`, `consistency`, `figure_review`, `revision`, and `verification`. `workflow` identifies deterministic Armarius activity and is not a model role. Classical role names are display labels only; configuration, storage, APIs, and logs use the stable keys.

## Local routing

Keep route selection and pricing in the Git-ignored `.scriptorium/config.toml`:

```toml
max_concurrency = 2

[roles]
substantive_review = "primary"
copyedit = "primary"
consistency = "primary"
figure_review = "primary"
visual_transcription = "visual"
revision = "primary"
verification = "primary"

[routes.primary]
runtime = "codex"
model_provider = "openai"
model = "USER_CONFIGURED_MODEL"
input_usd_per_million = 0
output_usd_per_million = 0
reasoning_effort = "high"

[routes.visual]
runtime = "codex"
model_provider = "openai"
model = "USER_CONFIGURED_MODEL"
input_usd_per_million = 0
output_usd_per_million = 0
reasoning_effort = "high"
```

Replace every model placeholder before a paid run. Scriptorium does not choose a concrete model or silently fall back to another route. Use `run retry --route ROUTE` when an operator explicitly wants a different route after failure.
`visual_transcription` must map explicitly to a route whose model can read the rendered page images. It is a separate task from the reviewers and Verifier; Scriptorium never falls back to a reviewer route. Additional named routes can be added when figure review or verification should use a different model.

`runtime` defaults to `codex` so existing Codex-only local configurations remain valid. Native routes are explicit:

```toml
[routes.claude]
runtime = "claude_code"
model_provider = "anthropic"
model = "CLAUDE_MODEL_NAME"
input_usd_per_million = 0
output_usd_per_million = 0

[routes.gemini]
runtime = "antigravity"
model_provider = "gemini"
model = "GEMINI_MODEL_NAME"
input_usd_per_million = 0
output_usd_per_million = 0
```

Claude Code routes require `model_provider = "anthropic"`. Antigravity routes require `model_provider = "gemini"` and use only `GEMINI_API_KEY`; Vertex or other Google credential modes are not accepted. Unknown runtime names and mismatched providers are configuration errors.

Do not set `runtime_version` in `.scriptorium/config.toml`. It is written only by run freezing. At run start, Scriptorium freezes the resolved revision, both configuration layers, role-to-route mapping, runtime name and exact SDK version per route, model/provider, prompts and schemas with digests, source manifest and file digests, and budget/pricing snapshot.

The pinned versions are:

- `codex`: `openai-codex==0.144.4` (base installation)
- `claude_code`: `claude-agent-sdk==0.2.128` (`scriptorium[claude]`)
- `antigravity`: `google-antigravity==0.1.8` (`scriptorium[antigravity]`)

Changing an installed SDK version does not rewrite a frozen run. Resume requires the frozen runtime, exact SDK version, provider, and model. A historical frozen configuration without a route-level runtime continues to use its original top-level Codex runtime without modifying the stored configuration.

## Runtime authentication

Secrets belong in native user configuration or environment variables, never in `scriptorium.toml`, `.scriptorium/config.toml`, SQLite, or the manuscript bundle.

- Codex endpoints and credentials remain in Codex user configuration. OpenAI-compatible and laboratory gateways can still be configured as Codex providers.
- Claude Code uses the official SDK's bundled Claude harness. Authentication may come from its native login or API configuration; a live smoke test is the authoritative authentication check.
- Antigravity reads `GEMINI_API_KEY` only.

Run:

```bash
scriptorium doctor
```

before starting work. Doctor resolves the selected profile plus its visual-transcription, revision, and verification routes, then checks only the runtimes those routes actually reference. Each required SDK must be installed at the exact pinned version. Doctor also checks `GEMINI_API_KEY` for Antigravity; Claude Code authentication is exercised only by the explicitly enabled live smoke test.

Doctor rejects unresolved model placeholders. When a dollar budget is requested, missing route prices are also a configuration error. An explicitly configured zero price is accepted for an OSS or laboratory-gateway route; Scriptorium does not infer billing from the provider name.

## Budget semantics

Token usage comes from the selected native runtime. Scriptorium normalizes it before estimating cost with the local input/output prices frozen on the route:

- Claude input includes ordinary input, cache-create, and cache-read tokens; cached input is the cache-read subset.
- Antigravity output includes candidate and thought tokens; reasoning output is the thought subset.

Input and output totals are priced once. Cached input and reasoning output remain separately auditable subdivisions and are not added to those totals again. Native usage and cost details stay in the NDJSON trace.

Scriptorium checks recorded estimated cost before starting each new task. A task already in progress can make the estimate slightly exceed the requested budget, after which the run enters `waiting_budget`.

Visual transcription uses the same frozen route pricing and budget accounting as every other model task. PDFs without raster pages do not create a transcription task or incur its cost.

This is a local scheduling gate and audit estimate, not a provider-level billing cap.
