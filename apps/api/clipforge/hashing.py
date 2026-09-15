import hashlib
import json
from typing import Any


def content_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def attach_hashes(state: dict[str, Any]) -> dict[str, Any]:
    components = (
        "research",
        "facts",
        "script",
        "voice",
        "storyboard",
        "assets",
        "scenes",
        "captions",
        "music",
        "timeline",
        "render",
        "qc",
    )
    state["content_hashes"] = {
        component: content_hash(state.get(component)) for component in components
    }
    return state
