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

Stable role keys are `substantive_review`, `copyedit`, `consistency`, `figure_review`, `revision`, and `verification`. `workflow` identifies deterministic Armarius activity and is not a model role. Classical role names are display labels only; configuration, storage, APIs, and logs use the stable keys.

## Local routing

Keep route selection and pricing in the Git-ignored `.scriptorium/config.toml`:

```toml
max_concurrency = 2

[roles]
substantive_review = "primary"
copyedit = "primary"
consistency = "primary"
figure_review = "primary"
revision = "primary"
verification = "primary"

[routes.primary]
model_provider = "openai"
model = "USER_CONFIGURED_MODEL"
input_usd_per_million = 0
output_usd_per_million = 0
reasoning_effort = "high"
```

Replace every model placeholder before a paid run. Scriptorium does not choose a concrete model or silently fall back to another route. Use `run retry --route ROUTE` when an operator explicitly wants a different route after failure.
Additional named routes can be added when figure review or verification should use a different model.

At run start, Scriptorium freezes the resolved revision, both configuration layers, role-to-route mapping, model/provider/runtime versions, prompts and schemas with digests, source manifest and file digests, and budget/pricing snapshot.

## Provider configuration

Provider endpoint, authentication, and secrets belong in Codex user configuration or environment variables. Do not copy them into `scriptorium.toml`, `.scriptorium/config.toml`, or SQLite.

OpenAI and providers exposing a Responses/OpenAI-compatible API run through the Codex provider mechanism. Ollama, LM Studio, and a compatible laboratory gateway use the same runtime contract when configured as Codex providers. Native Anthropic and Gemini APIs are not supported directly; expose them through an external compatible gateway if required.

Scriptorium does not depend on native `openai`, `anthropic`, or Google model SDKs and does not include LiteLLM. A laboratory may deploy LiteLLM or another gateway externally, but Scriptorium sees only the configured Codex provider.

Run:

```bash
scriptorium doctor
```

before starting work. Doctor rejects unresolved model placeholders. When a dollar budget is requested, missing route prices are also a configuration error. An explicitly configured zero price is accepted for an OSS or laboratory-gateway route; Scriptorium does not infer billing from the provider name.

## Budget semantics

Token usage comes from Codex results. Scriptorium estimates cost using the route prices frozen at run start and checks recorded cost before starting each new task. A task already in progress can make the estimate slightly exceed the requested budget, after which the run enters `waiting_budget`.
The Codex input and output totals are priced once. Cached input and reasoning output remain separately auditable subdivisions and are not added to those totals a second time.

This is a local scheduling gate and audit estimate, not a provider-level billing cap.
