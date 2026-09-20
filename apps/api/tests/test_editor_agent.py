import copy
import json

from sqlalchemy import update

from clipforge.config import Settings
from clipforge.editor_agent import (
    AgentDecision,
    AgentToolCall,
    EditorModelRouter,
    list_chat_messages,
    run_editor_turn,
)
from clipforge.editor_tools import EditorToolbox
from clipforge.models import ProjectChatMessage, ProjectRevision
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.services import create_project, current_revision


def agent_settings(tmp_path, **updates) -> Settings:
    values = {
        "_env_file": None,
        "clipforge_ai_mode": "local",
        "editor_agent_provider": "local",
        "openai_api_key": None,
        "brave_search_api_key": None,
        "pexels_api_key": None,
        "render_root": tmp_path,
    }
    values.update(updates)
    return Settings(**values)


def project_for_agent(db, settings):
    return create_project(
        db,
        ProjectCreate(
            prompt="Write a fictional story about a lighthouse keeper during a violent storm",
            options=AdvancedOptions(max_duration=45),
        ),
        settings,
    )


def install_yellowstone_editorial_sentence(db, project, settings):
    del settings
    revision = current_revision(project)
    state = copy.deepcopy(revision.state)
    state["script"]["blocks"] = [
            {
                "id": "voice_block_01",
                "role": "answer",
                "text": (
                    "If Yellowstone erupted tomorrow, lava flows would have minimal direct "
                    "effect outside Yellowstone National Park."
                ),
            },
            {
                "id": "voice_block_02",
                "role": "context",
                "text": (
                    "A Yellowstone Volcano Observatory scientist says there is currently no "
                    "activity indicating that an eruption is coming."
                ),
            },
            {
                "id": "voice_block_03",
                "role": "support",
                "text": (
                    "That assessment is based on the provided material; add the original "
                    "source links before publication."
                ),
            },
        ]
    state["script"]["text"] = " ".join(
        block["text"] for block in state["script"]["blocks"]
    )
    state["script"]["word_count"] = len(state["script"]["text"].split())
    db.execute(
        update(ProjectRevision)
        .where(ProjectRevision.id == revision.id)
        .values(state=state)
    )
    db.commit()
    db.expire_all()
    db.refresh(project)


class StaticProvider:
    name = "test"

    def __init__(self, decision: AgentDecision):
        self.decision = decision
        self.seen = None

    def decide(self, **kwargs) -> AgentDecision:
        self.seen = kwargs
        return self.decision


def test_current_script_answer_comes_from_project_state(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    script = current_revision(project).state["script"]["text"]

    result = run_editor_turn(
        db, project, "What is the current script?", settings, auto_render=False
    )

    assert script in result.assistant.content
    assert project.current_revision == 1


def test_voice_question_reads_project_state_without_revision(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)

    result = run_editor_turn(
        db, project, "Which voice are we using?", settings, auto_render=False
    )

    assert current_revision(project).state["voice"]["profile"] in result.assistant.content
    assert project.current_revision == 1


def test_deeper_voice_message_runs_real_voice_edit(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)

    result = run_editor_turn(
        db, project, "Make the voice deeper", settings, auto_render=False
    )
    voice = current_revision(result.project).state["voice"]

    assert result.project.current_revision == 2
    assert voice["tone"] == "deep"
    assert voice["voice_id"] == "onyx"
    assert result.assistant.tool_metadata["tools"][0]["tool"] == "edit_voice"


def test_followup_pronoun_uses_persisted_voice_focus(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    first = run_editor_turn(
        db, project, "Which voice are we using?", settings, auto_render=False
    )

    second = run_editor_turn(
        db, first.project, "Make it slower", settings, auto_render=False
    )

    assert current_revision(second.project).state["voice"]["speed"] < 1
    assert second.project.current_revision == 2


def test_time_range_cut_changes_real_script_and_timeline(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    before = current_revision(project).state
    assert before["timeline"]["duration"] > 17

    result = run_editor_turn(
        db, project, "Cut seconds 13 to 17", settings, auto_render=False
    )
    after = current_revision(result.project).state

    assert result.project.current_revision == 2
    assert after["script"]["word_count"] < before["script"]["word_count"]
    assert after["timeline"]["duration"] < before["timeline"]["duration"]
    assert after["captions"]["items"]


def test_replace_scene_footage_runs_media_tool(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)

    result = run_editor_turn(
        db, project, "Replace the footage in scene 2", settings, auto_render=False
    )
    scene = current_revision(result.project).state["scenes"][1]

    assert scene["asset_status"] == "replacement_required"
    assert scene["preferred_media"] == "video"
    assert result.assistant.tool_metadata["tools"][0]["tool"] == "replace_scene_media"


def test_failed_tool_is_reported_without_success_claim(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    provider = StaticProvider(
        AgentDecision(
            tool_calls=[
                AgentToolCall(name="remove_scene", arguments={"scene_number": 999})
            ]
        )
    )

    result = run_editor_turn(
        db,
        project,
        "Remove scene 999",
        settings,
        provider=provider,
        auto_render=False,
    )

    assert result.project.current_revision == 1
    assert "couldn't complete" in result.assistant.content.casefold()
    assert "i removed" not in result.assistant.content.casefold()
    assert result.assistant.tool_metadata["tools"][0]["success"] is False


def test_ambiguous_action_asks_for_clarification(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)

    result = run_editor_turn(
        db, project, "Make it better", settings, auto_render=False
    )

    assert "more" in result.assistant.content.casefold()
    assert result.project.current_revision == 1


def test_invalid_tool_arguments_are_rejected(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    toolbox = EditorToolbox(db, project, settings, auto_render=False)

    result = toolbox.execute("edit_voice", {"voice_id": "unsupported"})

    assert result.success is False
    assert project.current_revision == 1


def test_chat_messages_persist_and_can_be_reloaded(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    run_editor_turn(db, project, "What is the current script?", settings, auto_render=False)

    reloaded = list_chat_messages(db, project.id)

    assert [message.role for message in reloaded] == ["user", "assistant"]
    assert reloaded[0].content == "What is the current script?"
    assert current_revision(project).state["script"]["text"] in reloaded[1].content


def test_context_history_is_bounded(db, tmp_path):
    settings = agent_settings(tmp_path, editor_agent_history_limit=4)
    project = project_for_agent(db, settings)
    for index in range(12):
        db.add(
            ProjectChatMessage(
                project_id=project.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"message {index}",
                tool_metadata={},
            )
        )
    db.commit()
    provider = StaticProvider(AgentDecision(answer="Hello."))

    run_editor_turn(db, project, "Hello", settings, provider=provider, auto_render=False)

    assert provider.seen is not None
    assert len(provider.seen["history"]) == 4
    assert provider.seen["history"][0]["content"] == "message 8"


def test_agent_context_and_responses_redact_secrets(db, tmp_path):
    secret = "sk-unit-test-sensitive-value-123456"
    settings = agent_settings(tmp_path, openai_api_key=secret)
    project = project_for_agent(db, settings)
    provider = StaticProvider(AgentDecision(answer=f"Credential: {secret}"))

    result = run_editor_turn(
        db,
        project,
        f"Hello, here is {secret}",
        settings,
        provider=provider,
        auto_render=False,
    )

    assert provider.seen is not None
    assert secret not in json.dumps(provider.seen, default=str)
    assert secret not in result.assistant.content
    assert secret not in " ".join(message.content for message in result.messages)


def test_quoted_source_text_edit_outranks_source_keyword_and_resynchronizes(
    db, tmp_path
):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    install_yellowstone_editorial_sentence(db, project, settings)
    wrong_read_only_decision = StaticProvider(
        AgentDecision(tool_calls=[AgentToolCall(name="get_sources")])
    )

    result = run_editor_turn(
        db,
        project,
        '"; add the original source links before publication." Das soll nicht im Skript sein',
        settings,
        provider=wrong_read_only_decision,
        auto_render=False,
    )
    state = current_revision(result.project).state
    downstream = json.dumps(
        {
            "script": state["script"],
            "scenes": state["scenes"],
            "captions": state["captions"]["items"],
        }
    ).casefold()

    assert result.project.current_revision == 2
    assert "source links before publication" not in downstream
    assert "removed the requested text" in result.assistant.content.casefold()
    assert result.assistant.tool_metadata["tools"][0]["tool"] == "delete_script_text"
    assert wrong_read_only_decision.seen is None


def test_delete_quoted_yellowstone_text_from_script_and_video(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    install_yellowstone_editorial_sentence(db, project, settings)

    result = run_editor_turn(
        db,
        project,
        'Delete "add the original source links before publication" from the script and delete it in the video',
        settings,
        auto_render=False,
    )
    state = current_revision(result.project).state

    assert result.project.current_revision == 2
    assert "add the original source links" not in state["script"]["text"].casefold()
    assert all(
        "add the original source links" not in item["text"].casefold()
        for item in state["captions"]["items"]
    )
    assert state["render"]["status"] == "regeneration_required"


def test_german_deictic_script_deletion_uses_recent_quoted_target(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    install_yellowstone_editorial_sentence(db, project, settings)
    db.add(
        ProjectChatMessage(
            project_id=project.id,
            role="user",
            content='"add the original source links before publication"',
            tool_metadata={},
        )
    )
    db.commit()

    result = run_editor_turn(
        db, project, "Entferne diesen Satz", settings, auto_render=False
    )

    assert result.project.current_revision == 2
    assert "source links before publication" not in current_revision(result.project).state[
        "script"
    ]["text"].casefold()


def test_exact_text_not_found_does_not_create_revision_or_claim_success(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)

    result = run_editor_turn(
        db,
        project,
        'Lösch genau diesen Abschnitt: "This sentence is not in the narration."',
        settings,
        auto_render=False,
    )

    assert result.project.current_revision == 1
    assert "exact text was not found" in result.assistant.content.casefold()
    assert "i removed" not in result.assistant.content.casefold()
    assert result.assistant.tool_metadata["tools"][0]["success"] is False


def test_remove_last_sentence_is_a_real_mutation(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    before = current_revision(project).state["script"]["text"]
    last_sentence = before.rsplit(". ", 1)[-1]

    result = run_editor_turn(
        db, project, "Nimm den letzten Satz raus", settings, auto_render=False
    )

    assert result.project.current_revision == 2
    assert last_sentence not in current_revision(result.project).state["script"]["text"]


def test_action_answer_without_tool_becomes_clarification(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)
    provider = StaticProvider(AgentDecision(answer="Done, I changed it."))

    result = run_editor_turn(
        db,
        project,
        "Make the voice deeper",
        settings,
        provider=provider,
        auto_render=False,
    )

    assert "no project tool" in result.assistant.content
    assert result.project.current_revision == 1


def test_model_router_uses_stronger_tier_only_for_complex_requests(tmp_path):
    settings = agent_settings(tmp_path, editor_agent_strong_word_threshold=12)
    router = EditorModelRouter(settings)

    assert router.route("Which voice are we using?") == "fast"
    assert router.route("Show me the script and then shorten the intro after that") == "strong"


def test_timestamp_question_reads_the_intersecting_scenes(db, tmp_path):
    settings = agent_settings(tmp_path)
    project = project_for_agent(db, settings)

    result = run_editor_turn(
        db, project, "What happens between 00:13–00:17?", settings, auto_render=False
    )

    assert "Between 13 and 17 seconds" in result.assistant.content
    assert "Scene" in result.assistant.content
