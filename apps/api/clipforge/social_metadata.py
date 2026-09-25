"""Platform-specific, persisted social hashtag suggestions.

This module deliberately has no trend provider.  It produces content-relevant
metadata only; a future provider can add a trend signal to each platform entry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings

Platform = Literal["tiktok", "instagram", "youtube"]
PLATFORMS: tuple[Platform, ...] = ("tiktok", "instagram", "youtube")
_SPAM = {"#fyp", "#foryou", "#viral", "#trending", "#xyzbca", "#fun", "#cool", "#video"}
_STOPWORDS = {
    "und", "der", "die", "das", "den", "dem", "ein", "eine", "einer", "ist", "sind", "mit",
    "für", "von", "wie", "warum", "was", "the", "and", "with", "why", "what", "this", "that",
}


def normalize_hashtag(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token:
        return None
    if re.search(r"\s", token):
        return None
    if not token.startswith("#"):
        token = f"#{token}"
    if len(token) < 2 or len(token) > 80 or re.search(r"\s", token):
        return None
    if not re.fullmatch(r"#[\w-]+", token, flags=re.UNICODE):
        return None
    return token


def normalize_hashtags(values: Iterable[object], *, reject_spam: bool = False) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = normalize_hashtag(value)
        if token is None or (reject_spam and token.casefold() in _SPAM):
            continue
        key = token.casefold()
        if key not in seen:
            seen.add(key)
            result.append(token)
    return result[:8]


class PlatformHashtags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    description: str = ""
    hashtags: list[str] = Field(min_length=2, max_length=8)

    @field_validator("hashtags")
    @classmethod
    def validate_hashtags(cls, value: list[str]) -> list[str]:
        normalized = normalize_hashtags(value, reject_spam=True)
        if len(normalized) < 2:
            raise ValueError("at least two valid, content-relevant hashtags are required")
        return normalized


class SocialHashtagOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiktok: PlatformHashtags
    instagram: PlatformHashtags
    youtube: PlatformHashtags


class SocialMetadataProvider(Protocol):
    def generate(self, content: dict) -> SocialHashtagOutput: ...


_INSTRUCTIONS = """Generate independent social metadata for TikTok, Instagram, and YouTube Shorts.
Base them only on the final topic, language, content type, and final narration. Each platform must be
considered independently, though useful overlap is allowed. Return a strong accurate title, a concise
platform-appropriate description, and only as many relevant hashtags as this specific platform and topic
justify (never pad to a fixed count). Prefer accurate topic/entity/concept terms.
Write every title and description in the project's language and a calm, curious, informative tone.
Emojis: a title normally carries 1-2 emojis, a description 1-3, each one tied to the actual subject
(a thing, place, or idea the video explains), placed naturally - e.g. after a sentence or at the end,
never as a chain of repeats. YouTube may stay a little more restrained than TikTok or Instagram.
No clickbait: no alarm or hype emojis (such as sirens or exclamation marks), no repeated emojis, no
all-caps shouting, no "you won't believe"-style phrases, no invented claims. Hashtags stay plain text
without emojis.
Do not use #fyp, #foryou, #viral, #trending, #xyzbca, generic spam, trend claims, scores, descriptions,
or whitespace in a tag. Return only the requested structured data."""


class OpenAISocialMetadataProvider:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = OpenAI(api_key=settings.openai_api_key)

    def generate(self, content: dict) -> SocialHashtagOutput:
        try:
            response = self._client.responses.parse(
                model=self._settings.openai_director_model,
                instructions=_INSTRUCTIONS,
                input=json.dumps(content, ensure_ascii=False),
                text_format=SocialHashtagOutput,
                store=False,
            )
        except OpenAIError as exc:
            raise SocialMetadataError(str(exc)) from exc
        parsed = response.output_parsed
        if not isinstance(parsed, SocialHashtagOutput):
            raise SocialMetadataError("No structured hashtag output was returned.")
        return parsed


class SocialMetadataError(RuntimeError):
    pass


def _terms(content: dict) -> list[str]:
    source = f"{content.get('topic', '')} {content.get('script', '')}"
    words = re.findall(r"[\wÀ-ÖØ-öø-ÿ-]{3,}", source, flags=re.UNICODE)
    unique: list[str] = []
    seen: set[str] = set()
    for word in words:
        key = word.casefold()
        if key in _STOPWORDS or key in seen:
            continue
        seen.add(key)
        unique.append(word[:40])
    return unique[:5] or ["Wissen"]


# ---------------------------------------------------------------------------
# Emoji policy: a few relevant emojis, never a clickbait chain
# ---------------------------------------------------------------------------

TITLE_EMOJI_LIMIT = 2
DESCRIPTION_EMOJI_LIMIT = 3
# How many emojis are *added* when a text arrives without one.  YouTube stays
# a little more restrained than TikTok and Instagram.
_ADDED_TITLE_EMOJIS: dict[Platform, int] = {"tiktok": 2, "instagram": 2, "youtube": 1}
_ADDED_DESCRIPTION_EMOJIS: dict[Platform, int] = {"tiktok": 2, "instagram": 3, "youtube": 2}

_EMOJI_CHAR = (
    "\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF"
    "\u203C\u2049\u3030\u303D\u3297\u3299"
)
_EMOJI = re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2}"
    f"|[{_EMOJI_CHAR}]\uFE0F?[\U0001F3FB-\U0001F3FF]?"
    f"(?:\u200D[{_EMOJI_CHAR}]\uFE0F?[\U0001F3FB-\U0001F3FF]?)*)"
)
# Alarm emojis only ever add urgency, never meaning.
_ALARM_EMOJIS = {"🚨", "‼", "⁉", "❗", "❕", "❓", "❔", "⚠"}
# Hype emojis are allowed only when the subject itself is fire, explosions, etc.
_HYPE_EMOJIS = {"🔥": "fire", "💥": "explosion", "😱": "", "🤯": "", "💯": "", "😳": "", "👀": ""}
_CLICKBAIT = re.compile(
    r"you\s+(?:won'?t|will\s+not|will\s+never)\s+believe|shocking|must[\s-]+(?:see|watch)|gone\s+wrong|"
    r"watch\s+(?:till|until|to)\s+the\s+end|wait\s+for\s+it|not\s+clickbait|"
    r"(?:du|ihr)\s+(?:wirst|werdet)\s+(?:es\s+)?nicht\s+glauben|schockierend|unfassbar|"
    r"bis\s+zum\s+ende\s+(?:schauen|gucken)|kein\s+clickbait",
    re.IGNORECASE,
)

# General concept lexicon (German/English stems).  A stem prefixed with "="
# matches a whole word only; other stems match a word prefix, or anywhere
# inside a long compound word ("Meerwasser").  This is a vocabulary of common
# subjects, not a list of video topics.
_CONCEPTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("rainbow", "🌈", ("regenbogen", "rainbow")),
    ("space", "🚀", ("weltall", "weltraum", "=space", "=universum", "=universe", "galax", "astronaut", "rakete", "rocket", "=stern", "=sterne", "=star", "=stars")),
    ("planet", "🪐", ("planet", "=mars", "jupiter", "saturn")),
    ("moon", "🌙", ("=mond", "=moon")),
    ("sun", "☀️", ("=sonne", "=sun", "sonnenlicht", "sunlight", "solar")),
    ("volcano", "🌋", ("vulkan", "volcano", "erdbeben", "earthquake")),
    ("earth", "🌍", ("=erde", "=earth", "planet erde", "klima", "climate")),
    ("ocean", "🌊", ("=meer", "meere", "ocean", "ozean", "welle", "=wave", "=waves", "=sea", "=seas")),
    ("island", "🏝️", ("insel", "island")),
    ("rain", "🌧️", ("=regen", "=rain", "wetter", "weather", "gewitter", "sturm", "storm", "wolke", "cloud")),
    ("lightning", "⚡", ("blitz", "lightning", "=strom", "electric", "elektr")),
    ("cold", "❄️", ("=eis", "=ice", "=kalt", "=kälte", "=cold", "schnee", "=snow", "frost", "gletscher", "glacier")),
    ("heat", "🌡️", ("hitze", "=heat", "temperatur", "temperature", "fieber", "fever", "=heiß", "=hot")),
    ("fire", "🔥", ("feuer", "=fire", "flamme", "flame", "=brennt", "=burn")),
    ("explosion", "💥", ("explosion", "explod")),
    ("water", "💧", ("wasser", "water", "=nass", "=nasse", "=nassen", "=wet", "feucht", "tropfen", "=drop", "=drops")),
    ("light", "✨", ("=licht", "=light", "leucht", "=glow", "glüh", "funkel")),
    ("brain", "🧠", ("gehirn", "=hirn", "brain", "=nerv", "nerven", "=nerve", "=nerves", "neuro", "gedächtnis", "memory")),
    ("hand", "🖐️", ("=hand", "=hände", "=händen", "=hands", "finger")),
    ("eye", "👁️", ("=auge", "=augen", "=eye", "=eyes")),
    ("ear", "👂", ("=ohr", "=ohren", "=ear", "=ears", "=hören", "=hearing")),
    ("tooth", "🦷", ("=zahn", "=zähne", "=tooth", "=teeth")),
    ("heart", "❤️", ("=herz", "herzen", "=heart", "herzschlag", "heartbeat")),
    ("blood", "🩸", ("=blut", "=blood")),
    ("bone", "🦴", ("knochen", "=bone", "=bones", "skelett", "skeleton")),
    ("muscle", "💪", ("muskel", "muscle", "=sport", "fitness", "training")),
    ("sleep", "😴", ("schlaf", "=sleep", "träum", "=dream", "=dreams")),
    ("dna", "🧬", ("=dna", "=dns", "genetic", "genetisch", "erbgut", "=zelle", "=zellen", "=cell", "=cells")),
    ("germ", "🦠", ("bakteri", "bacteri", "=virus", "=viren", "=keim", "=keime", "=germ", "=germs")),
    ("dog", "🐶", ("=hund", "=hunde", "=dog", "=dogs")),
    ("cat", "🐱", ("=katze", "=katzen", "=cat", "=cats")),
    ("bird", "🐦", ("vogel", "vögel", "=bird", "=birds")),
    ("fish", "🐟", ("=fisch", "=fische", "=fish")),
    ("shark", "🦈", ("=hai", "=haie", "shark")),
    ("bee", "🐝", ("biene", "=bee", "=bees")),
    ("octopus", "🐙", ("krake", "oktopus", "octopus")),
    ("insect", "🐞", ("insekt", "insect", "käfer", "beetle", "=ameise", "=ameisen", "=ant", "=ants", "firefl")),
    ("animal", "🐾", ("=tier", "=tiere", "=tieren", "animal")),
    ("plant", "🌱", ("pflanz", "=plant", "=plants", "blume", "flower", "=blatt", "blätter", "=leaf", "=leaves")),
    ("tree", "🌳", ("=baum", "bäume", "=tree", "=trees", "=wald", "wälder", "forest")),
    ("coffee", "☕", ("kaffee", "coffee", "koffein", "caffeine")),
    ("chocolate", "🍫", ("schokolad", "chocolate")),
    ("food", "🍎", ("=essen", "=food", "nahrung", "ernährung", "nutrition", "=obst", "=fruit", "gemüse", "vegetable", "lebensmittel")),
    ("money", "💰", ("=geld", "=money", "wirtschaft", "econom", "=euro", "dollar", "=kosten", "=cost", "=costs")),
    ("history", "🏛️", ("geschicht", "history", "historisch", "historic", "=antike", "ancient", "römer", "=romans", "mittelalter", "medieval", "pyramid")),
    ("car", "🚗", ("=auto", "=autos", "=car", "=cars", "fahrzeug", "vehicle")),
    ("plane", "✈️", ("flugzeug", "airplane", "=plane", "=planes", "=flug", "flight")),
    ("robot", "🤖", ("=ki", "=ai", "künstliche intelligenz", "artificial", "roboter", "robot", "algorithm")),
    ("computer", "💻", ("computer", "internet", "smartphone", "=handy", "software")),
    ("time", "⏰", ("=zeit", "=time", "=uhr", "=clock")),
    ("music", "🎵", ("musik", "music", "=sound", "=sounds", "schall")),
    ("color", "🎨", ("=farbe", "=farben", "=color", "=colors", "=colour", "=colours")),
    ("language", "🗣️", ("sprache", "sprachen", "language", "=wort", "=wörter", "=word", "=words")),
    ("science", "🔬", ("wissenschaft", "science", "forsch", "research", "experiment", "=labor", "chemie", "chemistry", "physik", "physics", "molekül", "molecule", "=atom", "=atome", "=atoms")),
)


def emoji_clusters(text: str) -> list[str]:
    """Every emoji (with its modifiers / ZWJ sequence) in ``text``."""
    return _EMOJI.findall(text or "")


def count_emojis(text: str) -> int:
    return len(emoji_clusters(text))


def _bare(emoji: str) -> str:
    return re.sub("[\uFE0F\U0001F3FB-\U0001F3FF]", "", emoji)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    return text.strip()


def _concepts(text: str) -> list[tuple[str, str]]:
    """Subject concepts in order of first appearance: [(concept, emoji), ...]."""
    lowered = (text or "").casefold()
    words = [(match.start(), match.group()) for match in re.finditer(r"[^\W\d_]+", lowered)]
    found: dict[str, tuple[int, str]] = {}
    for name, emoji, stems in _CONCEPTS:
        for stem in stems:
            if " " in stem:
                position = lowered.find(stem)
                hit = position if position >= 0 else None
            elif stem.startswith("="):
                hit = next((start for start, word in words if word == stem[1:]), None)
            else:
                hit = next(
                    (start for start, word in words if word.startswith(stem) or (len(stem) >= 5 and len(word) > len(stem) + 3 and stem in word)),
                    None,
                )
            if hit is not None and (name not in found or hit < found[name][0]):
                found[name] = (hit, emoji)
    ordered = sorted(found.items(), key=lambda item: item[1][0])
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, (_position, emoji) in ordered:
        if emoji not in seen:
            seen.add(emoji)
            result.append((name, emoji))
    return result


def _is_clickbait(text: str) -> bool:
    if _CLICKBAIT.search(text):
        return True
    shouted = [word for word in re.findall(r"[^\W\d_]{4,}", text) if word.isupper()]
    return len(shouted) >= 3


def _keep_emoji(emoji: str, subject: set[str]) -> bool:
    bare = _bare(emoji)
    if bare in _ALARM_EMOJIS:
        return False
    if bare in _HYPE_EMOJIS:
        concept = _HYPE_EMOJIS[bare]
        return bool(concept) and concept in subject
    return True


def _limit_emojis(text: str, limit: int, subject: set[str]) -> str:
    """Drop alarm/hype emojis, repeats and anything over ``limit``, in place."""
    kept: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        emoji = match.group()
        key = _bare(emoji)
        if key in kept or len(kept) >= limit or not _keep_emoji(emoji, subject):
            return " "
        kept.add(key)
        return emoji

    text = re.sub(r"!{2,}", "!", re.sub(r"\?[?!]+", "?", text))
    return _tidy(_EMOJI.sub(replace, text))


def _suggested_emojis(text: str, subject: list[tuple[str, str]], count: int, subject_names: set[str]) -> list[str]:
    """Topic emojis for a text: its own concepts first, then the project's."""
    chosen: list[str] = []
    for _name, emoji in [*_concepts(text), *subject]:
        if emoji not in chosen and _keep_emoji(emoji, subject_names):
            chosen.append(emoji)
    chosen = chosen[:count]
    if len(chosen) < count:
        fallback = "🤔" if text.rstrip().endswith("?") else "💡"
        if fallback not in chosen:
            chosen.append(fallback)
    return chosen


def _with_title_emojis(title: str, emojis: list[str]) -> str:
    return f"{title} {''.join(emojis)}".strip() if title else title


def _with_description_emojis(description: str, emojis: list[str]) -> str:
    if not description or not emojis:
        return description
    first_sentence = re.search(r"[.!?](?=\s+\S)", description)
    if first_sentence and len(emojis) > 1:
        head, tail = description[: first_sentence.end()], description[first_sentence.end():].strip()
        rest = "".join(emojis[1:])
        return f"{head} {emojis[0]} {tail} {rest}".strip()
    return f"{description} {''.join(emojis)}".strip()


def apply_emoji_policy(output: SocialHashtagOutput, content: dict, *, fallback: SocialHashtagOutput | None = None) -> SocialHashtagOutput:
    """Titles get 1-2 and descriptions 1-3 subject emojis; spam never survives.

    Emojis the text already carries stay where they were placed unless they
    are alarm/hype emojis, repeats or over the limit.  A text without any gets
    emojis that match the project's own subject.  Clickbait copy is replaced
    by the neutral local copy.  Hashtags never carry emojis (see
    :func:`normalize_hashtag`).
    """
    subject = _concepts(f"{content.get('topic', '')} {content.get('script', '')}")
    subject_names = {name for name, _emoji in subject}
    topic_subject = _concepts(str(content.get("topic") or "")) or subject
    platforms: dict[str, PlatformHashtags] = {}
    for platform in PLATFORMS:
        entry = getattr(output, platform)
        backup = getattr(fallback, platform) if fallback else None
        title, description = entry.title.strip(), entry.description.strip()
        if backup and _is_clickbait(_EMOJI.sub(" ", title)):
            title = backup.title
        if backup and _is_clickbait(_EMOJI.sub(" ", description)):
            description = backup.description
        title = _limit_emojis(title, TITLE_EMOJI_LIMIT, subject_names)
        description = _limit_emojis(description, DESCRIPTION_EMOJI_LIMIT, subject_names)
        if title and not count_emojis(title):
            title = _with_title_emojis(title, _suggested_emojis(title, topic_subject, _ADDED_TITLE_EMOJIS[platform], subject_names))
        if description and not count_emojis(description):
            description = _with_description_emojis(description, _suggested_emojis(description, subject, _ADDED_DESCRIPTION_EMOJIS[platform], subject_names))
        platforms[platform] = PlatformHashtags(title=title, description=description, hashtags=list(entry.hashtags))
    return SocialHashtagOutput(**platforms)


def _lead_sentence(script: str, limit: int = 160) -> str:
    sentence = re.split(r"(?<=[.!?])\s+", script.strip(), maxsplit=1)[0].strip() if script.strip() else ""
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return sentence


def _local_output(content: dict) -> SocialHashtagOutput:
    terms = [f"#{term}" for term in _terms(content)]
    language = str(content.get("language") or "en").casefold()
    german = language.startswith("de")
    # These are platform-specific editorial contexts, not trend assertions.
    suffixes = ("#KurzErklärt", "#Wissen") if german else ("#QuickLearn", "#Learn")
    instagram = ("#WissensSnack", "#Entdecken") if german else ("#LearnSomething", "#Discover")
    youtube = ("#WissensShorts", "#Shorts") if german else ("#EducationalShorts", "#Shorts")
    question = str(content.get("topic") or ("Wissensvideo" if german else "Explainer")).strip()
    topic = question.rstrip("?!. ")
    lead = _lead_sentence(str(content.get("script") or ""))
    asks = question.endswith("?")
    if german:
        titles = (question, f"{topic} – kurz erklärt", f"{topic} | Wissens-Short")
        leads = lead or f"Die kurze Antwort auf: {topic}."
        descriptions = (
            f"{leads} Die Erklärung in wenigen Sekunden.",
            f"{leads} Speichere dir die Erklärung für später.",
            f"{question if asks else f'Was steckt hinter {topic}?'} {lead or 'Hier ist die kompakte Erklärung.'}",
        )
    else:
        titles = (question, f"{topic} – explained", f"{topic} | Short")
        leads = lead or f"The short answer to: {topic}."
        descriptions = (
            f"{leads} Explained in seconds.",
            f"{leads} Save it for later.",
            f"{question if asks else f'What is behind {topic}?'} {lead or 'Here is the quick explanation.'}",
        )
    return SocialHashtagOutput(
        tiktok=PlatformHashtags(title=titles[0], description=descriptions[0], hashtags=normalize_hashtags([*terms[:3], *suffixes], reject_spam=True)),
        instagram=PlatformHashtags(title=titles[1], description=descriptions[1], hashtags=normalize_hashtags([*terms[:2], *instagram], reject_spam=True)),
        youtube=PlatformHashtags(title=titles[2], description=descriptions[2], hashtags=normalize_hashtags([*terms[:2], *youtube], reject_spam=True)),
    )


def content_from_state(state: dict) -> dict:
    intent = state.get("intent") or {}
    script = state.get("script") or {}
    return {
        "topic": str(intent.get("topic") or ""),
        "script": str(script.get("text") or ""),
        "language": str(intent.get("language") or "en"),
        "content_type": str(intent.get("content_type") or "short video"),
    }


def generate_social_metadata(state: dict, settings: Settings, *, provider: SocialMetadataProvider | None = None) -> dict:
    content = content_from_state(state)
    try:
        output = provider.generate(content) if provider else (
            OpenAISocialMetadataProvider(settings).generate(content)
            if settings.clipforge_ai_mode == "openai" and settings.openai_api_key
            else None
        )
        local = _local_output(content)
        output = apply_emoji_policy(output or local, content, fallback=local)
        return {
            "status": "available",
            "source": "generated",
            "platforms": {
                platform: {
                    "title": getattr(output, platform).title,
                    "description": getattr(output, platform).description,
                    "hashtags": list(getattr(output, platform).hashtags),
                    "manual": False,
                }
                for platform in PLATFORMS
            },
        }
    except (SocialMetadataError, ValueError, TypeError) as exc:
        return {"status": "unavailable", "source": "generated", "error": str(exc), "platforms": {}}
