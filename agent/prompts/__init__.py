"""Versioned prompts (requirement L2): agent/prompts/<version>.yaml, see README.md.

A version that has been used in an evaluation run is never edited; add a new file.
"""

from functools import cache
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).resolve().parent
CURRENT_PROMPT_VERSION = "v1"


@cache
def load_prompts(version: str = CURRENT_PROMPT_VERSION) -> dict:
    """The parsed YAML for a version: {"version", "description", "agents": {...}}."""
    path = PROMPTS_DIR / f"{version}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no prompt version {version!r} in {PROMPTS_DIR}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("version") != version:
        raise ValueError(f"{path.name} declares version {data.get('version')!r}")
    return data


def agent_prompts(agent: str, version: str = CURRENT_PROMPT_VERSION) -> dict[str, str]:
    """One agent's templates, e.g. agent_prompts("investigator")["system"]."""
    agents = load_prompts(version).get("agents", {})
    if agent not in agents:
        raise KeyError(f"prompt version {version!r} has no agent {agent!r}")
    return agents[agent]
