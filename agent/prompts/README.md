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
