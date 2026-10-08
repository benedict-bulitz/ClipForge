from __future__ import annotations

"""YouTube's creator content type in one canonical form.

The YouTube Analytics ``creatorContentType`` dimension is documented with
upper-snake values (``SHORTS``, ``VIDEO_ON_DEMAND``, ``LIVE_STREAM``) but the
API answers in camel case (``shorts``, ``videoOnDemand``, ``liveStream``).
ClipForge stores and compares one representation, upper snake case, so the
writer and every reader cannot diverge.  Readers normalise too, so rows stored
before this canonical form (lower/camel case) need no migration.

The type is only ever YouTube's own answer: nothing here classifies a video
as a Short because ClipForge rendered it vertically.
"""

import re

SHORTS = "SHORTS"
UNSPECIFIED = "UNSPECIFIED"
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[\s\-]+")


def normalize(value: object) -> str | None:
    """``shorts``/``Shorts``/``SHORTS`` -> ``SHORTS``; ``videoOnDemand`` -> ``VIDEO_ON_DEMAND``.

    Empty and ``UNSPECIFIED`` (any casing) mean "not classified": ``None``.
    """
    if not isinstance(value, str):
        return None
    text = _SEPARATORS.sub("_", _CAMEL_BOUNDARY.sub("_", value.strip())).upper()
    return text if text and text != UNSPECIFIED else None


def is_short(value: object) -> bool:
    """Whether YouTube confirmed the video as a Short (whatever casing was stored)."""
    return normalize(value) == SHORTS
