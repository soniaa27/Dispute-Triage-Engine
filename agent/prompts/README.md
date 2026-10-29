# Prompts

Prompts are versioned artifacts (requirement L2). Each version is one YAML file in this
folder, e.g. `v1.yaml`, `v2.yaml`. Once a version has been used in an evaluation run it is
never edited; create a new version instead.

Suggested shape:

```yaml
version: v1
description: Baseline classifier + liability prompt
agents:
  classifier:
    system: |
      ...
  liability:
    system: |
      ...
```

The version string is recorded in `RunResult.prompt_version` for every run.
The current fake agent reports `prompt_version: fake-0`.

Load prompts with `agent.prompts`: `agent_prompts("investigator")` returns that agent's
templates from `CURRENT_PROMPT_VERSION` (`v1`), and `load_prompts("v1")` returns the whole
file. Templates use `string.Template` placeholders (`$facts`), so JSON braces need no
escaping. A file's `version:` must equal its file name; a test checks this.

| Version | Agents | Notes |
|---------|--------|-------|
| `v1` | investigator | Baseline tool loop: search / answer JSON, delay given as a fact (D-020) |

The Investigator's prompts are `v<n>.yaml`. The Verifier's are `verifier_v<n>.yaml`
(declaring `version: verifier_v<n>`), versioned separately. A graph run records both, e.g.
`inv-v1+ver-v1`.
