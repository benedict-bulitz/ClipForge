"""Story Arc x structured visual semantics, end to end.

The story arc decides WHAT is withheld (its primary answer, by fact
identity); the visual plan's target keys say HOW it is depicted; media search
consumes the bound ``protected_visual_target`` and only before the reveal.
"""
import copy
from pathlib import Path

import pytest
from test_staged_media_search import Commons, Provider, Verifier, cand, settings_for

from clipforge.ai import (
    AIIntent,
    AIPayoffPlan,
    AIPlanResult,
    AIProjectPlan,
    AIScriptBlock,
    AIVisualIntent,
)
from clipforge.config import Settings
from clipforge.media import build_visual_query_plan, prepare_project_media
from clipforge.pacing import analyze_pacing
from clipforge.pipeline import _refresh_script_derivatives, build_initial_state
from clipforge.reactions import plan_viewer_reactions
from clipforge.research import ResearchResult
from clipforge.review import local_review_items
from clipforge.schemas import AdvancedOptions
from clipforge.story_arc import annotate_story_roles


def fact(claim: str, importance: float = 0.8) -> dict:
    return {
        "claim": claim, "importance": importance, "confidence": 0.9, "verification": "source_attributed",
        "sources": [{"label": "a", "url": f"https://a.test/{abs(hash(claim))}"}, {"label": "b", "url": f"https://b.test/{abs(hash(claim))}"}],
    }


def generate(monkeypatch, tmp_path: Path, question: str, blocks: list[tuple[dict, str, AIVisualIntent]], planner_target: str) -> dict:
    """Run the real pipeline with fixture research and a planner that supplies visual targets."""
    facts = [item for item, _role, _visual in blocks]
    plan = AIProjectPlan(
        intent=AIIntent(
            topic=question, intent="explain", question=question, language="de", content_type="factual_explainer",
            tone="documentary", research_required=True, visual_style="documentary",
        ),
        research_questions=[], facts=[], answer_skeleton=["ANSWER", "SUPPORT"],
        script_blocks=[AIScriptBlock(role=role, text=item["claim"]) for item, role, _visual in blocks],
        music_mood="documentary",
        visual_intents=[visual for _item, _role, visual in blocks],
        payoff_plan=AIPayoffPlan(curiosity_question=question, payoff=facts[-1]["claim"], protected_visual_target=planner_target),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult([dict(item) for item in facts], [{"label": "a", "url": "https://a.test"}], "verified_sources", "fixture"),
    )
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: AIPlanResult(plan, "connected"))
    return build_initial_state(question, AdvancedOptions(), Settings(clipforge_ai_mode="local", openai_api_key=None, render_root=tmp_path))


def visual(goal: str, queries: list[str], targets: list[str]) -> AIVisualIntent:
    return AIVisualIntent(visual_goal=goal, objects=[goal.split()[0]], media_queries=queries, media_query_targets=targets)


ISLANDS_Q = "Welches Land hat mehr Inseln – Schweden oder Indonesien?"
ISLANDS = [
    (fact("Indonesien hat etwa 17.000 Inseln."), "support",
     visual("Indonesian islands from above", ["indonesian islands aerial"], ["subject_b"])),
    (fact("Schweden hat rund 267.570 Inseln – mehr als jedes andere Land der Welt.", 0.95), "answer",
     visual("Swedish archipelago from above", ["swedish archipelago", "swedish islands aerial"], ["subject_a", "subject_a"])),
    (fact("In der Eiszeit haben Gletscher Schwedens Küste zerklüftet; dadurch entstanden unzählige kleine Inseln."), "explanation",
     visual("glacier carved rocky coastline", ["glacier carved coastline"], ["context"])),
    (fact("Indonesien ist trotzdem der größte Inselstaat der Welt.", 0.7), "payoff",
     visual("Indonesian island nation from above", ["indonesian island nation"], ["subject_b"])),
]


def media_provider() -> Provider:
    return Provider(videos={
        "indonesian islands aerial": [cand("id", "indonesian islands aerial", "Indonesian islands aerial drone view")],
        "swedish archipelago": [cand("se", "swedish archipelago", "Swedish archipelago islands from above")],
        "glacier carved coastline": [cand("gl", "glacier carved coastline", "Glacier carved rocky coastline")],
        "indonesian island nation": [cand("nation", "indonesian island nation", "Indonesian island nation aerial islands")],
    })


def scene_by_role(state: dict, role: str) -> dict:
    return next(scene for scene in state["scenes"] if scene.get("story_role") == role)


# --- required Sweden / Indonesia integration ---------------------------------

def test_story_arc_drives_visual_protection_end_to_end(monkeypatch, tmp_path):
    # The planner confuses the final payoff (Indonesia) with the answer.
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS, planner_target="subject_b")
    arc = state["story_arc"]

    # Story roles
    assert arc["primary_answer_id"] == "fact_02"  # Sweden
    assert arc["final_payoff_id"] == "fact_04"  # Indonesia island nation
    assert arc["curiosity_gap"]["withhold_answer"] is True

    # Story arc -> structured visual target (identity, not spelling; arc wins)
    protection = arc["visual_protection"]
    assert state["payoff_plan"]["protected_visual_target"] == "subject_a"
    assert protection == {**protection, "source": "story_arc", "planner_target": "subject_b", "planner_conflict": True, "scope": "before_reveal"}
    assert state["payoff_plan"]["hook_must_not_reveal"].rstrip(".") == "Schweden"  # German text, English queries

    # Story identity reaches scenes and visual intents
    answer, final = scene_by_role(state, "primary_answer"), scene_by_role(state, "secondary_insight")
    evidence = scene_by_role(state, "comparison")
    assert answer["story_stage"] == "reveal" and answer["visual_intent"]["story_role"] == "primary_answer"
    assert final["is_final_payoff"] and final["story_stage"] == "after_reveal"
    assert evidence["story_stage"] == "before_reveal"
    assert answer["visual_intent"]["media_query_targets"] == ["subject_a", "subject_a"]

    pexels, commons = media_provider(), Commons()
    prepare_project_media(state, "project", settings_for(tmp_path), client=pexels, fallback_client=commons, visual_verifier=Verifier())

    for scene in state["scenes"]:
        search = scene["media_search"]
        assert search["logical_queries_executed"] <= 3
        executed = " ".join(search["executed_queries"]).casefold()
        if scene["story_stage"] == "before_reveal":
            # Sweden's visual target never runs before the reveal ...
            assert "swedish" not in executed and "schweden" not in executed
    # ... allowed evidence still runs before the reveal,
    assert evidence["media_search"]["executed_queries"][0] == "indonesian islands aerial"
    assert evidence["media"]["provider_id"] == "id"
    # after the reveal the Sweden visual is allowed,
    assert answer["media_search"]["executed_queries"][0] == "swedish archipelago"
    assert answer["media"]["provider_id"] == "se"
    # and the final Indonesia insight stays representable.
    assert final["media"]["provider_id"] == "nation"
    assert "swedish" not in " ".join(pexels.queries[:1]).casefold()


def test_before_reveal_scene_plan_blocks_the_answer_target_and_revealed_scene_allows_it(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS, planner_target="")
    evidence = copy.deepcopy(scene_by_role(state, "comparison"))
    evidence["visual_intent"].update(
        media_queries=["indonesian islands aerial", "swedish archipelago", "islands aerial"],
        media_query_targets=["subject_b", "subject_a", "shared"],
    )
    revealed = copy.deepcopy(evidence) | {"story_stage": "after_reveal"}

    before_plan = build_visual_query_plan(evidence, state)
    after_plan = build_visual_query_plan(revealed, state)

    assert state["story_arc"]["visual_protection"]["source"] == "story_arc"
    assert "swedish archipelago" not in before_plan["queries"]
    assert before_plan["protected_targets"] == ["subject_a"] and before_plan["protection_scope"] == "before_reveal"
    assert "swedish archipelago" in after_plan["queries"]
    assert after_plan["protected_targets"] == [] and after_plan["protection_scope"] == "revealed"


# --- unseen topic -------------------------------------------------------------

DIVE_Q = "Wer taucht tiefer: Pottwal oder Kaiserpinguin?"
DIVE = [
    (fact("Kaiserpinguine tauchen bis zu 565 Meter tief."), "support",
     visual("emperor penguin diving underwater", ["emperor penguin diving"], ["subject_b"])),
    (fact("Pottwale tauchen über 2.000 Meter tief – tiefer als fast jedes andere Säugetier.", 0.95), "answer",
     visual("sperm whale diving into the deep", ["sperm whale diving"], ["subject_a"])),
    (fact("Sie speichern viel Sauerstoff in ihren Muskeln; deshalb halten sie lange ohne Luft aus."), "explanation",
     visual("deep dark ocean water", ["deep ocean darkness"], ["context"])),
    (fact("Der Kaiserpinguin ist trotzdem der größte Pinguin der Welt.", 0.7), "payoff",
     visual("emperor penguin colony standing on ice", ["emperor penguin colony"], ["subject_b"])),
]


def test_unseen_topic_chain_needs_no_vocabulary(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, DIVE_Q, DIVE, planner_target="")
    arc = state["story_arc"]

    assert arc["primary_answer_id"] == "fact_02"
    assert arc["final_payoff_id"] == "fact_04"
    assert state["payoff_plan"]["protected_visual_target"] == "subject_a"
    assert arc["visual_protection"]["source"] == "story_arc"

    pexels = Provider(videos={
        "emperor penguin diving": [cand("pd", "emperor penguin diving", "Emperor penguin diving underwater")],
        "sperm whale diving": [cand("sw", "sperm whale diving", "Sperm whale diving into the deep ocean")],
        "deep ocean darkness": [cand("oc", "deep ocean darkness", "Deep ocean darkness below the surface")],
        "emperor penguin colony": [cand("pc", "emperor penguin colony", "Emperor penguin colony standing on ice")],
    })
    prepare_project_media(state, "project", settings_for(tmp_path), client=pexels, fallback_client=Commons(), visual_verifier=Verifier())

    for scene in state["scenes"]:
        executed = " ".join(scene["media_search"]["executed_queries"])
        if scene["story_stage"] == "before_reveal":
            assert "whale" not in executed
        assert scene["media_search"]["logical_queries_executed"] <= 3
    assert scene_by_role(state, "primary_answer")["media"]["provider_id"] == "sw"
    assert scene_by_role(state, "comparison")["media"]["provider_id"] == "pd"
    assert scene_by_role(state, "secondary_insight")["media"]["provider_id"] == "pc"


# --- compatibility matrix -------------------------------------------------------

def _strip_story(state: dict) -> None:
    state.pop("story_arc", None)
    for scene in state["scenes"]:
        for key in ("story_role", "story_unit_ids", "is_primary_answer", "is_final_payoff", "story_stage"):
            scene.pop(key, None)
        scene.get("visual_intent", {}).pop("story_role", None)
        scene.get("visual_intent", {}).pop("story_stage", None)


def _strip_visual_keys(state: dict) -> None:
    state["payoff_plan"]["protected_visual_target"] = ""
    state.get("story_arc", {}).pop("visual_protection", None)
    for scene in state["scenes"]:
        scene.get("visual_intent", {}).pop("media_query_targets", None)


@pytest.mark.parametrize("has_story, has_visual_keys", [(False, False), (True, False), (False, True), (True, True)])
def test_every_combination_of_story_and_visual_state_loads(monkeypatch, tmp_path, has_story, has_visual_keys):
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS, planner_target="subject_a")
    if not has_visual_keys:
        _strip_visual_keys(state)
    if not has_story:
        _strip_story(state)
    annotate_story_roles(state)

    evidence = next((scene for scene in state["scenes"] if scene.get("story_stage") == "before_reveal"), state["scenes"][0])
    plan = build_visual_query_plan(evidence, state)
    analyze_pacing(state)
    plan_viewer_reactions(state)
    local_review_items(state)
    _refresh_script_derivatives(state, old_scenes=copy.deepcopy(state["scenes"]))

    assert plan["queries"] and len(plan["queries"]) <= 3
    assert state["pacing_analysis"]["status"] == "ready" and state["reaction_plan"]["status"] == "ready"
    if has_story:
        # Story stages scope protection; the bound target follows the arc when keys exist.
        assert plan["protection_scope"] in {"before_reveal", "revealed"}
        expected = "subject_a" if has_visual_keys else ""
        assert state["payoff_plan"]["protected_visual_target"] == expected
        assert state["story_arc"]["visual_protection"]["source"] == ("story_arc" if has_visual_keys else "none")
    else:
        # Older projects keep project-wide protection exactly as before.
        assert plan["protection_scope"] == "project"
        assert "story_arc" not in state
        assert state["payoff_plan"]["protected_visual_target"] == ("subject_a" if has_visual_keys else "")


def test_story_without_visual_keys_still_protects_before_the_reveal_only(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS, planner_target="")
    _strip_visual_keys(state)
    annotate_story_roles(state)
    before_scene = next(scene for scene in state["scenes"] if scene.get("story_stage") == "before_reveal")
    before = copy.deepcopy(before_scene) | {"visual_intent": {"media_queries": ["schweden inseln", "indonesien inseln"]}}
    after = before | {"story_stage": "after_reveal"}

    # Same-language word protection (German payoff text vs German fallback query), stage-scoped.
    assert "schweden inseln" not in build_visual_query_plan(before, state)["queries"]
    assert "schweden inseln" in build_visual_query_plan(after, state)["queries"]


# --- edit paths ------------------------------------------------------------------

def test_edits_keep_answer_payoff_story_roles_and_visual_protection(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS, planner_target="subject_b")

    state["script"]["blocks"][-1]["text"] = "Indonesien bleibt trotzdem der größte Inselstaat der Welt."
    _refresh_script_derivatives(state, old_scenes=copy.deepcopy(state["scenes"]))
    assert state["payoff_plan"]["protected_visual_target"] == "subject_a"
    assert state["story_arc"]["visual_protection"]["planner_target"] == "subject_b"
    assert scene_by_role(state, "primary_answer")["visual_intent"]["media_query_targets"] == ["subject_a", "subject_a"]

    # A tight duration edit drops optional information, never the answer or payoff.
    state["duration"]["max_seconds"] = 10
    _refresh_script_derivatives(state, old_scenes=copy.deepcopy(state["scenes"]))
    carried = {fact_id for block in state["script"]["blocks"] for fact_id in block.get("fact_ids") or []}
    assert {"fact_02", "fact_04"} <= carried
    assert any(scene.get("is_primary_answer") for scene in state["scenes"])
    assert any(scene.get("story_stage") == "before_reveal" for scene in state["scenes"])
