DEPENDENCIES: dict[str, set[str]] = {
    "research": {"facts"},
    "facts": {"script"},
    "script": {"voice", "storyboard"},
    "voice": {"alignment"},
    "alignment": {"captions", "timeline"},
    "storyboard": {"assets"},
    "assets": {"crops"},
    "crops": {"timeline"},
    "captions": {"timeline"},
    "attention": {"render"},
    "music": {"timeline"},
    "timeline": {"render"},
    "render": {"qc"},
}


def expand_dependencies(changed: set[str]) -> list[str]:
    resolved = set(changed)
    frontier = list(changed)
    while frontier:
        current = frontier.pop()
        for dependent in DEPENDENCIES.get(current, set()):
            if dependent not in resolved:
                resolved.add(dependent)
                frontier.append(dependent)
    order = [
        "research",
        "facts",
        "script",
        "voice",
        "alignment",
        "storyboard",
        "assets",
        "crops",
        "captions",
        "attention",
        "music",
        "timeline",
        "render",
        "qc",
    ]
    return [component for component in order if component in resolved]


def resolve_edit_scope(instruction: str, components: set[str] | None = None) -> list[str]:
    text = instruction.casefold()
    changed: set[str] = set(components or ())
    if any(word in text for word in ("untertitel", "caption", "text kleiner", "text größer", "text grösser", "text groesser")):
        changed.add("captions")
    if any(word in text for word in ("musik", "music", "soundtrack")):
        changed.add("music")
    if any(word in text for word in ("stimme", "voice", "sprecher", "speaker")):
        changed.add("voice")
    attention_request = any(word in text for word in ("attention", "visual activity", "visual callout", "aufmerksamkeit", "visuale aktivität", "callouts"))
    if attention_request:
        changed.add("attention")
    if not attention_request and any(word in text for word in ("clip", "visual", "bild", "szene", "scene", "schnitt")):
        changed.add("assets")
    facts_are_protected = any(
        phrase in text
        for phrase in ("don't change facts", "do not change facts", "fakten nicht ändern", "fakten nicht aendern")
    )
    if not facts_are_protected and any(
        word in text for word in ("füge", "fuege", "ergänze", "ergaenze", "add ", "recherch", "fakt")
    ):
        changed.add("research")
    if any(
        word in text
        for word in (
            "kürzer",
            "kuerzer",
            "shorter",
            "entferne",
            "remove",
            "dramatisch",
            "dramatic",
            "spannender",
            "deutsch",
            "english",
            "englisch",
            "translate",
            "übersetz",
            "uebersetz",
        )
    ):
        changed.add("script")
    return expand_dependencies(changed)
