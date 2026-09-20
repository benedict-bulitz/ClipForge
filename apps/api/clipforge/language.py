import re
from typing import Literal

LanguageCode = Literal["en", "de"]
LanguageChoice = Literal["auto", "en", "de"]

GERMAN_MARKERS = {
    "aber", "auf", "aus", "gebaut", "der", "die", "das", "dein", "eine", "einer",
    "erkläre", "für", "hat", "ich", "ist", "kann", "mit", "nicht", "oder", "planet",
    "schwerer", "sich", "sind", "und", "unser", "unsere", "warum", "wenn", "wird",
    "wieso", "wie", "wir", "zur", "über",
}
ENGLISH_MARKERS = {
    "about", "and", "are", "built", "can", "does", "explain", "for", "how", "is",
    "not", "our", "planet", "the", "this", "what", "when", "why", "with", "would",
}


def normalize_language(value: str | None) -> LanguageChoice:
    if not value:
        return "auto"
    normalized = value.casefold().strip()
    if normalized in {"de", "deutsch", "german"}:
        return "de"
    if normalized in {"en", "englisch", "english"}:
        return "en"
    return "auto"


def infer_language(text: str) -> LanguageCode:
    words = re.findall(r"[a-zäöüß]+", text.casefold())
    german = sum(word in GERMAN_MARKERS for word in words)
    english = sum(word in ENGLISH_MARKERS for word in words)
    german += sum(2 for character in "äöüß" if character in text.casefold())
    german += sum(word.endswith(("ung", "keit", "lich", "isch")) for word in words)
    return "de" if german > english else "en"


def resolve_language(prompt: str, choice: str | None) -> LanguageCode:
    normalized = normalize_language(choice)
    return infer_language(prompt) if normalized == "auto" else normalized


def detect_text_language(text: str) -> LanguageCode | Literal["unknown"]:
    words = re.findall(r"[a-zäöüß]+", text.casefold())
    if len(words) < 3:
        return "unknown"
    german = sum(word in GERMAN_MARKERS for word in words)
    english = sum(word in ENGLISH_MARKERS for word in words)
    if german == english == 0:
        return "unknown"
    return "de" if german > english else "en"
