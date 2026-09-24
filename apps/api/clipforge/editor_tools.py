from __future__ import annotations

import copy
import difflib
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from .config import Settings
from .models import Project
from .pipeline import _normalise_blocks, _refresh_script_derivatives
from .renderer import RenderUnavailable
from .services import (
    RevisionConflict,
    current_revision,
    effective_revision_state,
    get_project,
    mutate_project_state,
    redo_project,
    render_project,
    undo_project,
)
from .voice import VoicePresentation, VoiceTone, apply_voice_preferences


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArguments(ToolArguments):
    pass


class SceneArguments(ToolArguments):
    scene_number: int = Field(ge=1)


class EditVoiceArguments(ToolArguments):
    presentation: VoicePresentation | None = None
    tone: VoiceTone | None = None
    speed: Literal["slower", "faster", "normal"] | None = None
    change_speaker: bool = False

    @model_validator(mode="after")
    def require_change(self):
        if not any((self.presentation, self.tone, self.speed, self.change_speaker)):
            raise ValueError("Choose a presentation, tone, speed, or speaker change.")
        return self


class EditScriptArguments(ToolArguments):
    action: Literal["shorter", "shorter_intro", "stronger_hook", "rewrite_intro", "clearer"]
    replacement_text: str | None = Field(default=None, min_length=3, max_length=800)


class DeleteScriptTextArguments(ToolArguments):
    target_text: str = Field(min_length=2, max_length=1_200)


class EditSceneArguments(ToolArguments):
    scene_number: int = Field(ge=1)
    narration_text: str | None = Field(default=None, min_length=2, max_length=1_200)
    visual_direction: str | None = Field(default=None, min_length=2, max_length=500)

    @model_validator(mode="after")
    def require_change(self):
        if not self.narration_text and not self.visual_direction:
            raise ValueError("Provide narration_text or visual_direction.")
        return self


class ReplaceSceneMediaArguments(SceneArguments):
    visual_direction: str | None = Field(default=None, min_length=2, max_length=500)


class EditCaptionsArguments(ToolArguments):
    size: Literal["larger", "smaller"] | None = None
    position: Literal["upper", "lower"] | None = None
    style: Literal["bold_clean", "minimal_lower_third", "karaoke"] | None = None
    density: Literal["normal", "reduced"] | None = None

    @model_validator(mode="after")
    def require_change(self):
        if not any((self.size, self.position, self.style, self.density)):
            raise ValueError("Choose a caption size, position, style, or density.")
        return self


class ChangeFormatArguments(ToolArguments):
    aspect_ratio: Literal["9:16", "1:1", "16:9"]


class ChangeDurationArguments(ToolArguments):
    min_seconds: int | None = Field(default=None, ge=10, le=180)
    max_seconds: int | None = Field(default=None, ge=10, le=180)
    pacing: Literal["slower", "balanced", "faster"] | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.min_seconds is None and self.max_seconds is None and self.pacing is None:
            raise ValueError("Provide min_seconds, max_seconds, or pacing.")
        if self.min_seconds is not None and self.max_seconds is not None and self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds cannot exceed max_seconds.")
        return self


class CutTimeRangeArguments(ToolArguments):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds.")
        if self.end_seconds - self.start_seconds < 0.1:
            raise ValueError("The selected range is too short to remove safely.")
        return self


class TimelineRangeArguments(ToolArguments):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds.")
        return self


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    arguments: type[ToolArguments]
    mutating: bool = False

    def public_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": self.arguments.model_json_schema(),
            "mutating": self.mutating,
        }


@dataclass
class ToolResult:
    name: str
    success: bool
    message: str
    data: dict[str, Any]
    mutating: bool = False
    revision: int | None = None
    focus: str | None = None

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "success": self.success,
            "mutating": self.mutating,
            "revision": self.revision,
        }


TOOL_DEFINITIONS = [
    ToolDefinition("get_project_summary", "Read a concise overview of the current project.", NoArguments),
    ToolDefinition("get_script", "Read the current narration script and hook.", NoArguments),
    ToolDefinition("get_scenes", "List current scenes with timing and purpose.", NoArguments),
    ToolDefinition("get_scene", "Read one scene by its one-based scene number.", SceneArguments),
    ToolDefinition("get_voice_settings", "Read the current narrator voice configuration.", NoArguments),
    ToolDefinition("get_caption_settings", "Read caption style, size, position, and density.", NoArguments),
    ToolDefinition("get_render_status", "Read whether the project has a current playable render.", NoArguments),
    ToolDefinition("get_sources", "Read the sources used by the project.", NoArguments),
    ToolDefinition("get_media_for_scene", "Read Pexels or fallback media for one scene.", SceneArguments),
    ToolDefinition("get_revision_history", "Read recent project revisions and changes.", NoArguments),
    ToolDefinition("get_current_revision", "Read the current revision number and last change.", NoArguments),
    ToolDefinition("get_timeline_info", "Read duration, aspect ratio, pacing, and scene timing.", NoArguments),
    ToolDefinition(
        "get_timeline_range",
        "Describe the scenes and narration inside a requested time range.",
        TimelineRangeArguments,
    ),
    ToolDefinition("edit_voice", "Change narrator presentation, tone, speed, or speaker.", EditVoiceArguments, True),
    ToolDefinition("edit_script", "Shorten or rewrite the current script or hook.", EditScriptArguments, True),
    ToolDefinition(
        "delete_script_text",
        "Delete exact quoted text from canonical narration and resynchronize dependent video state.",
        DeleteScriptTextArguments,
        True,
    ),
    ToolDefinition("edit_scene", "Change narration or visual direction for one scene.", EditSceneArguments, True),
    ToolDefinition("replace_scene_media", "Select different Pexels media for one scene.", ReplaceSceneMediaArguments, True),
    ToolDefinition("edit_captions", "Change caption size, position, style, or density.", EditCaptionsArguments, True),
    ToolDefinition("change_format", "Change the video aspect ratio.", ChangeFormatArguments, True),
    ToolDefinition("change_duration_or_pacing", "Change maximum duration or overall pacing.", ChangeDurationArguments, True),
    ToolDefinition("cut_time_range", "Remove a real time range and resynchronize the project.", CutTimeRangeArguments, True),
    ToolDefinition("remove_scene", "Remove one scene and its narration.", SceneArguments, True),
    ToolDefinition("render_video", "Render the current project again.", NoArguments, True),
    ToolDefinition("undo_last_edit", "Move the project back to its parent revision.", NoArguments, True),
    ToolDefinition("redo_last_edit", "Reapply the next edit on the active revision branch.", NoArguments, True),
]
TOOL_MAP = {definition.name: definition for definition in TOOL_DEFINITIONS}


class EditorToolbox:
    def __init__(
        self,
        db: Session,
        project: Project,
        settings: Settings,
        *,
        auto_render: bool = True,
    ):
        self.db = db
        self.project = project
        self.settings = settings
        self.auto_render = auto_render

    @property
    def state(self) -> dict[str, Any]:
        return effective_revision_state(self.project)

    @staticmethod
    def catalog() -> list[dict[str, Any]]:
        return [definition.public_schema() for definition in TOOL_DEFINITIONS]

    def execute(
        self, name: str, arguments: dict[str, Any], *, auto_render: bool | None = None
    ) -> ToolResult:
        definition = TOOL_MAP.get(name)
        if definition is None:
            return ToolResult(name, False, "That project tool is not available.", {})
        try:
            validated = definition.arguments.model_validate(arguments)
        except ValidationError as exc:
            message = exc.errors(include_url=False)[0].get("msg", "Invalid tool arguments")
            return ToolResult(name, False, f"I couldn't use {name}: {message}", {})
        handler = getattr(self, f"_tool_{name}")
        try:
            return handler(
                validated,
                self.auto_render if auto_render is None else auto_render,
            )
        except (RevisionConflict, RenderUnavailable, ValueError) as exc:
            return ToolResult(name, False, f"I couldn't complete that action: {exc}", {})
        except Exception:  # noqa: BLE001 - application-tool boundary must fail closed
            self.db.rollback()
            return ToolResult(
                name,
                False,
                "I couldn't complete that project action. The project was left in its last safe state.",
                {},
            )

    def _scene(self, scene_number: int) -> dict[str, Any]:
        scenes = self.state.get("scenes", [])
        if scene_number > len(scenes):
            raise ValueError(f"This project has only {len(scenes)} scenes.")
        return scenes[scene_number - 1]

    def _refresh_project(self) -> None:
        self.db.refresh(self.project)
        refreshed = get_project(self.db, self.project.id)
        if refreshed is not None:
            self.project = refreshed

    def _mutation_result(self, name: str, message: str, focus: str | None = None) -> ToolResult:
        self._refresh_project()
        revision = current_revision(self.project)
        render = revision.state.get("render", {})
        if render.get("status") == "complete":
            suffix = " The updated video was rendered successfully."
        elif render.get("status") == "regeneration_failed":
            suffix = " The edit was saved, but rendering failed, so the previous preview remains available."
        elif render.get("stale"):
            suffix = " The edit was saved and the previous preview remains available until rerendering completes."
        else:
            suffix = " The edit was saved in the project."
        return ToolResult(
            name,
            True,
            message + suffix,
            {"render_status": render.get("status")},
            mutating=True,
            revision=revision.number,
            focus=focus,
        )

    def _tool_get_project_summary(self, _args: NoArguments, _auto: bool) -> ToolResult:
        state = self.state
        duration = state["duration"].get("actual_seconds") or state["duration"]["estimated_seconds"]
        message = (
            f"This project is “{self.project.title}”. It has {len(state['scenes'])} scenes, "
            f"runs about {duration:.1f} seconds, and is on revision {self.project.current_revision}. "
            f"The current status is {self.project.status.replace('_', ' ')}."
        )
        return ToolResult("get_project_summary", True, message, {"scene_count": len(state["scenes"])})

    def _tool_get_script(self, _args: NoArguments, _auto: bool) -> ToolResult:
        script = self.state["script"]
        message = (
            f"The current script is {script['word_count']} words long:\n\n{script['text']}\n\n"
            "You can also view it in the Script tab."
        )
        return ToolResult("get_script", True, message, {"word_count": script["word_count"]}, focus="script")

    def _tool_get_scenes(self, _args: NoArguments, _auto: bool) -> ToolResult:
        lines = []
        for index, scene in enumerate(self.state["scenes"], 1):
            media = scene.get("media") or {}
            media_summary = (
                f" Pexels {media.get('kind')} by {media.get('creator', 'a contributor')}"
                f" for “{media.get('query', scene['visual_goal'])}”."
                if media
                else f" {scene.get('asset_status', 'fallback')} visual."
            )
            lines.append(
                f"Scene {index}: {scene['start']:.1f}–{scene['end']:.1f}s — "
                f"{scene['visual_goal']}.{media_summary}"
            )
        return ToolResult("get_scenes", True, "The current scenes are:\n" + "\n".join(lines), {"count": len(lines)}, focus="scenes")

    def _tool_get_scene(self, args: SceneArguments, _auto: bool) -> ToolResult:
        scene = self._scene(args.scene_number)
        message = (
            f"Scene {args.scene_number} runs from {scene['start']:.1f} to {scene['end']:.1f} seconds. "
            f"It says: “{scene['narration']}” Its visual direction is: {scene['visual_goal']}."
        )
        return ToolResult("get_scene", True, message, {"scene_number": args.scene_number}, focus=f"scene:{args.scene_number}")

    def _tool_get_voice_settings(self, _args: NoArguments, _auto: bool) -> ToolResult:
        voice = self.state["voice"]
        message = (
            f"The narrator is set to {voice.get('profile', 'natural')}, with a "
            f"{voice.get('gender_presentation', 'neutral')} presentation, "
            f"{voice.get('tone', 'natural')} tone, and {float(voice.get('speed', 1)):.2f}× speed."
        )
        return ToolResult("get_voice_settings", True, message, {"profile": voice.get("profile")}, focus="voice")

    def _tool_get_caption_settings(self, _args: NoArguments, _auto: bool) -> ToolResult:
        captions = self.state["captions"]
        message = (
            f"Captions use the {captions.get('style', 'bold clean').replace('_', ' ')} style "
            f"at size {captions.get('font_size', 72)}, positioned {captions.get('position', 'lower')}. "
            f"The highlight color is {captions.get('highlight_color', '#ffffff')}."
        )
        return ToolResult("get_caption_settings", True, message, {}, focus="captions")

    def _tool_get_render_status(self, _args: NoArguments, _auto: bool) -> ToolResult:
        render = self.state["render"]
        if render.get("status") == "complete" and render.get("url"):
            message = "The video is rendered and the current playable preview is available."
        elif render.get("url"):
            message = "A previous playable preview is available, but the latest revision still needs a successful render."
        else:
            message = f"There is no playable render yet. The render status is {render.get('status', 'unknown')}."
        return ToolResult("get_render_status", True, message, {"status": render.get("status")}, focus="render")

    def _tool_get_sources(self, _args: NoArguments, _auto: bool) -> ToolResult:
        sources = self.state.get("research", {}).get("sources", [])
        if not sources:
            message = "This project does not currently have external research sources."
        else:
            message = "The project uses these sources:\n" + "\n".join(
                f"• {source['label']}: {source['url']}" for source in sources
            )
        return ToolResult("get_sources", True, message, {"count": len(sources)}, focus="sources")

    def _tool_get_media_for_scene(self, args: SceneArguments, _auto: bool) -> ToolResult:
        scene = self._scene(args.scene_number)
        media = scene.get("media") or {}
        if media:
            message = (
                f"Scene {args.scene_number} uses a Pexels {media.get('kind')} by "
                f"{media.get('creator', 'a Pexels contributor')}, selected for the query "
                f"“{media.get('query', scene['visual_goal'])}”."
            )
        else:
            message = (
                f"Scene {args.scene_number} is using the {scene.get('asset_status', 'fallback')} "
                "visual because no downloaded media is attached."
            )
        return ToolResult("get_media_for_scene", True, message, {"kind": media.get("kind")}, focus=f"scene:{args.scene_number}")

    def _tool_get_revision_history(self, _args: NoArguments, _auto: bool) -> ToolResult:
        revisions = sorted(self.project.revisions, key=lambda item: item.number, reverse=True)[:8]
        lines = [f"v{item.number}: {item.instruction}" for item in revisions]
        return ToolResult("get_revision_history", True, "Recent revisions:\n" + "\n".join(lines), {"count": len(lines)}, focus="revisions")

    def _tool_get_current_revision(self, _args: NoArguments, _auto: bool) -> ToolResult:
        revision = current_revision(self.project)
        changed = ", ".join(revision.changed_components) or "project pointer"
        message = f"The current revision is v{revision.number}. Its change was “{revision.instruction}” and it affected {changed}."
        return ToolResult("get_current_revision", True, message, {"revision": revision.number}, focus="revisions")

    def _tool_get_timeline_info(self, _args: NoArguments, _auto: bool) -> ToolResult:
        state = self.state
        duration = state["duration"].get("actual_seconds") or state["timeline"]["duration"]
        timeline = state["timeline"]
        message = (
            f"The timeline is {duration:.1f} seconds long, uses {timeline['aspect_ratio']} at "
            f"{timeline['fps']} fps, and has {timeline.get('cut_pace', 'balanced')} pacing."
        )
        return ToolResult("get_timeline_info", True, message, {"duration": duration}, focus="timeline")

    def _tool_get_timeline_range(
        self, args: TimelineRangeArguments, _auto: bool
    ) -> ToolResult:
        total = float(self.state["timeline"]["duration"])
        if args.start_seconds >= total:
            raise ValueError(f"The video is only {total:.1f} seconds long.")
        end = min(args.end_seconds, total)
        matches = [
            (index, scene)
            for index, scene in enumerate(self.state["scenes"], 1)
            if float(scene["end"]) > args.start_seconds and float(scene["start"]) < end
        ]
        lines = [
            f"Scene {index} ({scene['start']:.1f}–{scene['end']:.1f}s): {scene['narration']}"
            for index, scene in matches
        ]
        message = (
            f"Between {args.start_seconds:g} and {end:g} seconds, the video covers:\n"
            + "\n".join(lines)
        )
        return ToolResult(
            "get_timeline_range",
            True,
            message,
            {"start_seconds": args.start_seconds, "end_seconds": end},
            focus="timeline",
        )

    def _tool_edit_voice(self, args: EditVoiceArguments, auto: bool) -> ToolResult:
        def mutate(state: dict) -> str:
            apply_voice_preferences(
                state["voice"],
                gender=args.presentation,
                tone=args.tone,
                speed=args.speed,
                change_speaker=args.change_speaker,
            )
            for block in state["voice"].get("blocks", []):
                block["status"] = "awaiting_tts"
            return "updated narrator voice"

        mutate_project_state(
            self.db,
            self.project,
            instruction="Editor Agent: update narrator voice",
            changed_roots={"voice"},
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result("edit_voice", "I updated the narrator voice.", "voice")

    def _tool_edit_script(self, args: EditScriptArguments, auto: bool) -> ToolResult:
        old_scenes = copy.deepcopy(self.state["scenes"])

        def mutate(state: dict) -> str:
            blocks = state["script"]["blocks"]
            if args.action == "shorter":
                if len(blocks) > 2:
                    blocks.pop(-2)
                elif len(blocks) == 2:
                    blocks.pop()
                else:
                    sentences = [
                        sentence.strip()
                        for sentence in blocks[0]["text"].split(".")
                        if sentence.strip()
                    ]
                    if len(sentences) > 1:
                        blocks[0]["text"] = sentences[0] + "."
            elif args.action == "shorter_intro":
                words = blocks[0]["text"].split()
                blocks[0]["text"] = " ".join(words[: max(4, math.ceil(len(words) * 0.65))])
            elif args.replacement_text:
                blocks[0]["text"] = args.replacement_text
            elif args.action in {"stronger_hook", "rewrite_intro"}:
                sentences = [
                    sentence.strip()
                    for sentence in blocks[0]["text"].split(".")
                    if sentence.strip()
                ]
                if sentences:
                    blocks[0]["text"] = sentences[0] + "."
                else:
                    raise ValueError("A stronger opening needs replacement_text.")
            else:
                raise ValueError("A clearer rewrite needs replacement_text.")
            blocks[:] = _normalise_blocks(blocks, int(state["duration"]["max_seconds"]), story_arc=state.get("story_arc"))
            _refresh_script_derivatives(state, old_scenes=old_scenes)
            _invalidate_scene_media(state)
            return args.action.replace("_", " ")

        mutate_project_state(
            self.db,
            self.project,
            instruction=f"Editor Agent: {args.action.replace('_', ' ')}",
            changed_roots={"script"},
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result("edit_script", f"I applied the {args.action.replace('_', ' ')} script edit.", "script")

    def _tool_delete_script_text(
        self, args: DeleteScriptTextArguments, auto: bool
    ) -> ToolResult:
        old_scenes = copy.deepcopy(self.state["scenes"])
        target = _normalize_deletion_target(args.target_text)

        def mutate(state: dict) -> str:
            blocks = state["script"]["blocks"]
            changed = False
            for block in blocks:
                updated, removed = _delete_text_match(str(block.get("text") or ""), target)
                if removed:
                    block["text"] = updated
                    changed = True
            if not changed:
                candidate = _closest_script_sentence(
                    target, [str(block.get("text") or "") for block in blocks]
                )
                detail = f" Closest text: “{candidate}”" if candidate else ""
                raise ValueError(f"The exact text was not found in the current script.{detail}")
            remaining = [block for block in blocks if str(block.get("text") or "").strip()]
            if not remaining:
                raise ValueError("That deletion would remove the entire narration.")
            state["script"]["blocks"] = _normalise_blocks(
                remaining, int(state["duration"]["max_seconds"]),
                story_arc=state.get("story_arc"),
            )
            _refresh_script_derivatives(state, old_scenes=old_scenes)
            _invalidate_scene_media(state)
            return "deleted exact text from canonical narration"

        mutate_project_state(
            self.db,
            self.project,
            instruction="Editor Agent: delete exact script text",
            changed_roots={"script"},
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result(
            "delete_script_text",
            "I removed the requested text from the script and resynchronized narration, scenes, captions, and render state.",
            "script",
        )

    def _tool_edit_scene(self, args: EditSceneArguments, auto: bool) -> ToolResult:
        scene = self._scene(args.scene_number)
        block_id = scene["block_id"]
        old_scenes = copy.deepcopy(self.state["scenes"])

        def mutate(state: dict) -> str:
            if args.narration_text:
                block = next(item for item in state["script"]["blocks"] if item["id"] == block_id)
                block["text"] = args.narration_text
                _refresh_script_derivatives(state, old_scenes=old_scenes)
            target = next(item for item in state["scenes"] if item["block_id"] == block_id)
            if args.visual_direction:
                target["visual_goal"] = args.visual_direction
                target["edit_instruction"] = args.visual_direction
            target.pop("media", None)
            target["asset_status"] = "replacement_required"
            state["assets"].update(status="search_required", license_manifest=[])
            return f"updated scene {args.scene_number}"

        roots = {"script"} if args.narration_text else {"assets"}
        mutate_project_state(
            self.db,
            self.project,
            instruction=f"Editor Agent: edit scene {args.scene_number}",
            changed_roots=roots,
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result("edit_scene", f"I updated scene {args.scene_number}.", f"scene:{args.scene_number}")

    def _tool_replace_scene_media(self, args: ReplaceSceneMediaArguments, auto: bool) -> ToolResult:
        scene = self._scene(args.scene_number)
        scene_id = scene["id"]
        preferred_media = (
            scene.get("media", {}).get("kind")
            if isinstance(scene.get("media"), dict)
            else scene.get("preferred_media")
        ) or "video"

        def mutate(state: dict) -> str:
            target = next(item for item in state["scenes"] if item["id"] == scene_id)
            target["asset_status"] = "replacement_required"
            target["preferred_media"] = preferred_media
            if args.visual_direction:
                target["visual_goal"] = args.visual_direction
                target["edit_instruction"] = args.visual_direction
            state["assets"].update(status="search_required", preference="different")
            return f"requested different footage for scene {args.scene_number}"

        mutate_project_state(
            self.db,
            self.project,
            instruction=f"Editor Agent: replace scene {args.scene_number} footage",
            changed_roots={"assets"},
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result(
            "replace_scene_media",
            f"I requested different footage for scene {args.scene_number}.",
            f"scene:{args.scene_number}",
        )

    def _tool_edit_captions(self, args: EditCaptionsArguments, auto: bool) -> ToolResult:
        def mutate(state: dict) -> str:
            captions = state["captions"]
            if args.size == "larger":
                captions["font_size"] = min(112, int(captions.get("font_size", 72)) + 10)
            elif args.size == "smaller":
                captions["font_size"] = max(36, int(captions.get("font_size", 72)) - 10)
            if args.position:
                captions["position"] = args.position
            if args.style:
                captions["style"] = args.style
            if args.density:
                captions["density"] = args.density
                if args.density == "reduced":
                    captions["items"] = captions.get("items", [])[::2]
            return "updated captions"

        mutate_project_state(
            self.db,
            self.project,
            instruction="Editor Agent: update captions",
            changed_roots={"captions"},
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result("edit_captions", "I updated the caption configuration.", "captions")

    def _tool_change_format(self, args: ChangeFormatArguments, auto: bool) -> ToolResult:
        def mutate(state: dict) -> str:
            width, height = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}[args.aspect_ratio]
            state["timeline"].update(width=width, height=height, aspect_ratio=args.aspect_ratio)
            state.setdefault("options", {})["aspect_ratio"] = args.aspect_ratio
            return f"changed format to {args.aspect_ratio}"

        mutate_project_state(
            self.db,
            self.project,
            instruction=f"Editor Agent: change format to {args.aspect_ratio}",
            changed_roots={"timeline"},
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result("change_format", f"I changed the video format to {args.aspect_ratio}.", "timeline")

    def _tool_change_duration_or_pacing(self, args: ChangeDurationArguments, auto: bool) -> ToolResult:
        old_scenes = copy.deepcopy(self.state["scenes"])

        def mutate(state: dict) -> str:
            roots = []
            if args.min_seconds is not None:
                maximum = args.max_seconds or int(state["duration"]["max_seconds"])
                if args.min_seconds > maximum:
                    raise ValueError("Minimum duration cannot exceed maximum duration.")
                state["duration"]["minimum_seconds"] = args.min_seconds
                state.setdefault("options", {})["min_duration"] = args.min_seconds
                roots.append(f"{args.min_seconds} second minimum")
            if args.max_seconds is not None:
                existing_minimum = state["duration"].get("minimum_seconds")
                if existing_minimum is not None and int(existing_minimum) > args.max_seconds:
                    raise ValueError("Maximum duration cannot be shorter than the selected minimum.")
                state["duration"]["max_seconds"] = args.max_seconds
                state.setdefault("options", {})["max_duration"] = args.max_seconds
                blocks = _normalise_blocks(state["script"]["blocks"], args.max_seconds, story_arc=state.get("story_arc"))
                state["script"]["blocks"] = blocks
                _refresh_script_derivatives(state, old_scenes=old_scenes)
                _invalidate_scene_media(state)
                roots.append(f"{args.max_seconds} second maximum")
            if args.pacing:
                pace = {"faster": "fast", "slower": "slow", "balanced": "balanced"}[args.pacing]
                state["timeline"]["cut_pace"] = pace
                state.setdefault("options", {})["pacing"] = pace
                _refresh_script_derivatives(state, old_scenes=old_scenes)
                _invalidate_scene_media(state)
                roots.append(f"{args.pacing} pacing")
            if args.min_seconds is not None and args.max_seconds is None:
                _refresh_script_derivatives(state, old_scenes=old_scenes)
            return " and ".join(roots)

        changed_roots = {"script"} if args.max_seconds is not None or args.min_seconds is not None else {"timeline"}
        mutate_project_state(
            self.db,
            self.project,
            instruction="Editor Agent: change duration or pacing",
            changed_roots=changed_roots,
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result("change_duration_or_pacing", "I updated the video duration and pacing.", "timeline")

    def _tool_cut_time_range(self, args: CutTimeRangeArguments, auto: bool) -> ToolResult:
        old_state = copy.deepcopy(self.state)
        total_duration = float(old_state["timeline"]["duration"])
        if args.start_seconds >= total_duration:
            raise ValueError(f"The video is only {total_duration:.1f} seconds long.")
        end = min(args.end_seconds, total_duration)

        def mutate(state: dict) -> str:
            blocks_by_id = {block["id"]: block for block in state["script"]["blocks"]}
            remove_ids: set[str] = set()
            for scene in old_state["scenes"]:
                overlap_start = max(args.start_seconds, float(scene["start"]))
                overlap_end = min(end, float(scene["end"]))
                if overlap_end <= overlap_start:
                    continue
                block = blocks_by_id.get(scene["block_id"])
                if block is None:
                    continue
                scene_duration = max(0.1, float(scene["end"]) - float(scene["start"]))
                words = block["text"].split()
                start_index = math.floor((overlap_start - float(scene["start"])) / scene_duration * len(words))
                end_index = math.ceil((overlap_end - float(scene["start"])) / scene_duration * len(words))
                remaining = words[:start_index] + words[end_index:]
                if len(remaining) < 2:
                    remove_ids.add(block["id"])
                else:
                    block["text"] = " ".join(remaining)
            remaining_blocks = [block for block in state["script"]["blocks"] if block["id"] not in remove_ids]
            if not remaining_blocks:
                raise ValueError("That cut would remove the entire narration.")
            state["script"]["blocks"] = _normalise_blocks(
                remaining_blocks, int(state["duration"]["max_seconds"]),
                story_arc=state.get("story_arc"),
            )
            _refresh_script_derivatives(state, old_scenes=[])
            _invalidate_scene_media(state)
            return f"removed {args.start_seconds:g}–{end:g} seconds and resynchronized the timeline"

        mutate_project_state(
            self.db,
            self.project,
            instruction=f"Editor Agent: cut {args.start_seconds:g}–{end:g} seconds",
            changed_roots={"script"},
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result("cut_time_range", f"I removed the section from {args.start_seconds:g} to {end:g} seconds and resynchronized narration, scenes, and captions.", "timeline")

    def _tool_remove_scene(self, args: SceneArguments, auto: bool) -> ToolResult:
        scene = self._scene(args.scene_number)
        block_id = scene["block_id"]

        def mutate(state: dict) -> str:
            blocks = [block for block in state["script"]["blocks"] if block["id"] != block_id]
            if not blocks:
                raise ValueError("The only scene cannot be removed.")
            state["script"]["blocks"] = _normalise_blocks(
                blocks, int(state["duration"]["max_seconds"]),
                story_arc=state.get("story_arc"),
            )
            _refresh_script_derivatives(state, old_scenes=[])
            _invalidate_scene_media(state)
            return f"removed scene {args.scene_number}"

        mutate_project_state(
            self.db,
            self.project,
            instruction=f"Editor Agent: remove scene {args.scene_number}",
            changed_roots={"script"},
            mutate=mutate,
            settings=self.settings,
            auto_render=auto,
        )
        return self._mutation_result("remove_scene", f"I removed scene {args.scene_number} and resynchronized the project.", "scenes")

    def _tool_render_video(self, _args: NoArguments, _auto: bool) -> ToolResult:
        render_project(self.db, self.project, self.settings)
        return self._mutation_result("render_video", "I rendered the current project.", "render")

    def _tool_undo_last_edit(self, _args: NoArguments, _auto: bool) -> ToolResult:
        target = undo_project(self.db, self.project)
        if target is None:
            raise ValueError("There is no earlier revision to restore.")
        return self._mutation_result("undo_last_edit", f"I restored revision {target.number}.", "revisions")

    def _tool_redo_last_edit(self, _args: NoArguments, _auto: bool) -> ToolResult:
        target = redo_project(self.db, self.project)
        if target is None:
            raise ValueError("There is no edit to redo on the active branch.")
        return self._mutation_result(
            "redo_last_edit", f"I restored revision {target.number}.", "revisions"
        )


def _invalidate_scene_media(state: dict) -> None:
    for scene in state.get("scenes", []):
        scene.pop("media", None)
        scene["asset_status"] = "search_required"
        scene["preferred_media"] = "video"
    state.setdefault("assets", {}).update(status="search_required", license_manifest=[])


def _normalize_deletion_target(value: str) -> str:
    target = " ".join(value.strip().strip('"“”„').split())
    return target.strip()


def _delete_text_match(text: str, target: str) -> tuple[str, bool]:
    if not target:
        return text, False
    flexible = r"\s+".join(re.escape(part) for part in target.split())
    match = re.search(flexible, text, flags=re.IGNORECASE)
    if match is None and target[:1] in ";,:–—-":
        flexible = r"\s+".join(re.escape(part) for part in target[1:].strip().split())
        match = re.search(rf"\s*[;,:–—-]?\s*{flexible}", text, flags=re.IGNORECASE)
    if match is None:
        return text, False
    updated = text[: match.start()] + text[match.end() :]
    updated = re.sub(r"\s+([,.;:!?])", r"\1", updated)
    updated = re.sub(r"([,;:])\s*([.!?])", r"\2", updated)
    updated = re.sub(r"\s{2,}", " ", updated).strip(" ,;:–—-")
    if updated and updated[-1] not in ".!?":
        updated += "."
    return updated, True


def _closest_script_sentence(target: str, blocks: list[str]) -> str | None:
    sentences = [
        sentence.strip()
        for block in blocks
        for sentence in re.split(r"(?<=[.!?])\s+", block)
        if sentence.strip()
    ]
    matches = difflib.get_close_matches(target, sentences, n=1, cutoff=0.45)
    return matches[0] if matches else None
