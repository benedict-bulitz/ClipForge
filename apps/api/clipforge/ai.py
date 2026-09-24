import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

from .config import Settings
from .hook_library import generation_playbook
from .narration import clean_research_claim
from .schemas import AdvancedOptions

DIRECTOR_INSTRUCTIONS = (
    "You are ClipForge's short-form director. Write the shortest complete explanation that answers "
    "the user's question well. Identify a compact story_arc over the research evidence (1-based fact_index): "
    "give each fact a role (primary_answer, essential_context, evidence, comparison, supporting_fact, "
    "explanation, secondary_insight, ranked_item), what it depends_on, and whether it may_appear_in_hook; name "
    "primary_answer_index (the fact that actually answers the question) and final_payoff_index (the last "
    "meaningful beat, which may differ from the answer). A related but different insight is secondary_insight, "
    "never the answer. State the concrete curiosity_gap the viewer has. "
    "Identify a compact payoff_plan with the central curiosity, actual payoff, "
    "supporting information, desired viewer reaction, and whether the hook must withhold the payoff. "
    "Start with one very short curiosity hook or setup that makes sense to a viewer who never saw the "
    "user's prompt. Reveal the answer in the next sentence when immediate disclosure is needed for clarity; "
    "when payoff_plan requires a protected payoff, let useful supporting information earn it instead. Never "
    "use a fixed number of seconds to delay a reveal, add filler, or append a generic outro after the payoff. "
    "The supplied novelty_plan is conservative guidance derived only from the provided research evidence. "
    "Use it to prioritize useful explanatory or comparative value, never to invent or exaggerate novelty. "
    "The supplied hook_playbook is the canonical ClipForge hook manifest. Use its strategy definitions, "
    "when-to-use and avoid guidance, quality rules, and evidence opportunities to write three to five "
    "original topic-specific hook candidates. Return their matching manifest strategy IDs in hook_candidates "
    "and identify the winner in selected_hook_strategy. Do not invent a competing strategy catalogue or copy "
    "fallback templates. A curiosity or evidence hook is allowed only when no manifest family fits better. "
    "script_blocks must contain only the selected audience-facing hook. With usable research evidence, never "
    "repeat or lightly paraphrase the user's question: a question hook must add genuine tension, challenge, "
    "contrast, implication, or insight. For each script block, return a concise visual_intents entry describing "
    "physical objects, actions, context, visual_strategy, and up to four English provider-facing media_queries. "
    "For each media query also return, in media_query_targets at the same position, a stable target key for what "
    "it depicts: subject_a or subject_b for the two sides of a comparison (the same key for the same side in every "
    "block), shared for the concept both sides share, or context. In payoff_plan, protected_visual_target is the "
    "target key whose imagery would reveal hook_must_not_reveal, or empty when nothing is protected. "
    "A visual goal must describe what should appear on screen, never conversational uncertainty, research prose, or meta commentary. "
    "The hook must be honest, usually no more than fourteen words, and must not delay the useful answer. "
    "For every hook candidate and the selected opening, ask: would a typical 10–14 year old "
    "understand this on first listen without prior knowledge? Use everyday German when writing "
    "German: short, concrete, natural spoken wording, no unexplained jargon, abstract academic "
    "phrasing, unnecessarily clever wording, fake sensationalism, or rigid hook templates. "
    "Simplify a difficult question into clear everyday language while preserving its factual "
    "meaning; clarity takes priority over novelty or a clever hook strategy. "
    "Never begin a hook with an obscure specialist term. Explain the familiar effect first; name a necessary "
    "technical term later in plain language. "
    "Never open with 'The short answer to', 'Today we are going to', 'Have you ever wondered', "
    "'Let's take a look', or other setup about the act of answering. Assume zero prior knowledge. "
    "Use ordinary words, short sentences, and one useful idea at a time. Explain a necessary technical "
    "term immediately in plain language. Do not pad toward the maximum duration. Stop when the answer "
    "is complete. Omit low-value names, institutions, dates, dimensions, visitor counts, and repeated "
    "examples unless they directly answer the question. Research is evidence: rewrite it as clean, "
    "natural narration and never copy source formatting, HTML, Markdown, headings, ellipses, search "
    "artifacts, attribution boilerplate, or editorial directions such as adding source links before "
    "publication into speech. Internal roles such as answer, hook, context, detail, support, cause, "
    "turn, and payoff are metadata and must never appear in spoken text. Do not invent claims or sources. "
    "Write every script block in the requested language and never switch languages unless explicitly "
    "asked. Never use a number, popularity/adoption claim, or words like everyone/currently/trending unless the supplied research evidence supports it. "
    "Avoid generic controversy, personal attacks, and 'you've been lied to' style clichés. Keep narration within the maximum at roughly 165 words per minute."
)


class AIIntent(BaseModel):
    topic: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    question: str = Field(min_length=1)
    language: str = Field(min_length=2)
    content_type: str = Field(min_length=1)
    tone: str = Field(min_length=1)
    research_required: bool
    visual_style: str = Field(min_length=1)
    shortform: bool = True


class AIFact(BaseModel):
    claim: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    source_label: str | None = None
    source_url: str | None = None


class AIScriptBlock(BaseModel):
    role: str = Field(min_length=1)
    text: str = Field(min_length=1)


class AIHookCandidate(BaseModel):
    strategy: str = Field(min_length=1)
    text: str = Field(min_length=1)


class AIVisualIntent(BaseModel):
    visual_goal: str = Field(min_length=1)
    objects: list[str] = Field(default_factory=list, max_length=6)
    actions: list[str] = Field(default_factory=list, max_length=6)
    context: list[str] = Field(default_factory=list, max_length=6)
    visual_strategy: Literal["literal", "process", "physical_example", "diagram_or_card"] = "literal"
    media_queries: list[str] = Field(default_factory=list, max_length=4)
    media_query_targets: list[str] = Field(default_factory=list, max_length=4)


class AIPayoffPlan(BaseModel):
    curiosity_question: str = Field(min_length=1, max_length=320)
    payoff: str = Field(min_length=1, max_length=320)
    payoff_type: str = Field(default="answer", min_length=1, max_length=80)
    payoff_dependencies: list[str] = Field(default_factory=list, max_length=4)
    reveal_policy: Literal["after_supporting_information", "immediate_context_allowed"] = "immediate_context_allowed"
    hook_must_not_reveal: str = Field(default="", max_length=320)
    protected_visual_target: str = Field(default="", max_length=32)
    desired_viewer_reaction: str = Field(default="insight", max_length=80)
    supporting_information: list[str] = Field(default_factory=list, max_length=4)


class AIVisualHook(BaseModel):
    visual_goal: str = Field(min_length=2, max_length=220)
    subjects_to_show: list[str] = Field(default_factory=list, max_length=4)
    contrast: str = Field(default="", max_length=140)
    motion_or_change: str = Field(default="", max_length=140)
    visual_priority: str = Field(default="", max_length=140)
    must_not_show: list[str] = Field(default_factory=list, max_length=4)
    media_queries: list[str] = Field(default_factory=list, max_length=4)
    media_query_targets: list[str] = Field(default_factory=list, max_length=4)


class AIStoryUnit(BaseModel):
    fact_index: int = Field(ge=1, le=10)
    role: str = Field(min_length=1, max_length=40)
    depends_on: list[int] = Field(default_factory=list, max_length=6)
    may_appear_in_hook: bool = True


class AIStoryArc(BaseModel):
    units: list[AIStoryUnit] = Field(default_factory=list, max_length=10)
    primary_answer_index: int | None = Field(default=None, ge=1, le=10)
    final_payoff_index: int | None = Field(default=None, ge=1, le=10)
    curiosity_gap: str = Field(default="", max_length=240)


class AIProjectPlan(BaseModel):
    intent: AIIntent
    research_questions: list[str] = Field(min_length=0, max_length=6)
    facts: list[AIFact] = Field(min_length=0, max_length=10)
    answer_skeleton: list[str] = Field(min_length=2, max_length=8)
    script_blocks: list[AIScriptBlock] = Field(min_length=2, max_length=8)
    music_mood: str = Field(min_length=1)
    hook_candidates: list[AIHookCandidate] = Field(default_factory=list, max_length=5)
    selected_hook_strategy: str | None = None
    visual_intents: list[AIVisualIntent] = Field(default_factory=list, max_length=8)
    payoff_plan: AIPayoffPlan | None = None
    story_arc: AIStoryArc | None = None


class AIHookGenerationResponse(BaseModel):
    hook_candidates: list[AIHookCandidate] = Field(min_length=1, max_length=5)
    selected_hook_strategy: str = Field(min_length=1)
    visual_hook: AIVisualHook | None = None
    on_screen_text_hook: str | None = Field(default=None, max_length=80)


class AIMusicRecommendations(BaseModel):
    track_ids: list[str] = Field(default_factory=list, max_length=12)


class AIEditDirective(BaseModel):
    components: list[
        Literal[
            "voice",
            "assets",
            "script",
            "captions",
            "music",
            "research",
            "language",
            "format",
            "duration",
        ]
    ] = Field(default_factory=list)
    voice_gender: Literal["masculine", "feminine", "neutral"] | None = None
    voice_tone: Literal["deep", "warm", "energetic", "calm"] | None = None
    voice_speed: Literal["slower", "faster", "normal"] | None = None
    change_speaker: bool = False
    visual_action: Literal[
        "more_video", "real_footage", "less_text", "different", "dynamic", "relevant"
    ] | None = None
    script_action: Literal["shorter", "rewrite_intro", "stronger_hook", "clearer"] | None = None
    caption_action: Literal["larger", "smaller", "style", "reduce", "move"] | None = None
    clarification: str | None = None


@dataclass(frozen=True)
class AIPlanResult:
    plan: AIProjectPlan | None
    status: str
    error: str | None = None


@dataclass(frozen=True)
class AIHookGenerationResult:
    candidates: list[dict[str, str]]
    selected_strategy: str | None
    status: str
    error: str | None = None
    triple_hook: dict[str, Any] | None = None


@dataclass(frozen=True)
class AIMusicRecommendationResult:
    track_ids: list[str]
    status: str
    error: str | None = None


HOOK_GENERATION_INSTRUCTIONS = (
    "You are ClipForge's hook writer. The supplied body is final and must remain unchanged. "
    "Generate only original spoken hook candidates for the opening of that body. The supplied "
    "hook_playbook is the canonical strategy manifest: use its strategy definitions, when-to-use, "
    "avoid guidance, and evidence opportunities; do not invent a competing strategy catalogue or "
    "copy fallback templates. Choose only factually supportable strategies. A hook must create a "
    "real reason to continue—curiosity, viewer involvement, tension, contrast, a correction, a "
    "challenge, or a genuinely surprising insight—before the explanation. Never use a plain "
    "restatement of the body as an evidence insight, repeat or lightly paraphrase the user's "
    "question, use clickbait, or begin with unexplained specialist terminology. Keep German "
    "everyday, short, concrete, and understandable on first listen by a typical 10–14 year old. "
    "The supplied story_arc names the primary question, the curiosity gap and which facts may appear in a hook; "
    "never use a fact that may not appear in the hook, and when withhold_answer is false do not invent mystery. "
    "The supplied payoff_plan states whether a payoff is protected. Never reveal hook_must_not_reveal in "
    "a spoken hook, visual hook, text hook, visual query, or visual subject. Alongside the candidates, return "
    "one visual_hook and one very short on_screen_text_hook for the chosen strategy. The three channels must "
    "serve the same curiosity but must not repeat the same sentence. The visual hook should describe real "
    "subjects, contrast or motion for the existing media pipeline, and explicit must_not_show constraints. "
    "Give each visual_hook media query a media_query_targets key at the same position, using the target keys "
    "of the supplied plan (subject_a, subject_b, shared, context); never search the protected_visual_target. "
    "The supplied reaction_arc is guidance, never permission to exaggerate: use only reactions the supported "
    "payoff can honestly deliver, and prefer clarity over emotional intensity. The supplied format_plan "
    "is lightweight guidance for contrast, challenge, progression, or explanation; follow it without adding scenes or filler. "
    "The supplied novelty_plan is derived only from existing research and may suggest a useful angle, but never "
    "supports claims of global uniqueness or unsupported surprise. "
    "Return three to five candidates and select the strongest strategy. Return structured output only."
)

MUSIC_MATCHING_INSTRUCTIONS = (
    "You are ClipForge's music supervisor. Rank background tracks for this specific short video using "
    "its topic, script, mood, pacing, tone, and content style. Return only track_ids from the supplied "
    "candidate catalog, in best-first order. Never invent an ID, title, URL, or any other track."
)


def rank_music_with_openai(state: dict[str, Any], candidates: list[dict[str, Any]], settings: Settings) -> AIMusicRecommendationResult:
    """Ask the configured provider to order a bounded, real catalog shortlist."""
    if settings.clipforge_ai_mode != "openai":
        return AIMusicRecommendationResult([], "local_planner")
    if not settings.openai_api_key:
        return AIMusicRecommendationResult([], "missing_key", "OPENAI_API_KEY is not configured")
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else {}
    music = state.get("music") if isinstance(state.get("music"), dict) else {}
    script = state.get("script") if isinstance(state.get("script"), dict) else {}
    timeline = state.get("timeline") if isinstance(state.get("timeline"), dict) else {}
    request = {"video": {"topic": intent.get("topic"), "script": script.get("text"), "mood": music.get("mood"), "tone": intent.get("tone"), "content_style": intent.get("content_type"), "pacing": {"duration_seconds": timeline.get("duration"), "scene_count": len(state.get("scenes") or [])}}, "candidates": candidates}
    try:
        response = OpenAI(api_key=settings.openai_api_key).responses.parse(
            model=settings.openai_worker_model, instructions=MUSIC_MATCHING_INSTRUCTIONS,
            input=json.dumps(request, ensure_ascii=False), text_format=AIMusicRecommendations,
            max_output_tokens=500, store=False,
        )
        parsed = response.output_parsed
        if not isinstance(parsed, AIMusicRecommendations):
            return AIMusicRecommendationResult([], "provider_error", "No parsed music recommendations")
        return AIMusicRecommendationResult(parsed.track_ids, "connected")
    except (OpenAIError, ValueError, TypeError) as exc:
        return AIMusicRecommendationResult([], "provider_error", str(exc)[:240])


def generate_hook_candidates_with_openai(
    prompt: str,
    intent: dict[str, Any],
    facts: list[dict[str, Any]],
    body: str,
    settings: Settings,
    payoff_plan: dict[str, Any] | None = None,
    reaction_arc: dict[str, Any] | None = None,
    format_plan: dict[str, Any] | None = None,
    novelty_plan: dict[str, Any] | None = None,
    story_arc: dict[str, Any] | None = None,
) -> AIHookGenerationResult:
    """Generate manifest-guided candidates after the body is finalized."""
    if not settings.openai_api_key:
        return AIHookGenerationResult([], None, "missing_key", "OPENAI_API_KEY is not configured")
    request = {
        "prompt": prompt,
        "intent": {
            key: intent.get(key)
            for key in ("topic", "question", "language", "content_type", "tone")
        },
        "final_body": body,
        "payoff_plan": payoff_plan or {},
        "reaction_arc": reaction_arc or {},
        "format_plan": format_plan or {},
        "novelty_plan": novelty_plan or {},
        "story_arc": story_arc or {},
        "facts": [
            {"claim": clean_research_claim(str(fact.get("claim") or ""))}
            for fact in facts
            if fact.get("claim")
        ],
        "hook_playbook": generation_playbook(facts),
    }
    try:
        response = OpenAI(api_key=settings.openai_api_key).responses.parse(
            model=settings.openai_director_model,
            instructions=HOOK_GENERATION_INSTRUCTIONS,
            input=json.dumps(request, ensure_ascii=False),
            text_format=AIHookGenerationResponse,
            max_output_tokens=900,
            store=False,
        )
        parsed = response.output_parsed
        if not isinstance(parsed, AIHookGenerationResponse):
            return AIHookGenerationResult([], None, "provider_error", "No parsed hook result")
        return AIHookGenerationResult(
            [candidate.model_dump() for candidate in parsed.hook_candidates],
            parsed.selected_hook_strategy,
            "connected",
            triple_hook={
                "visual_hook": parsed.visual_hook.model_dump(mode="json") if parsed.visual_hook else None,
                "on_screen_text_hook": parsed.on_screen_text_hook,
            },
        )
    except (OpenAIError, ValueError, TypeError) as exc:
        return AIHookGenerationResult([], None, "provider_error", str(exc)[:240])


def plan_with_openai(
    prompt: str,
    options: AdvancedOptions,
    settings: Settings,
    *,
    evidence: list[str] | None = None,
    novelty_plan: dict[str, Any] | None = None,
) -> AIPlanResult:
    """Create a schema-validated semantic plan and report provider failure explicitly."""
    if settings.clipforge_ai_mode != "openai":
        return AIPlanResult(None, "local_planner")
    if not settings.openai_api_key:
        return AIPlanResult(None, "missing_key", "OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=settings.openai_api_key)
    request = {
        "prompt": prompt,
        "options": options.model_dump(mode="json", exclude_none=True),
        "research_evidence": [clean_research_claim(item) for item in (evidence or []) if item],
        "novelty_plan": novelty_plan or {},
        "hook_playbook": generation_playbook([
            {
                "claim": clean_research_claim(item),
                "verification": "source_attributed",
                "sources": [{"label": "research", "url": ""}],
            }
            for item in (evidence or [])
            if item
        ]),
    }
    try:
        response = client.responses.parse(
            model=settings.openai_director_model,
            instructions=DIRECTOR_INSTRUCTIONS,
            input=json.dumps(request, ensure_ascii=False),
            text_format=AIProjectPlan,
        )
        plan = response.output_parsed
        if not isinstance(plan, AIProjectPlan):
            return AIPlanResult(None, "provider_error", "OpenAI returned no parsed project plan")
        return AIPlanResult(plan, "connected")
    except (OpenAIError, ValueError, TypeError) as exc:
        return AIPlanResult(None, "provider_error", str(exc)[:240])


def ai_plan_to_dict(plan: AIProjectPlan) -> dict[str, Any]:
    return plan.model_dump(mode="json")


def direct_edit_with_openai(
    instruction: str, state: dict[str, Any], settings: Settings
) -> AIEditDirective | None:
    """Use the existing director for flexible language understanding when enabled."""
    if settings.clipforge_ai_mode != "openai" or not settings.openai_api_key:
        return None
    request = {
        "instruction": instruction,
        "project": {
            "language": state.get("intent", {}).get("language"),
            "tone": state.get("intent", {}).get("tone"),
            "voice_profile": state.get("voice", {}).get("profile"),
            "aspect_ratio": state.get("timeline", {}).get("aspect_ratio"),
        },
    }
    try:
        response = OpenAI(api_key=settings.openai_api_key).responses.parse(
            model=settings.openai_worker_model,
            instructions=(
                "You are ClipForge's edit director. Convert the user's natural-language request "
                "into concrete edit preferences. Infer ordinary paraphrases, including narrator "
                "gender presentation, vocal character and speed, visual footage requests, script "
                "rewrites, captions, music, language, duration and format. Select only requested "
                "components. If the desired change is genuinely unclear, return no components and "
                "ask one short, useful clarification question."
            ),
            input=json.dumps(request, ensure_ascii=False),
            text_format=AIEditDirective,
        )
        directive = response.output_parsed
        return directive if isinstance(directive, AIEditDirective) else None
    except (OpenAIError, ValueError, TypeError):
        return None


def interpret_edit(
    instruction: str, state: dict[str, Any], settings: Settings
) -> AIEditDirective:
    """Interpret an edit semantically, with a broad offline fallback for common requests."""
    directed = direct_edit_with_openai(instruction, state, settings)
    text = instruction.casefold()
    components: set[str] = set()
    values: dict[str, Any] = {}

    voice_context = bool(
        re.search(r"\b(voice|narrat(?:or|ion)|speaker|stimme|sprecher|erzähler|erzaehler)\b", text)
    )
    gender = None
    if re.search(r"\b(male|man|masculine|männlich|maennlich|maskulin)\b", text):
        gender = "masculine"
    elif re.search(r"\b(female|woman|feminine|weiblich|feminin)\b", text):
        gender = "feminine"
    tone = next(
        (
            value
            for pattern, value in (
                (r"\b(deep|deeper|lower|tief|tiefer)\b", "deep"),
                (r"\b(warm|warmer|wärmer|waermer)\b", "warm"),
                (r"\b(energetic|energy|lively|energisch|lebhaft)\b", "energetic"),
                (r"\b(calm|calmer|ruhig|ruhiger)\b", "calm"),
            )
            if re.search(pattern, text)
        ),
        None,
    )
    voice_speed = next(
        (
            value
            for pattern, value in (
                (r"\b(slower|slow down|langsamer)\b", "slower"),
                (r"\b(faster|speed up|schneller)\b", "faster"),
                (r"\b(normal speed|normales tempo)\b", "normal"),
            )
            if re.search(pattern, text)
        ),
        None,
    )
    change_speaker = bool(
        re.search(r"\b(change|switch|different|andere[nr]?|wechsel)\b.{0,24}\b(voice|speaker|narrator|stimme|sprecher|erzähler|erzaehler)\b", text)
    )
    if voice_context or gender or (tone and re.search(r"\b(sound|kling|narrat|voice|stimme)\b", text)):
        components.add("voice")
        values.update(
            voice_gender=gender,
            voice_tone=tone,
            voice_speed=voice_speed,
            change_speaker=change_speaker,
        )

    visual_action = next(
        (
            value
            for pattern, value in (
                (r"\b(more videos?|mehr videos?|mehr clips?)\b", "more_video"),
                (r"\b(real (?:video |visual )?footage|echte[srn]? (?:aufnahmen|videos?))\b", "real_footage"),
                (r"\b(less text|weniger text)\b", "less_text"),
                (r"\b(different (?:clips?|visuals?|shots?)|andere (?:clips?|bilder|szenen))\b", "different"),
                (r"\b(dynamic|dynamischer|mehr bewegung|more motion)\b", "dynamic"),
                (r"\b(relevant (?:footage|visuals?|clips?)|passende (?:bilder|clips?|videos?))\b", "relevant"),
            )
            if re.search(pattern, text)
        ),
        None,
    )
    if visual_action or re.search(r"\b(change|improve|update|ändere|aendere)\b.{0,20}\b(visuals?|footage|clips?|bilder|szenen)\b", text):
        components.add("assets")
        values["visual_action"] = visual_action or "different"

    script_action = next(
        (
            value
            for pattern, value in (
                (r"\b(shorter|kürzer|kuerzer|condense|tighten)\b", "shorter"),
                (r"\b(rewrite|change|redo|schreib.{0,8}neu|ändere|aendere)\b.{0,18}\b(intro|opening|einleitung)\b", "rewrite_intro"),
                (
                    r"(?:\b(stronger|better|stärker|staerker)\b.{0,15}\b(hook|opening|einstieg)\b|"
                    + r"\b(hook|opening|einstieg)\b.{0,15}\b(stronger|better|stärker|staerker)\b)",
                    "stronger_hook",
                ),
                (r"\b(clearer|more clearly|clarify|verständlicher|klarer|einfacher erklären|einfacher erklaeren)\b", "clearer"),
            )
            if re.search(pattern, text)
        ),
        None,
    )
    script_context = bool(re.search(r"\b(script|intro|hook|opening|text|skript|einleitung|einstieg)\b", text))
    short_video = script_action == "shorter" and bool(
        re.search(r"\b(video|short|clip|film)\b", text)
    )
    if script_action and (
        script_context
        or short_video
        or script_action in {"stronger_hook", "rewrite_intro", "clearer"}
    ):
        components.add("script")
        values["script_action"] = script_action

    caption_context = bool(re.search(r"\b(captions?|subtitles?|untertitel)\b", text))
    if caption_context:
        components.add("captions")
        values["caption_action"] = next(
            (
                value
                for pattern, value in (
                    (r"\b(larger|bigger|größer|grösser|groesser)\b", "larger"),
                    (r"\b(smaller|kleiner)\b", "smaller"),
                    (r"\b(style|design|look|stil)\b", "style"),
                    (r"\b(reduce|fewer|less|weniger|reduzier)\b", "reduce"),
                    (r"\b(move|position|higher|lower|verschieb|oben|unten)\b", "move"),
                )
                if re.search(pattern, text)
            ),
            "style" if re.search(r"\b(change|different|ändere|aendere)\b", text) else None,
        )

    if re.search(r"\b(music|soundtrack|musik)\b", text):
        components.add("music")
    if re.search(r"\b(translate|language|deutsch|german|englisch|english|sprache|übersetz|uebersetz)\b", text):
        components.add("language")
    if re.search(r"\b(9:16|16:9|1:1|portrait|landscape|square|format|seitenverhältnis)\b", text):
        components.add("format")
    if re.search(r"\b(duration|seconds?|sekunden?|länge|laenge)\b", text):
        components.add("duration")
    if re.search(r"\b(research|fact|facts|recherch|fakt|quelle|source)\b", text):
        components.add("research")

    if not components:
        if directed and (directed.components or directed.clarification):
            return directed
        return AIEditDirective(
            clarification=(
                "What would you like to change: the narration voice, wording, footage, captions, "
                "music, duration, language, or video format?"
            )
        )
    fallback = AIEditDirective(components=sorted(components), **values)
    if not directed:
        return fallback
    merged = fallback.model_dump()
    merged["components"] = sorted(set(fallback.components) | set(directed.components))
    for field in (
        "voice_gender",
        "voice_tone",
        "voice_speed",
        "visual_action",
        "script_action",
        "caption_action",
    ):
        if getattr(directed, field) is not None:
            merged[field] = getattr(directed, field)
    merged["change_speaker"] = fallback.change_speaker or directed.change_speaker
    merged["clarification"] = None
    return AIEditDirective.model_validate(merged)
