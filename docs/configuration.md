# Configuration

`scriptorium.toml` is committed with the manuscript. It names the LaTeX entrypoint, engine, and review roles. A run reads this file from the selected Git commit, never from uncommitted work.

```toml
[manuscript]
main = "main.tex"
engine = "pdflatex"

[profiles.quick]
roles = ["substantive_review", "copyedit"]

[profiles.full]
roles = ["substantive_review", "copyedit", "consistency", "figure_review"]
```

Supported engines are `pdflatex`, `xelatex`, and `lualatex`. Review roles are `substantive_review`, `copyedit`, `consistency`, and `figure_review`. Revision and verification tasks are prepared later by the workflow. `scriptorium init` writes a starter file and adds `.scriptorium/` to `.gitignore`.

The selected Codex, Claude Code, or Antigravity CLI session owns model choice, effort, authentication, and spending. Supply its actual selection when claiming a task. Scriptorium stores this as external provenance; it cannot verify provider billing. `--budget-usd`, `--route`, and `.scriptorium/config.toml` belonged to the removed internal runner. Remove the old local config before starting a new run. The CLI rejects those old options or configuration with a migration error.

`doctor` checks the frozen project configuration, Git revision, dependency closure, local LaTeX tools, and compilation. It does not call a model. New runs freeze the project config, source identities, navigation, prompts, output schemas, and evidence contract. A frozen task's `input_digest` binds its prompt, schema, and bundle.
