from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .editor_tools import TOOL_MAP, EditorToolbox, ToolResult
from .models import Project, ProjectChatMessage
from .services import effective_revision_state, get_project, serialize_project


class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str | None = Field(default=None, max_length=6_000)
    clarification: str | None = Field(default=None, max_length=1_000)
    tool_calls: list[AgentToolCall] = Field(default_factory=list, max_length=4)


class EditorProvider(Protocol):
    name: str

    def decide(
        self,
        *,
        message: str,
        project_context: dict[str, Any],
        history: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tier: Literal["fast", "strong"],
    ) -> AgentDecision: ...


class EditorProviderUnavailable(RuntimeError):
    pass


class OpenAIEditorProvider:
    name = "openai"

    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise EditorProviderUnavailable("OpenAI is not configured.")
        self._client = OpenAI(api_key=settings.openai_api_key)
        self._fast_model = settings.editor_agent_fast_model or settings.openai_worker_model
        self._strong_model = settings.editor_agent_strong_model or settings.openai_escalation_model

    def decide(
        self,
        *,
        message: str,
        project_context: dict[str, Any],
        history: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tier: Literal["fast", "strong"],
    ) -> AgentDecision:
        payload = {
            "user_message": message,
            "project": project_context,
            "recent_conversation": history,
            "available_tools": tools,
        }
        try:
            response = self._client.responses.parse(
                model=self._strong_model if tier == "strong" else self._fast_model,
                instructions=(
                    "You are ClipForge's project-aware Editor Agent. Decide whether to answer, "
                    "clarify, or use the supplied project tools. Use read tools for project facts "
                    "and action tools for changes. Resolve pronouns from recent conversation. "
                    "An explicit edit imperative outranks topical words inside quoted target text. "
                    "Treat quoted text in delete/remove requests as the exact edit target; never "
                    "answer such a request with sources merely because the quote mentions sources. "
                    "For mixed requests, call every needed read/action tool in order. Never claim "
                    "an action succeeded in answer; the application will report verified tool "
                    "results. Never request or reveal credentials. Only use listed tools, never "
                    "filesystem, shell, arbitrary URLs, or hidden reasoning. Keep answers concise."
                ),
                input=json.dumps(payload, ensure_ascii=False),
                text_format=AgentDecision,
                max_output_tokens=1_200,
                store=False,
            )
        except (OpenAIError, ValueError, TypeError) as exc:
            raise EditorProviderUnavailable("The configured editor model is unavailable.") from exc
        decision = response.output_parsed
        if not isinstance(decision, AgentDecision):
            raise EditorProviderUnavailable("The editor model returned no usable decision.")
        return decision


class OpenAICompatibleEditorProvider:
    """Ollama-compatible provider using the same typed decision contract."""

    name = "ollama"

    def __init__(self, settings: Settings):
        self._client = OpenAI(base_url=settings.editor_agent_ollama_base_url, api_key="ollama")
        self._model = settings.editor_agent_ollama_model

    def decide(
        self,
        *,
        message: str,
        project_context: dict[str, Any],
        history: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tier: Literal["fast", "strong"],
    ) -> AgentDecision:
        payload = {
            "message": message,
            "project": project_context,
            "history": history,
            "tools": tools,
            "tier": tier,
        }
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return a JSON Editor Agent decision matching the supplied schema. "
                            "Use only listed tools. Never claim unexecuted changes."
                        ),
                    },
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "editor_decision",
                        "strict": True,
                        "schema": AgentDecision.model_json_schema(),
                    },
                },
            )
            content = response.choices[0].message.content
            return AgentDecision.model_validate_json(content or "{}")
        except (OpenAIError, ValueError, TypeError, IndexError) as exc:
            raise EditorProviderUnavailable("The local editor model is unavailable.") from exc


class GeminiEditorProvider:
    """Provider boundary for a future Gemini adapter without changing agent or tool code."""

    name = "gemini"

    def decide(self, **_kwargs) -> AgentDecision:
        raise EditorProviderUnavailable(
            "Gemini editor support requires a configured Gemini adapter and credential."
        )


class LocalEditorProvider:
    """Small offline fallback; configured model providers remain the primary interface."""

    name = "local"

    def decide(
        self,
        *,
        message: str,
        project_context: dict[str, Any],
        history: list[dict[str, str]],
        tools: list[dict[str, Any]],
        tier: Literal["fast", "strong"],
    ) -> AgentDecision:
        del tools, tier
        text = message.casefold()
        focus = str(project_context.get("conversation_focus") or "")
        calls: list[AgentToolCall] = []
        scene_match = re.search(r"\bscene\s+(\d+)\b", text)
        scene_number = int(scene_match.group(1)) if scene_match else _focused_scene(focus)

        if any(
            term in text
            for term in (
                "current script",
                "script say",
                "what is the script",
                "what's the script",
                "where can i find the script",
                "where is the script",
            )
        ):
            calls.append(AgentToolCall(name="get_script"))
        if any(term in text for term in ("which voice", "what voice", "voice are we", "narrator are we")):
            calls.append(AgentToolCall(name="get_voice_settings"))
        if any(term in text for term in ("how long", "aspect ratio", "timeline", "pacing")) and not _looks_like_action(text):
            calls.append(AgentToolCall(name="get_timeline_info"))
        if "caption" in text and any(term in text for term in ("how", "configured", "current")) and not _looks_like_action(text):
            calls.append(AgentToolCall(name="get_caption_settings"))
        if "source" in text and not _looks_like_action(text):
            calls.append(AgentToolCall(name="get_sources"))
        if any(term in text for term in ("already rendered", "render status", "playable")):
            calls.append(AgentToolCall(name="get_render_status"))
        if any(term in text for term in ("last revision", "what changed")):
            calls.append(AgentToolCall(name="get_current_revision"))
        if scene_number and any(term in text for term in ("what's scene", "what is scene", "scene", "footage", "clip")) and not _looks_like_action(text):
            calls.append(AgentToolCall(name="get_scene", arguments={"scene_number": scene_number}))
        if any(term in text for term in ("pexels clips", "which clip", "used this clip")):
            if scene_number:
                calls.append(AgentToolCall(name="get_media_for_scene", arguments={"scene_number": scene_number}))
            else:
                calls.append(AgentToolCall(name="get_scenes"))
        if any(term in text for term in ("summarize", "summary", "how many scenes")):
            calls.append(AgentToolCall(name="get_project_summary"))
        if "hook" in text and not _looks_like_action(text):
            calls.append(AgentToolCall(name="get_script"))

        time_range = _time_range(text)
        if time_range and not any(term in text for term in ("cut", "remove", "shorten")):
            calls.append(
                AgentToolCall(
                    name="get_timeline_range",
                    arguments={"start_seconds": time_range[0], "end_seconds": time_range[1]},
                )
            )
        if time_range and any(term in text for term in ("cut", "remove", "shorten")):
            calls.append(
                AgentToolCall(
                    name="cut_time_range",
                    arguments={"start_seconds": time_range[0], "end_seconds": time_range[1]},
                )
            )
        if any(term in text for term in ("undo", "go back", "rückgängig")):
            calls.append(AgentToolCall(name="undo_last_edit"))
        if any(term in text for term in ("redo", "wiederholen", "erneut anwenden")):
            calls.append(AgentToolCall(name="redo_last_edit"))
        if any(term in text for term in ("render it again", "render again", "rerender")):
            calls.append(AgentToolCall(name="render_video"))
        if scene_number and "remove" in text and "scene" in text:
            calls.append(AgentToolCall(name="remove_scene", arguments={"scene_number": scene_number}))
        if any(term in text for term in ("replace", "different", "change")) and any(
            term in text for term in ("footage", "clip", "visual")
        ):
            if scene_number:
                calls.append(AgentToolCall(name="replace_scene_media", arguments={"scene_number": scene_number}))
            else:
                return AgentDecision(clarification="Which scene's footage should I replace?")

        voice_context = any(
            term in text for term in ("voice", "narrator", "narration", "speaker")
        ) or focus == "voice"
        if voice_context and _looks_like_action(text):
            voice_args: dict[str, Any] = {}
            if any(term in text for term in ("deep", "deeper")):
                voice_args["tone"] = "deep"
            elif "warm" in text:
                voice_args["tone"] = "warm"
            elif "energetic" in text:
                voice_args["tone"] = "energetic"
            elif "calm" in text:
                voice_args["tone"] = "calm"
            if any(term in text for term in ("masculine", "male")):
                voice_args["presentation"] = "masculine"
            elif any(term in text for term in ("feminine", "female")):
                voice_args["presentation"] = "feminine"
            if "slower" in text:
                voice_args["speed"] = "slower"
            elif "faster" in text:
                voice_args["speed"] = "faster"
            if any(
                term in text
                for term in ("change the speaker", "change speaker", "change the narrator voice")
            ):
                voice_args["change_speaker"] = True
            if voice_args:
                calls.append(AgentToolCall(name="edit_voice", arguments=voice_args))

        if "caption" in text and _looks_like_action(text):
            caption_args: dict[str, Any] = {}
            if any(term in text for term in ("larger", "bigger")):
                caption_args["size"] = "larger"
            elif "smaller" in text:
                caption_args["size"] = "smaller"
            if any(term in text for term in ("higher", "upper")):
                caption_args["position"] = "upper"
            elif "lower" in text:
                caption_args["position"] = "lower"
            if caption_args:
                calls.append(AgentToolCall(name="edit_captions", arguments=caption_args))
        if any(term in text for term in ("9:16", "16:9", "1:1")) and _looks_like_action(text):
            ratio = next(value for value in ("9:16", "16:9", "1:1") if value in text)
            calls.append(AgentToolCall(name="change_format", arguments={"aspect_ratio": ratio}))
        if "hook" in text and any(term in text for term in ("stronger", "rewrite", "change")):
            calls.append(AgentToolCall(name="edit_script", arguments={"action": "stronger_hook"}))
        if "intro" in text and "shorter" in text:
            calls.append(AgentToolCall(name="edit_script", arguments={"action": "shorter_intro"}))
        if "pacing" in text and any(term in text for term in ("faster", "slower")):
            calls.append(
                AgentToolCall(
                    name="change_duration_or_pacing",
                    arguments={"pacing": "faster" if "faster" in text else "slower"},
                )
            )

        calls = _deduplicate_calls(calls)
        if calls:
            return AgentDecision(tool_calls=calls)
        if text in {"hello", "hi", "hey", "thanks", "thank you"}:
            return AgentDecision(answer="I’m here to help with this project. Ask me about it or describe a change in your own words.")
        return AgentDecision(
            clarification=(
                "I’m not sure whether you want information or a project change. "
                "Could you tell me a little more about the result you want?"
            )
        )


@dataclass(frozen=True)
class AgentTurnResult:
    assistant: ProjectChatMessage
    messages: list[ProjectChatMessage]
    project: Project
    tier: Literal["fast", "strong"]


class EditorModelRouter:
    def __init__(self, settings: Settings):
        self.word_threshold = settings.editor_agent_strong_word_threshold

    def route(self, message: str) -> Literal["fast", "strong"]:
        words = message.split()
        multi_step = any(term in message.casefold() for term in (" and then ", " then ", " after that "))
        mixed = "?" in message and _looks_like_action(message.casefold())
        return "strong" if len(words) >= self.word_threshold or multi_step or mixed else "fast"


def get_editor_provider(settings: Settings) -> EditorProvider:
    configured = settings.editor_agent_provider.casefold()
    if configured == "auto":
        configured = "openai" if settings.openai_api_key else "local"
    if configured == "openai":
        return OpenAIEditorProvider(settings)
    if configured in {"ollama", "local_model"}:
        return OpenAICompatibleEditorProvider(settings)
    if configured == "gemini":
        return GeminiEditorProvider()
    return LocalEditorProvider()


def run_editor_turn(
    db: Session,
    project: Project,
    message: str,
    settings: Settings,
    *,
    provider: EditorProvider | None = None,
    auto_render: bool = True,
) -> AgentTurnResult:
    safe_message = redact_sensitive_text(message, settings)
    previous_history = chat_history_for_context(
        db, project.id, limit=settings.editor_agent_history_limit
    )
    user_message = ProjectChatMessage(
        project_id=project.id,
        role="user",
        content=safe_message,
        tool_metadata={},
    )
    db.add(user_message)
    db.commit()

    context = redact_sensitive_value(build_project_context(project, previous_history), settings)
    tier = EditorModelRouter(settings).route(safe_message)
    explicit_decision = _explicit_script_deletion(
        safe_message,
        previous_history,
        str(context.get("script", {}).get("text") or ""),
    )
    if explicit_decision is not None:
        selected_provider: EditorProvider = LocalEditorProvider()
        decision = explicit_decision
    else:
        selected_provider = provider or get_editor_provider(settings)
        try:
            decision = selected_provider.decide(
                message=safe_message,
                project_context=context,
                history=[{"role": item.role, "content": item.content} for item in previous_history],
                tools=EditorToolbox.catalog(),
                tier=tier,
            )
        except Exception:  # noqa: BLE001 - provider adapters are an untrusted boundary
            selected_provider = LocalEditorProvider()
            decision = selected_provider.decide(
                message=safe_message,
                project_context=context,
                history=[{"role": item.role, "content": item.content} for item in previous_history],
                tools=EditorToolbox.catalog(),
                tier=tier,
            )

    calls = decision.tool_calls[: settings.editor_agent_max_tool_calls]
    toolbox = EditorToolbox(db, project, settings, auto_render=auto_render)
    results: list[ToolResult] = []
    if calls:
        mutating_indexes = [
            index for index, call in enumerate(calls) if TOOL_MAP.get(call.name) and TOOL_MAP[call.name].mutating
        ]
        final_mutation = mutating_indexes[-1] if mutating_indexes else None
        for index, call in enumerate(calls):
            result = toolbox.execute(
                call.name,
                call.arguments,
                auto_render=auto_render and index == final_mutation,
            )
            results.append(result)
        response_text = "\n\n".join(result.message for result in results)
    elif decision.clarification:
        response_text = decision.clarification
    elif decision.answer and not _looks_like_action(safe_message.casefold()):
        response_text = decision.answer
    elif decision.answer:
        response_text = (
            "I understood that as a project change, but no project tool was selected. "
            "Could you clarify the exact result you want?"
        )
    else:
        response_text = "Could you clarify what you would like me to inspect or change in this project?"

    response_text = redact_sensitive_text(response_text, settings)[:6_000]
    focus = next((result.focus for result in reversed(results) if result.focus), None)
    assistant = ProjectChatMessage(
        project_id=project.id,
        role="assistant",
        content=response_text,
        tool_metadata={
            "provider": selected_provider.name,
            "tier": tier,
            "focus": focus,
            "tools": [result.safe_metadata() for result in results],
        },
    )
    db.add(assistant)
    db.commit()
    refreshed = get_project(db, project.id) or project
    return AgentTurnResult(
        assistant=assistant,
        messages=list_chat_messages(db, project.id),
        project=refreshed,
        tier=tier,
    )


def list_chat_messages(db: Session, project_id: str, *, limit: int = 200) -> list[ProjectChatMessage]:
    messages = db.scalars(
        select(ProjectChatMessage)
        .where(ProjectChatMessage.project_id == project_id)
        .order_by(ProjectChatMessage.created_at.desc())
        .limit(limit)
    ).all()
    return list(reversed(messages))


def chat_history_for_context(
    db: Session, project_id: str, *, limit: int
) -> list[ProjectChatMessage]:
    return list_chat_messages(db, project_id, limit=max(2, min(limit, 40)))


def build_project_context(
    project: Project, history: list[ProjectChatMessage]
) -> dict[str, Any]:
    state = effective_revision_state(project)
    recent_revisions = sorted(project.revisions, key=lambda item: item.number, reverse=True)[:5]
    focus = next(
        (
            item.tool_metadata.get("focus")
            for item in reversed(history)
            if item.role == "assistant" and item.tool_metadata.get("focus")
        ),
        None,
    )
    scenes = []
    for index, scene in enumerate(state.get("scenes", []), 1):
        media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
        scenes.append(
            {
                "number": index,
                "start": scene.get("start"),
                "end": scene.get("end"),
                "narration": scene.get("narration"),
                "visual_goal": scene.get("visual_goal"),
                "media": {
                    "provider": media.get("provider"),
                    "kind": media.get("kind"),
                    "creator": media.get("creator"),
                    "query": media.get("query"),
                }
                if media
                else None,
            }
        )
    return {
        "project_id": project.id,
        "title": project.title,
        "current_revision": project.current_revision,
        "original_prompt": project.original_prompt,
        "intent": state.get("intent"),
        "script": {
            "text": state.get("script", {}).get("text"),
            "word_count": state.get("script", {}).get("word_count"),
        },
        "scenes": scenes,
        "timeline": state.get("timeline"),
        "duration": state.get("duration"),
        "voice": {
            key: value
            for key, value in state.get("voice", {}).items()
            if key in {"provider", "profile", "voice_id", "gender_presentation", "tone", "speed", "status"}
        },
        "captions": {
            key: value
            for key, value in state.get("captions", {}).items()
            if key not in {"items"}
        },
        "render": state.get("render"),
        "media_status": {
            key: value
            for key, value in state.get("assets", {}).items()
            if key in {"status", "provider", "diagnostic", "selected_count"}
        },
        "source_count": len(state.get("research", {}).get("sources", [])),
        "recent_revisions": [
            {
                "number": revision.number,
                "instruction": revision.instruction,
                "changed_components": revision.changed_components,
            }
            for revision in recent_revisions
        ],
        "conversation_focus": focus,
    }


def redact_sensitive_text(text: str, settings: Settings) -> str:
    clean = text
    for secret in (
        settings.openai_api_key,
        settings.brave_search_api_key,
        settings.pexels_api_key,
    ):
        if secret:
            clean = clean.replace(secret, "[redacted]")
    clean = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[redacted]", clean)
    clean = re.sub(
        r"(?i)\b(api[_ -]?key|authorization|bearer)\s*[:=]\s*\S+",
        r"\1: [redacted]",
        clean,
    )
    return clean


def redact_sensitive_value(value: Any, settings: Settings) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value, settings)
    if isinstance(value, list):
        return [redact_sensitive_value(item, settings) for item in value]
    if isinstance(value, dict):
        return {
            str(key): redact_sensitive_value(item, settings)
            for key, item in value.items()
        }
    return value


def serialize_chat_message(message: ProjectChatMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "tool_metadata": message.tool_metadata,
        "created_at": message.created_at,
    }


def serialize_agent_turn(result: AgentTurnResult) -> dict[str, Any]:
    return {
        "messages": [serialize_chat_message(message) for message in result.messages],
        "project": serialize_project(result.project),
    }


def _focused_scene(focus: str) -> int | None:
    match = re.fullmatch(r"scene:(\d+)", focus)
    return int(match.group(1)) if match else None


def _time_range(text: str) -> tuple[float, float] | None:
    timestamp = re.search(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})", text)
    if timestamp:
        return (
            int(timestamp.group(1)) * 60 + int(timestamp.group(2)),
            int(timestamp.group(3)) * 60 + int(timestamp.group(4)),
        )
    match = re.search(
        r"(?:from|between|seconds?)?\s*(\d+(?:\.\d+)?)\s*(?:s|seconds?)?\s*"
        r"(?:to|through|and|[-–—])\s*(\d+(?:\.\d+)?)\s*(?:s|seconds?)?",
        text,
    )
    if match:
        return float(match.group(1)), float(match.group(2))
    return None


def _looks_like_action(text: str) -> bool:
    return any(
        term in text
        for term in (
            "make ",
            "change ",
            "use ",
            "cut ",
            "remove ",
            "replace ",
            "move ",
            "render ",
            "undo",
            "shorten ",
            "slower",
            "faster",
            "deeper",
            "larger",
            "delete ",
            "entferne ",
            "entfern ",
            "lösche ",
            "lösch ",
            "loesche ",
            "nimm ",
        )
    )


def _explicit_script_deletion(
    message: str,
    history: list[ProjectChatMessage],
    script: str,
) -> AgentDecision | None:
    outside_quotes = _without_quoted_text(message).casefold()
    deletion = bool(
        re.search(r"\b(?:delete|remove)\b", outside_quotes)
        or re.search(r"\b(?:entfern\w*|lösch\w*|loesch\w*)\b", outside_quotes)
        or re.search(r"\bnimm\b.+\braus\b", outside_quotes)
        or "soll nicht im skript sein" in outside_quotes
    )
    script_context = any(
        term in outside_quotes
        for term in ("script", "skript", "narration", "video", "satz", "abschnitt")
    )
    deictic = any(
        term in outside_quotes
        for term in ("this", "it", "diesen", "diese", "diesen satz", "genau diesen")
    )
    quoted = _quoted_text(message)
    if not deletion or not (script_context or quoted or deictic):
        return None
    target = quoted[-1] if quoted else None
    if target is None and (deictic or "soll nicht im skript sein" in outside_quotes):
        for item in reversed(history):
            prior_targets = _quoted_text(item.content)
            if prior_targets:
                target = prior_targets[-1]
                break
    if target is None and re.search(r"\b(?:last sentence|letzten satz)\b", outside_quotes):
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", script)
            if sentence.strip()
        ]
        target = sentences[-1] if sentences else None
    if target:
        return AgentDecision(
            tool_calls=[
                AgentToolCall(
                    name="delete_script_text",
                    arguments={"target_text": target},
                )
            ]
        )
    return AgentDecision(
        clarification="Which exact sentence or passage should I remove from the script?"
    )


def _quoted_text(value: str) -> list[str]:
    matches = re.findall(r'"([^"]+)"|“([^”]+)”|„([^“]+)“', value)
    return [
        " ".join(next(part for part in match if part).split())
        for match in matches
        if any(match)
    ]


def _without_quoted_text(value: str) -> str:
    return re.sub(r'"[^"]*"|“[^”]*”|„[^“]*“', " ", value)


def _deduplicate_calls(calls: list[AgentToolCall]) -> list[AgentToolCall]:
    unique: list[AgentToolCall] = []
    seen: set[str] = set()
    for call in calls:
        identity = json.dumps(call.model_dump(), sort_keys=True)
        if identity not in seen:
            seen.add(identity)
            unique.append(call)
    return unique
