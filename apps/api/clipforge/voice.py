from typing import Any, Literal

VoiceId = Literal[
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "onyx",
    "nova",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
]
VoicePresentation = Literal["neutral", "masculine", "feminine"]
VoiceTone = Literal[
    "warm", "calm", "energetic", "deep", "documentary", "conversational"
]

OPENAI_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "onyx",
    "nova",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
}


def initial_voice(
    preference: str | None = None,
    *,
    voice_id: VoiceId | None = None,
    presentation: VoicePresentation | None = None,
    tone: VoiceTone | None = None,
    speed: float | None = None,
) -> dict[str, Any]:
    """Return provider-neutral voice preferences with a supported OpenAI default."""
    label = (preference or "Warm documentary").replace("_", " ").strip()
    normalized = label.casefold()
    resolved_presentation = presentation or (
        "masculine" if any(term in normalized for term in ("male", "masculine")) else "neutral"
    )
    resolved_tone = tone or next(
        (value for marker, value in (("deep", "deep"), ("energetic", "energetic"), ("calm", "calm"), ("serious", "calm")) if marker in normalized),
        "warm",
    )
    resolved_voice_id = voice_id or (
        "onyx"
        if resolved_tone == "deep"
        else ("cedar" if resolved_presentation == "masculine" else "marin")
    )
    return {
        "profile": label,
        "voice_id": resolved_voice_id,
        "gender_presentation": resolved_presentation,
        "tone": resolved_tone,
        "speed": speed if speed is not None else 1.0,
    }


def apply_voice_preferences(
    voice: dict[str, Any],
    *,
    gender: str | None = None,
    tone: str | None = None,
    speed: str | None = None,
    change_speaker: bool = False,
) -> None:
    """Apply natural voice preferences without exposing provider IDs to the UI."""
    current_voice = str(voice.get("voice_id") or "marin")
    if current_voice not in OPENAI_VOICES:
        current_voice = "marin"

    if gender == "masculine":
        voice["gender_presentation"] = "masculine"
        current_voice = "cedar"
    elif gender == "feminine":
        voice["gender_presentation"] = "feminine"
        current_voice = "coral"
    elif gender == "neutral":
        voice["gender_presentation"] = "neutral"

    if tone == "deep":
        voice["tone"] = "deep"
        current_voice = "onyx"
        if gender is None:
            voice["gender_presentation"] = "masculine"
    elif tone in {"warm", "energetic", "calm", "documentary", "conversational"}:
        voice["tone"] = tone

    if change_speaker and gender is None and tone is None:
        alternatives = ["cedar", "coral", "sage", "marin"]
        current_voice = alternatives[(alternatives.index(current_voice) + 1) % len(alternatives)] if current_voice in alternatives else "cedar"

    current_speed = float(voice.get("speed") or 1.0)
    if speed == "slower":
        current_speed = max(0.7, round(current_speed - 0.12, 2))
    elif speed == "faster":
        current_speed = min(1.4, round(current_speed + 0.12, 2))
    elif speed == "normal":
        current_speed = 1.0

    voice["voice_id"] = current_voice
    voice["speed"] = current_speed
    descriptors = []
    presentation = voice.get("gender_presentation")
    if presentation in {"masculine", "feminine"}:
        descriptors.append(str(presentation).capitalize())
    current_tone = voice.get("tone")
    if current_tone:
        descriptors.append(str(current_tone))
    if current_speed < 0.95:
        descriptors.append("slower")
    elif current_speed > 1.05:
        descriptors.append("faster")
    voice["profile"] = " · ".join(descriptors) or "Natural documentary"
    voice["status"] = "regeneration_required"


def tts_instructions(voice: dict[str, Any], language: str) -> str:
    presentation = voice.get("gender_presentation", "neutral")
    tone = voice.get("tone", "natural")
    guidance = {
        "deep": "Use a lower register with steady, grounded delivery.",
        "warm": "Sound warm, approachable, and confident.",
        "energetic": "Use lively energy and crisp emphasis without rushing.",
        "calm": "Use an even, calm, reassuring delivery.",
        "documentary": "Use a precise, credible documentary narration style.",
        "conversational": "Sound natural and conversational, as if speaking to one person.",
    }.get(str(tone), "Use a natural documentary delivery.")
    presentation_text = {
        "masculine": "Use a masculine vocal presentation.",
        "feminine": "Use a feminine vocal presentation.",
    }.get(str(presentation), "")
    language_guidance = (
        "Speak in natural contemporary German."
        if str(language).casefold().startswith("de")
        else "Speak in natural conversational English."
    )
    delivery = (
        "Talk like an engaged creator explaining something interesting to one person. "
        "Vary rhythm and intonation, add small natural pauses at punctuation and idea boundaries, "
        "and emphasize key words subtly. Do not sound like a newsreader, advertisement, or synthetic monotone."
    )
    return f"Natural {language} narration. {language_guidance} {presentation_text} {guidance} {delivery}".strip()
