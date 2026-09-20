from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressEvent:
    stage: str
    label: str
    phase: str = "update"
    completed_units: int | None = None
    total_units: int | None = None
    cached: bool = False


ProgressCallback = Callable[[ProgressEvent], None]


def report_progress(
    callback: ProgressCallback | None,
    stage: str,
    label: str,
    *,
    phase: str = "update",
    completed_units: int | None = None,
    total_units: int | None = None,
    cached: bool = False,
) -> None:
    if callback is not None:
        callback(
            ProgressEvent(
                stage=stage,
                label=label,
                phase=phase,
                completed_units=completed_units,
                total_units=total_units,
                cached=cached,
            )
        )
