"""Contract for POST /runs/{run_id}/feedback."""

from pydantic import BaseModel, ConfigDict, Field


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    satisfactory: bool
    reason: str = Field(min_length=1, max_length=2000)


class FeedbackAccepted(BaseModel):
    feedback_id: int
    run_id: str
