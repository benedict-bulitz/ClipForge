"""Run the bounded live hook-playbook validation without creating a project."""

from __future__ import annotations

from clipforge.config import resolve_settings
from clipforge.pipeline import build_initial_state
from clipforge.schemas import AdvancedOptions

TOPICS = (
    "Warum tränen unsere Augen beim Zwiebelschneiden?",
    "Wieso kriegen wir Gänsehaut?",
)


def main() -> None:
    settings = resolve_settings()
    if settings.clipforge_ai_mode != "openai":
        settings = settings.model_copy(update={"clipforge_ai_mode": "openai"})

    for topic in TOPICS:
        state = build_initial_state(
            topic,
            AdvancedOptions(language="de", research="on", music_enabled=False),
            settings,
        )
        print(f"TOPIC: {topic}")
        script = state["script"]
        selected_hook = str(script.get("selected_hook") or "unavailable")
        selected_strategy = str(script.get("selected_hook_strategy") or "unavailable")
        body_blocks = [
            str(block.get("text") or "")
            for block in script.get("blocks", [])
            if block.get("role") != "hook"
        ]
        print(f"SELECTED STRATEGY: {selected_strategy}")
        print(f"SELECTED HOOK: {selected_hook}")
        print(f"FINAL FIRST SENTENCE: {script.get('text', '').split('. ', 1)[0]}")
        print(f"FIRST BODY SENTENCE: {body_blocks[0] if body_blocks else 'unavailable'}")


if __name__ == "__main__":
    main()
