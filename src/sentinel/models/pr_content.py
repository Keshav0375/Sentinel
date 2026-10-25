"""PR content models — the revert PR's title and description (backend §3.4)."""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["TITLE_MAX_CHARS", "PRContent"]

# §3.4: the title is "under 72 characters".
TITLE_MAX_CHARS = 71


class PRContent(BaseModel):
    """The ``POST /generate/pr-content`` response body (§3.4), field for field.

    Text only. GitHub Actions opens the revert PR with it and a human merges or closes
    that PR; nothing here is executed.
    """

    title: str = Field(max_length=TITLE_MAX_CHARS)
    description: str
    model_used: str = Field(description="The provider/model that produced the text.")
    tokens_used: int = Field(ge=0, description="Input plus output tokens of that run.")
