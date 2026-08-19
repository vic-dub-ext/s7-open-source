"""Return type shared by every stage's `run()` entrypoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

StageStatus = Literal["done", "failed", "partial", "skipped"]


class StageResult(BaseModel):
    stage: str
    status: StageStatus
    counts: dict[str, int] = {}
    error: str | None = None
