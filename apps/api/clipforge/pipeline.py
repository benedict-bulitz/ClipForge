import copy
import re
from datetime import UTC, datetime
from typing import Any

from .ai import ai_plan_to_dict, plan_with_openai
from .config import Settings
from .dependencies import resolve_edit_scope
from .hashing import attach_hashes
from .research import research_topic
from .schemas import AdvancedOptions

STAGE_LABELS = [
    ("intent", "Understanding your idea"),
    ("research", "Researching"),
    ("script", "Writing story"),
    ("assets", "Creating visuals"),
    ("voice", "Generating voice"),
    ("captions", "Building captions"),
    ("render", "Editing video"),
    ("qc", "Reviewing video"),
]


class UnsupportedEdit(ValueError):
    pass


def _language(prompt: str, override: str | None) -> str:
    if override:
        return {
            "german": "de",
            "deutsch": "de",
            "de": "de",
            "english": "en",
            "englisch": "en",
            "en": "en",
        }.get(override.casefold(), override.casefold())
    words = set(re.findall(r"[a-zäöüß]+", prompt.casefold()))
    german = {"der", "die", "das", "warum", "erkläre", "erstelle", "mache", "wäre", "über"}
    english = {"why", "what", "how", "explain", "create", "make", "about"}
    return "de" if len(words & german) > len(words & english) else "en"


def _intent(prompt: str, options: AdvancedOptions) -> dict[str, Any]:
    text = prompt.casefold()
    words = set(re.findall(r"[a-zäöüß]+", text))
    explicit_story = bool(words & {"story", "geschichte", "fiction"}) or any(
        term in text for term in ("erzähl mir", "erzaehl mir", "write a story", "fictional")
    )
    narrative_scene = any(term in text for term in ("wakes alone", "wacht allein", "wacht auf"))
    fictional = explicit_story or narrative_scene
    hypothetical = any(term in text for term in ("what if", "was wäre wenn", "was waere wenn"))
    current = any(term in words for term in ("heute", "aktuell", "latest", "today", "news", "tomorrow"))
    language = _language(prompt, options.language)
    topic = re.sub(r"[?!.,]", "", prompt).strip()
    if len(topic) > 76:
        topic = topic[:73].rstrip() + "…"
    content_type = (
        "fictional_story"
        if fictional
        else ("hypothetical_explainer" if hypothetical else ("current_explainer" if current else "factual_explainer"))
    )
    return {
        "topic": topic,
        "intent": "tell_story" if fictional else "explain",
        "question": prompt,
        "language": language,
        "content_type": content_type,
        "tone": options.style or ("cinematic" if fictional else "fast_documentary"),
        "research_required": not fictional,
        "visual_style": "cinematic_story" if fictional else "documentary_graphics",
        "shortform": True,
    }


def _fiction_plan(prompt: str, intent: dict[str, Any]) -> dict[str, Any]:
    de = intent["language"] == "de"
    subject = intent["topic"]
    if de:
        blocks = [
            ("hook", f"Als {subject}, stimmt etwas an der Welt plötzlich nicht mehr."),
            ("setup", "Ein einziges Signal widerspricht allem, worauf die Hauptfigur vertraut hat."),
            ("turn", "Die Spur führt nicht nach draußen, sondern zu einer Entscheidung aus der eigenen Vergangenheit."),
            ("payoff", "Jetzt bleiben nur Sekunden, um zu entscheiden, welche Wahrheit überleben darf."),
        ]
    else:
        blocks = [
            ("hook", f"When {subject}, one detail makes the whole world feel wrong."),
            ("setup", "A single signal contradicts everything the main character trusted."),
            ("turn", "The trail leads inward, toward a choice made in their own past."),
            ("payoff", "Only seconds remain to decide which truth gets to survive."),
        ]
    return {
        "intent": intent,
        "research_questions": [],
        "facts": [],
        "answer_skeleton": [role.upper() for role, _ in blocks],
        "script_blocks": [{"role": role, "text": text} for role, text in blocks],
        "music_mood": "cinematic_suspense",
    }


def _factual_blocks(intent: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    de = intent["language"] == "de"
    if not facts:
        message = (
            "Die Recherche war nicht erreichbar. Öffne Build readiness und verbinde einen Recherche-Anbieter."
            if de
            else "Research was unavailable. Open Build readiness and connect a research provider."
        )
        return [{"role": "status", "text": message}, {"role": "next_step", "text": intent["question"]}]
    claims = [fact["claim"] for fact in facts[:4]]
    hook = (
        f"Die kurze Antwort auf „{intent['topic']}“ beginnt hier:"
        if de
        else f"The short answer to “{intent['topic']}” starts here:"
    )
    roles = ["context", "cause", "turn", "payoff"]
    blocks = [{"role": "hook", "text": hook}]
    blocks.extend(
        {"role": roles[index], "text": claim}
        for index, claim in enumerate(claims)
    )
    return blocks


def _words(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def _fit_blocks(blocks: list[dict[str, str]], max_duration: int, wpm: int = 155) -> list[dict[str, str]]:
    max_words = max(12, int(max_duration * wpm / 60))
    fitted = copy.deepcopy(blocks)
    while len(fitted) > 2 and sum(len(_words(block["text"])) for block in fitted) > max_words:
        removable = next(
            (index for index in range(len(fitted) - 2, 0, -1) if fitted[index]["role"] not in {"hook", "payoff"}),
            None,
        )
        if removable is None:
            break
        fitted.pop(removable)
    total = sum(len(_words(block["text"])) for block in fitted)
    while total > max_words:
        index = max(range(len(fitted)), key=lambda item: len(_words(fitted[item]["text"])))
        words = _words(fitted[index]["text"])
        if len(words) <= 4:
            break
        fitted[index]["text"] = " ".join(words[:-1]).rstrip(",:;") + "…"
        total -= 1
    return fitted


def _dimensions(aspect_ratio: str) -> tuple[int, int]:
    return {"1:1": (1080, 1080), "16:9": (1920, 1080)}.get(aspect_ratio, (1080, 1920))


def _build_scenes(
    blocks: list[dict[str, str]], total_duration: float, old_scenes: list[dict] | None = None
) -> list[dict[str, Any]]:
    total_words = max(1, sum(len(_words(block["text"])) for block in blocks))
    old_by_block = {scene.get("block_id"): scene for scene in old_scenes or []}
    cursor = 0.0
    scenes: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        share = len(_words(block["text"])) / total_words
        end = total_duration if index == len(blocks) - 1 else round(cursor + total_duration * share, 2)
        existing = old_by_block.get(block["id"], {})
        scenes.append(
            {
                "id": existing.get("id", f"scene_{block['id'].removeprefix('voice_block_')}"),
                "block_id": block["id"],
                "start": round(cursor, 2),
                "end": end,
                "narration": block["text"],
                "visual_goal": existing.get(
                    "visual_goal", f"Illustrate {block['role']}: {block['text'][:88]}"
                ),
                "preferred_media": existing.get("preferred_media", "generated_card"),
                "fallback_media": "generated_card",
                "search_queries": _words(block["text"])[:5],
                "motion": existing.get("motion", "slow_push" if index % 2 == 0 else "subtle_pan"),
                "asset_status": existing.get("asset_status", "generated_card_ready"),
            }
        )
        cursor = end
    return scenes


def _build_captions(script: str, duration: float) -> list[dict[str, Any]]:
    words = _words(script)
    seconds_per_word = duration / max(1, len(words))
    return [
        {
            "text": " ".join(words[index : index + 4]),
            "start": round(index * seconds_per_word, 2),
            "end": round(min(len(words), index + 4) * seconds_per_word, 2),
        }
        for index in range(0, len(words), 4)
    ]


def _normalise_blocks(blocks: list[dict[str, Any]], max_duration: int) -> list[dict[str, str]]:
    clean = [
        {"role": str(block["role"]).strip(), "text": str(block["text"]).strip()}
        for block in blocks
        if str(block.get("role", "")).strip() and str(block.get("text", "")).strip()
    ]
    fitted = _fit_blocks(clean, max_duration)
    for index, block in enumerate(fitted, 1):
        block["id"] = f"voice_block_{index:02d}"
    return fitted


def _refresh_script_derivatives(
    state: dict[str, Any], *, old_scenes: list[dict] | None = None
) -> None:
    blocks = state["script"]["blocks"]
    script_text = " ".join(block["text"] for block in blocks)
    word_count = len(_words(script_text))
    wpm = int(state["duration"].get("speaking_rate_wpm", 155))
    max_duration = int(state["duration"]["max_seconds"])
    duration = round(min(max_duration, max(4, word_count / wpm * 60)), 2)
    state["script"]["text"] = script_text
    state["script"]["word_count"] = word_count
    state["duration"]["estimated_seconds"] = duration
    state["duration"]["actual_seconds"] = None
    state["scenes"] = _build_scenes(blocks, duration, old_scenes)
    state["storyboard"] = {"status": "ready", "scene_count": len(state["scenes"])}
    state["voice"]["blocks"] = [{"id": block["id"], "status": "awaiting_tts"} for block in blocks]
    state["voice"]["status"] = "regeneration_required"
    state["captions"]["items"] = _build_captions(script_text, duration)
    state["captions"]["timing"] = "estimated"
    state["timeline"]["duration"] = duration
    state["timeline"]["timing"] = "estimated"
    state["timeline"]["scene_ids"] = [scene["id"] for scene in state["scenes"]]


def build_initial_state(
    prompt: str, options: AdvancedOptions, settings: Settings
) -> dict[str, Any]:
    intent = _intent(prompt, options)
    ai_result = plan_with_openai(prompt, options, settings)
    if ai_result.plan:
        plan = ai_plan_to_dict(ai_result.plan)
        plan["intent"]["language"] = intent["language"]
    elif intent["content_type"] == "fictional_story":
        plan = _fiction_plan(prompt, intent)
    else:
        plan = {
            "intent": intent,
            "research_questions": [
                f"What is the direct answer to: {prompt}",
                "Which facts are essential and attributable?",
            ],
            "facts": [],
            "answer_skeleton": ["HOOK", "CONTEXT", "PAYOFF"],
            "script_blocks": [],
            "music_mood": "documentary_pulse",
        }

    sources: list[dict] = []
    research_status = "skipped"
    research_provider = "not_needed"
    research_error = None
    if intent["research_required"]:
        result = research_topic(prompt, intent["language"], settings)
        research_status = result.status
        research_provider = result.provider
        research_error = result.error
        sources = result.sources
        if result.facts:
            plan["facts"] = result.facts
            plan["script_blocks"] = _factual_blocks(intent, result.facts)

    facts = plan.get("facts", [])
    for index, fact in enumerate(facts, 1):
        fact["id"] = f"fact_{index:02d}"
        fact["priority"] = fact.get("priority") or (
            "MUST_KNOW" if fact.get("importance", 0) >= 0.7 else "USEFUL"
        )
        if "sources" not in fact:
            label = fact.pop("source_label", None)
            url = fact.pop("source_url", None)
            fact["sources"] = [{"label": label, "url": url}] if label and url else []
        fact["verification"] = (
            fact.get("verification")
            or ("source_attributed" if fact["sources"] else "unverified_model_synthesis")
        )

    max_duration = options.max_duration or settings.shortform_max_duration
    raw_blocks = plan.get("script_blocks") or _factual_blocks(intent, facts)
    blocks = _normalise_blocks(raw_blocks, max_duration)
    script_text = " ".join(block["text"] for block in blocks)
    word_count = len(_words(script_text))
    wpm = 155
    estimated_duration = round(min(max_duration, max(4, word_count / wpm * 60)), 2)
    scenes = _build_scenes(blocks, estimated_duration)
    width, height = _dimensions(options.aspect_ratio)
    factual_ready = not intent["research_required"] or bool(facts and sources)
    now = datetime.now(UTC).isoformat()
    state: dict[str, Any] = {
        "version": 1,
        "created_at": now,
        "prompt": prompt,
        "mode": "auto",
        "options": options.model_dump(mode="json"),
        "intent": intent,
        "research": {
            "required": intent["research_required"],
            "questions": plan.get("research_questions", []),
            "status": research_status,
            "provider": research_provider,
            "error": research_error,
            "sources": sources,
        },
        "facts": facts,
        "information_plan": {
            "must_know": [fact["id"] for fact in facts if fact["priority"] == "MUST_KNOW"],
            "answer_skeleton": [block["role"].upper() for block in blocks],
        },
        "script": {
            "text": script_text,
            "word_count": word_count,
            "blocks": blocks,
            "fact_map": [
                {"block_id": block["id"], "fact_id": facts[min(index, len(facts) - 1)]["id"]}
                for index, block in enumerate(blocks[1:])
                if facts
            ],
        },
        "duration": {
            "mode": "AUTO",
            "estimated_seconds": estimated_duration,
            "actual_seconds": None,
            "max_seconds": max_duration,
            "speaking_rate_wpm": wpm,
        },
        "voice": {
            "provider": "openai_or_system",
            "profile": options.voice or "warm_documentary",
            "blocks": [{"id": block["id"], "status": "awaiting_tts"} for block in blocks],
            "status": "awaiting_tts",
        },
        "storyboard": {"status": "ready", "scene_count": len(scenes)},
        "assets": {"status": "generated_cards_ready", "license_manifest": []},
        "scenes": scenes,
        "captions": {
            "style": options.caption_style or "bold_clean",
            "font_size": 72,
            "highlight_color": "#ff6838",
            "items": _build_captions(script_text, estimated_duration),
            "timing": "estimated",
        },
        "music": {"mood": options.music or plan["music_mood"], "energy": 0.65, "status": "planned"},
        "timeline": {
            "duration": estimated_duration,
            "timing": "estimated",
            "width": width,
            "height": height,
            "aspect_ratio": options.aspect_ratio,
            "fps": 30,
            "cut_pace": "balanced",
            "scene_ids": [scene["id"] for scene in scenes],
        },
        "render": {"status": "ready_to_render" if factual_ready else "blocked_by_research", "url": None},
        "qc": {"status": "not_started", "round": 0, "max_rounds": 2, "issues": []},
        "integrations": {
            "director": ai_result.status,
            "research": research_provider if factual_ready else "unavailable",
            "media": "pexels_connected" if settings.pexels_api_key else "generated_cards",
            "voice": "openai_ready" if settings.openai_api_key else "system_voice",
            "render": "bundled_ffmpeg",
            "quality_review": "local_checks",
        },
        "provider_errors": {
            key: value
            for key, value in {"director": ai_result.error, "research": research_error}.items()
            if value
        },
        "pipeline": [
            {
                "id": stage,
                "label": label,
                "status": (
                    "complete"
                    if stage in {"intent", "assets", "captions"}
                    or (stage == "research" and factual_ready)
                    or (stage == "script" and factual_ready)
                    else ("blocked" if stage in {"research", "script"} and not factual_ready else "planned")
                ),
            }
            for stage, label in STAGE_LABELS
        ],
        "edit_history": [],
    }
    return attach_hashes(state)


def _requested_language(text: str) -> str | None:
    if any(term in text for term in ("auf deutsch", "to german", "in german", "übersetz", "uebersetz")):
        return "de"
    if any(term in text for term in ("auf englisch", "to english", "in english")):
        return "en"
    return None


def _timestamp_seconds(text: str) -> int | None:
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    match = re.search(r"\b(?:sekunde|second|at)\s+(\d{1,3})\b", text)
    return int(match.group(1)) if match else None


def apply_edit(
    previous: dict[str, Any], instruction: str, settings: Settings
) -> tuple[dict[str, Any], list[str]]:
    text = instruction.casefold()
    requested_language = _requested_language(text)
    state = copy.deepcopy(previous)
    state["version"] = int(previous.get("version", 1)) + 1
    changed = resolve_edit_scope(instruction)
    applied: list[str] = []

    if requested_language and requested_language != state["intent"]["language"]:
        options = AdvancedOptions.model_validate(state.get("options", {}))
        options.language = requested_language
        translated = build_initial_state(state["prompt"], options, settings)
        translated["version"] = state["version"]
        translated["created_at"] = state["created_at"]
        translated["edit_history"] = copy.deepcopy(state.get("edit_history", []))
        state = translated
        applied.append("language")

    if any(word in text for word in ("untertitel", "caption", "text ")):
        captions = state["captions"]
        if any(word in text for word in ("größer", "grösser", "groesser", "larger", "bigger")):
            captions["font_size"] = min(112, int(captions.get("font_size", 72)) + 10)
            applied.append("caption size")
        if any(word in text for word in ("kleiner", "smaller")):
            captions["font_size"] = max(36, int(captions.get("font_size", 72)) - 10)
            applied.append("caption size")
        colors = {
            "gelb": "#ffd166",
            "yellow": "#ffd166",
            "orange": "#ff6838",
            "weiss": "#ffffff",
            "white": "#ffffff",
        }
        for keyword, color in colors.items():
            if keyword in text:
                captions["highlight_color"] = color
                applied.append("caption color")
                break

    if any(word in text for word in ("musik", "music", "soundtrack")):
        moods = {
            "ruh": "subtle_editorial",
            "calm": "subtle_editorial",
            "dram": "dramatic_pulse",
            "spann": "cinematic_suspense",
            "upbeat": "upbeat_motion",
        }
        state["music"]["mood"] = next(
            (mood for marker, mood in moods.items() if marker in text), "fresh_selection"
        )
        state["music"]["status"] = "reselection_required"
        applied.append("soundtrack")

    if any(word in text for word in ("stimme", "voice", "sprecher", "speaker")):
        state["voice"]["profile"] = (
            "female_serious"
            if any(word in text for word in ("weib", "female"))
            else ("energetic" if "energet" in text else "serious_documentary")
        )
        state["voice"]["status"] = "regeneration_required"
        applied.append("voice")

    blocks = state["script"]["blocks"]
    old_scenes = copy.deepcopy(state["scenes"])
    script_changed = bool(requested_language and requested_language != previous["intent"]["language"])

    if any(word in text for word in ("kürzer", "kuerzer", "shorter", "verkürz", "verkuerz")):
        if len(blocks) > 2:
            blocks.pop(-2)
        state["duration"]["max_seconds"] = max(
            10, min(int(state["duration"]["max_seconds"]), round(previous["duration"]["estimated_seconds"] * 0.8))
        )
        blocks[:] = _normalise_blocks(blocks, state["duration"]["max_seconds"])
        script_changed = True
        applied.append("shorter script")

    if any(word in text for word in ("entferne", "remove", "lösche", "loesche")):
        if len(blocks) <= 2:
            raise UnsupportedEdit("The script has no removable middle sentence.")
        target_match = re.search(r"(?:about|über|ueber|zu)\s+([\wäöüß -]+)", text)
        target_words = set(_words(target_match.group(1))) if target_match else set()
        candidates = range(1, len(blocks) - 1)
        remove_index = max(
            candidates,
            key=lambda index: len(target_words & set(_words(blocks[index]["text"].casefold()))),
        )
        blocks.pop(remove_index)
        blocks[:] = _normalise_blocks(blocks, state["duration"]["max_seconds"])
        script_changed = True
        applied.append("removed sentence")

    facts_protected = any(
        phrase in text
        for phrase in ("don't change facts", "do not change facts", "fakten nicht ändern", "fakten nicht aendern")
    )
    if any(word in text for word in ("dramatisch", "dramatic", "spannender")):
        de = state["intent"]["language"] == "de"
        blocks[0]["text"] = (
            "Ein einziger Moment veränderte alles — und fast niemand erkannte ihn rechtzeitig."
            if de
            else "One moment changed everything — and almost nobody recognized it in time."
        )
        state["intent"]["tone"] = "dramatic_documentary"
        script_changed = True
        applied.append("dramatic opening")

    wants_more_facts = not facts_protected and any(
        word in text
        for word in ("füge", "fuege", "ergänze", "ergaenze", "add ", "recherch")
    )
    if wants_more_facts:
        result = research_topic(f"{state['prompt']} {instruction}", state["intent"]["language"], settings)
        existing_claims = {fact["claim"] for fact in state["facts"]}
        new_facts = [fact for fact in result.facts if fact["claim"] not in existing_claims]
        if not new_facts:
            raise UnsupportedEdit("No attributable new fact was found for that request.")
        next_fact = new_facts[0]
        next_fact["id"] = f"fact_{len(state['facts']) + 1:02d}"
        state["facts"].append(next_fact)
        state["research"]["sources"] = list(
            {source["url"]: source for source in state["research"]["sources"] + result.sources}.values()
        )
        state["research"]["status"] = result.status
        blocks.insert(-1, {"id": "", "role": "detail", "text": next_fact["claim"]})
        blocks[:] = _normalise_blocks(blocks, state["duration"]["max_seconds"])
        script_changed = True
        applied.append("researched fact")

    if script_changed:
        _refresh_script_derivatives(state, old_scenes=old_scenes)

    if any(word in text for word in ("clip", "visual", "bild", "szene", "scene", "schnitt")):
        timestamp = _timestamp_seconds(text)
        scene = state["scenes"][0]
        if timestamp is not None:
            containing = [
                item for item in state["scenes"] if item["start"] <= timestamp <= item["end"]
            ]
            scene = containing[0] if containing else min(
                state["scenes"],
                key=lambda item: min(abs(item["start"] - timestamp), abs(item["end"] - timestamp)),
            )
        if any(word in text for word in ("schneller", "faster", "fast cuts", "schnelle schnitte")):
            state["timeline"]["cut_pace"] = "fast"
            for item in state["scenes"]:
                item["motion"] = "fast_cut"
            applied.append("cut pace")
        else:
            scene["asset_status"] = "replacement_required"
            scene["edit_instruction"] = instruction
            applied.append(f"scene {scene['id']}")

    aspect = next((ratio for ratio in ("9:16", "1:1", "16:9") if ratio in instruction), None)
    if aspect and aspect != state["timeline"].get("aspect_ratio"):
        width, height = _dimensions(aspect)
        state["timeline"].update({"width": width, "height": height, "aspect_ratio": aspect})
        state.setdefault("options", {})["aspect_ratio"] = aspect
        applied.append("format")

    if not applied:
        raise UnsupportedEdit(
            "I could not turn that request into a concrete edit. Mention script, captions, voice, music, visuals, duration, language, or format."
        )

    for component in changed:
        if component == "render":
            state["render"] = {"status": "regeneration_required", "url": None}
        elif component == "qc":
            state["qc"].update({"status": "not_started", "issues": []})
    state["edit_history"].append(
        {
            "revision": state["version"],
            "instruction": instruction,
            "changed_components": changed,
            "summary": ", ".join(applied),
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    return attach_hashes(state), changed
