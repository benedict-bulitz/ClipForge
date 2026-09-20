"""Explicit local OpenCLIP preparation and smoke check (never run on import)."""
from __future__ import annotations

import math

from PIL import Image

from clipforge.visual_verifier import OpenClipVisualVerifier, visual_intent_text


def main() -> None:
    verifier = OpenClipVisualVerifier()
    print(verifier.prepare_model())
    image = Image.new("RGB", (64, 64), (30, 120, 220))
    score = verifier.score_image(image, ["a blue square", "a construction site"], asset_identity="smoke-blue-square")
    if not math.isfinite(score):
        raise RuntimeError("OpenCLIP returned a non-finite score")
    print({"score": score, "finite": True, "prompts": visual_intent_text({"visual_intent": {"visual_goal": "blue square", "objects": ["square"]}})})


if __name__ == "__main__":
    main()
