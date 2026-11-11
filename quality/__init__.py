"""Quality over time (requirement L6): the eval's LLM judge, run on a sample of live runs.

The offline evaluation (eval/) scores 24 fixed scenarios with known answers. It cannot see
drift on real traffic: a new prompt version, a model change on the provider side, or
disputes unlike the scenarios. This package scores a sample of finished live runs with the
same judge (eval.metrics.LLMJudge, the same prompts and metric definitions) and stores the
scores in quality_scores; GET /quality aggregates them and web/quality.html charts them.

    python -m quality run      # loop forever (the `quality` service in docker compose)
    python -m quality once     # one scoring pass, then exit
    python -m quality report   # print the scores per day
"""

from quality.scorer import QualitySettings, in_sample, score_once

__all__ = ["QualitySettings", "in_sample", "score_once"]
