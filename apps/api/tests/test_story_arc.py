"""Story / information arc: roles, order, reveal protection and integration.

Topics here are test fixtures only; the production arc has no topic vocabulary.
"""
import copy
import types
from pathlib import Path

import pytest

import clipforge.story_arc as story_module
from clipforge.ai import AIHookGenerationResult
from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.novelty import build_novelty_plan
from clipforge.pacing import analyze_pacing
from clipforge.pipeline import (
    _fit_blocks,
    _generate_body_with_v2_or_fallback,
    _refresh_script_derivatives,
    build_initial_state,
)
from clipforge.reactions import plan_viewer_reactions
from clipforge.research import ResearchResult
from clipforge.review import local_review_items
from clipforge.schemas import AdvancedOptions
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.story_arc import (
    annotate_story_roles,
    build_story_arc,
    safe_story_arc,
    story_arc_issues,
    story_brief,
    story_script_issues,
)


def fact(index: int, claim: str, importance: float = 0.8) -> dict:
    return {
        "id": f"fact_{index:02d}",
        "claim": claim,
        "importance": importance,
        "confidence": 0.9,
        "verification": "source_attributed",
        "sources": [{"label": f"s{index}", "url": f"https://s{index}.test/a"}, {"label": "x", "url": f"https://x{index}.test/b"}],
    }


ISLANDS_Q = "Welches Land hat mehr Inseln – Schweden oder Indonesien?"
ISLANDS = [
    fact(1, "Indonesien hat etwa 17.000 Inseln."),
    fact(2, "Schweden hat rund 267.570 Inseln – mehr als jedes andere Land der Welt.", 0.95),
    fact(3, "In der Eiszeit haben Gletscher Schwedens Küste zerklüftet; dadurch entstanden unzählige kleine Inseln."),
    fact(4, "Indonesien ist trotzdem der größte Inselstaat der Welt.", 0.7),
]
FIREFLY_Q = "Warum leuchten Glühwürmchen?"
FIREFLY = [
    fact(1, "Glühwürmchen leuchten, weil in ihrem Hinterleib eine chemische Reaktion Licht erzeugt.", 0.95),
    fact(2, "Dabei reagiert der Stoff Luciferin mit Sauerstoff, und das Enzym Luciferase beschleunigt die Reaktion."),
    fact(3, "Das Licht dient vor allem dazu, Partner anzulocken.", 0.6),
    fact(4, "Fast die ganze Energie wird zu Licht und kaum zu Wärme – deshalb spricht man von kaltem Licht."),
]
TREES_Q = "Was ist größer: Anzahl der Bäume auf der Erde oder Sterne in der Milchstraße?"
TREES = [
    fact(1, "Die Milchstraße enthält schätzungsweise 100 bis 400 Milliarden Sterne."),
    fact(2, "Auf der Erde wachsen rund drei Billionen Bäume."),
    fact(3, "Es gibt also mehr Bäume auf der Erde als Sterne in der Milchstraße.", 0.95),
    fact(4, "Die Zahl der Bäume ist seit Beginn der Landwirtschaft etwa um die Hälfte gesunken.", 0.5),
]
RANKING_Q = "Top 5 der längsten Flüsse der Welt"
RANKING = [
    fact(1, "Platz 1: Der Nil ist etwa 6.650 Kilometer lang."),
    fact(2, "Platz 2: Der Amazonas misst rund 6.400 Kilometer."),
    fact(3, "Platz 3: Der Jangtsekiang ist etwa 6.300 Kilometer lang."),
    fact(4, "Platz 4: Der Mississippi-Missouri kommt auf rund 6.275 Kilometer."),
    fact(5, "Platz 5: Der Jenissei ist etwa 5.540 Kilometer lang."),
]


def arc_for(question: str, facts: list[dict], *, protected: bool | None = None, supplied: dict | None = None) -> dict:
    intent = {"question": question, "topic": question, "language": "de"}
    novelty = build_novelty_plan(intent, facts)
    format_plan = plan_format(intent, facts, [], novelty)
    if protected is None:
        protected = format_plan["selected_format"] in {"comparison", "quiz"}
    return build_story_arc(intent, facts, format_plan, novelty, protected=protected, supplied=supplied)


def units(arc: dict) -> dict:
    return {unit["id"]: unit for unit in arc["units"]}


def settings(tmp_path: Path, **updates) -> Settings:
    values = {"clipforge_ai_mode": "local", "openai_api_key": None, "render_root": tmp_path, **updates}
    return Settings(**values)


def generate(monkeypatch, tmp_path: Path, question: str, facts: list[dict]) -> dict:
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult(
            [{key: value for key, value in item.items() if key != "id"} for item in facts],
            [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture",
        ),
    )
    return build_initial_state(question, AdvancedOptions(), settings(tmp_path))


def body_fact_order(state: dict) -> list[str]:
    order: list[str] = []
    for block in state["script"]["blocks"]:
        for fact_id in block.get("fact_ids") or []:
            if fact_id not in order:
                order.append(fact_id)
    return order


# --- A) comparison -----------------------------------------------------------

def test_comparison_primary_answer_is_not_the_secondary_insight():
    arc = arc_for(ISLANDS_Q, ISLANDS)
    by_id = units(arc)

    assert arc["structure"] == "reveal"
    assert arc["primary_answer_id"] == "fact_02"  # Sweden has more islands
    assert by_id["fact_04"]["role"] == "secondary_insight"  # largest island *nation*
    assert arc["final_payoff_id"] == "fact_04"  # answer and final payoff differ
    assert arc["order"] == ["fact_01", "fact_02", "fact_03", "fact_04"]
    assert by_id["fact_02"]["depends_on"] == ["fact_01"]
    assert "fact_02" in by_id["fact_03"]["depends_on"] and "fact_02" in by_id["fact_04"]["depends_on"]
    assert arc["curiosity_gap"]["withhold_answer"] is True
    assert arc["curiosity_gap"]["closed_by"] == "fact_02"
    assert set(arc["hook"]["protected_ids"]) == {"fact_02", "fact_03", "fact_04"}
    assert arc["issues"] == []


def test_comparison_pipeline_protects_the_real_answer_and_keeps_story_order(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS)
    arc = state["story_arc"]
    hook = state["script"]["blocks"][0]

    # The protected reveal is the primary answer, not the last (secondary) block.
    assert state["payoff_plan"]["hook_must_not_reveal"].rstrip(".") == "Schweden"
    assert state["payoff_plan"]["primary_answer_id"] == "fact_02"
    assert state["payoff_plan"]["final_payoff_id"] == "fact_04"
    assert hook["role"] == "hook" and "schweden" not in hook["text"].casefold()
    assert body_fact_order(state) == ["fact_01", "fact_02", "fact_03", "fact_04"]
    assert arc["completeness"] == {"mapped": True, "complete": True, "missing_required_ids": []}
    assert arc["script_issues"] == []
    triple = state["script"]["triple_hook"]["story_brief"]
    assert triple["withhold_answer"] is True and "fact_02" in triple["protected_fact_ids"]


def test_scene_reaction_pacing_and_visual_systems_receive_story_roles(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS)
    scenes = state["scenes"]
    answer = next(scene for scene in scenes if scene.get("is_primary_answer"))
    final = next(scene for scene in scenes if scene.get("is_final_payoff"))

    assert answer["story_role"] == "primary_answer" and answer["story_stage"] == "reveal"
    assert answer["visual_intent"]["story_role"] == "primary_answer"
    assert scenes[0]["story_stage"] == "before_reveal"
    reactions = {entry["scene_id"]: entry for entry in state["reaction_plan"]["scene_reactions"]}
    assert reactions[answer["id"]]["intended_reaction"] == state["reaction_plan"]["payoff_reaction"]
    assert reactions[final["id"]]["intended_reaction"] == "surprise"
    assessments = {item["scene_id"]: item for item in state["pacing_analysis"]["scene_assessments"]}
    assert assessments[answer["id"]]["story_role"] == "primary_answer"
    assert assessments[final["id"]]["purpose"] == "payoff"
    assert not any(
        item["story_required"] and item["recommendation"] in {"TRIM", "SHORTEN_POST_PAYOFF"}
        for item in assessments.values()
    )


# --- B) explanation ----------------------------------------------------------

def test_explainer_answers_early_and_keeps_the_causal_chain(monkeypatch, tmp_path):
    arc = arc_for(FIREFLY_Q, FIREFLY, protected=False)

    assert arc["structure"] == "answer_first"
    assert arc["primary_answer_id"] == "fact_01"
    assert arc["curiosity_gap"]["withhold_answer"] is False
    assert units(arc)["fact_01"]["may_appear_in_hook"] is True  # no artificial mystery
    assert arc["order"] == ["fact_01", "fact_02", "fact_03", "fact_04"]
    assert arc["final_payoff_id"] == "fact_04"

    state = generate(monkeypatch, tmp_path, FIREFLY_Q, FIREFLY)
    assert state["payoff_plan"]["hook_must_not_reveal"] == ""
    assert body_fact_order(state) == ["fact_01", "fact_02", "fact_03", "fact_04"]
    assert state["story_arc"]["completeness"]["complete"] is True


# --- C) question / reveal ----------------------------------------------------

def test_reveal_answer_cannot_leak_before_its_evidence(monkeypatch, tmp_path):
    arc = arc_for(TREES_Q, TREES)
    assert arc["primary_answer_id"] == "fact_03"
    assert {"fact_01", "fact_02"} <= set(units(arc)["fact_03"]["depends_on"])
    assert arc["order"][-1] == "fact_03"
    assert units(arc)["fact_03"]["may_appear_in_hook"] is False

    state = generate(monkeypatch, tmp_path, TREES_Q, TREES)
    order = body_fact_order(state)
    assert order.index("fact_03") > max(order.index("fact_01"), order.index("fact_02"))
    hook = next(block for block in state["script"]["blocks"] if block["role"] == "hook") if state["script"]["blocks"][0]["role"] == "hook" else None
    assert hook is None or "fact_03" not in (hook.get("fact_ids") or [])
    assert state["story_arc"]["script_issues"] == []


def test_validator_catches_a_reveal_leaking_into_the_hook_or_before_evidence():
    arc = arc_for(TREES_Q, TREES)
    state = {"story_arc": arc, "script": {"blocks": [
        {"id": "b1", "role": "hook", "text": "…", "fact_ids": ["fact_03"]},
        {"id": "b2", "role": "support", "text": "…", "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "support", "text": "…", "fact_ids": ["fact_02", "fact_04"]},
    ]}}

    issues = story_script_issues(state)

    assert "protected_reveal_in_hook" in issues
    assert "fact_before_dependency:fact_03" in issues


# --- D) ranking --------------------------------------------------------------

def test_ranking_progression_is_the_story_structure(monkeypatch, tmp_path):
    arc = arc_for(RANKING_Q, RANKING, protected=False)

    assert arc["structure"] == "ranked_progression"
    assert arc["order"] == ["fact_05", "fact_04", "fact_03", "fact_02", "fact_01"]
    assert arc["primary_answer_id"] == arc["final_payoff_id"] == "fact_01"
    assert all(unit["role"] == "ranked_item" for unit in arc["units"])
    assert units(arc)["fact_02"]["depends_on"] == ["fact_03"]
    assert "no_primary_answer" not in arc["issues"] and arc["issues"] == []

    state = generate(monkeypatch, tmp_path, RANKING_Q, RANKING)
    assert body_fact_order(state) == ["fact_05", "fact_04", "fact_03", "fact_02", "fact_01"]
    assert state["script"]["blocks"][-1]["role"] == "payoff"


def test_ranking_without_explicit_ranks_uses_research_order():
    items = [fact(1, "Der Gepard erreicht über 100 km/h."), fact(2, "Der Gabelbock läuft rund 88 km/h."), fact(3, "Der Springbock schafft etwa 80 km/h.")]
    arc = arc_for("Top 3 der schnellsten Landtiere", items, protected=False)

    assert arc["order"] == ["fact_03", "fact_02", "fact_01"]
    assert arc["primary_answer_id"] == "fact_01"
    assert "rank_basis_research_order" in arc["repairs"]


# --- E) arbitrary unseen topic -----------------------------------------------

def test_unseen_topics_need_no_vocabulary(monkeypatch, tmp_path):
    cats = [
        fact(1, "Cats purr because muscles in the larynx twitch rapidly and vibrate the vocal folds.", 0.95),
        fact(2, "The purr is produced on both the inhale and the exhale."),
        fact(3, "Cats also purr when injured, which may help them calm down.", 0.6),
    ]
    state = generate(monkeypatch, tmp_path, "Why do cats purr?", cats)
    assert state["story_arc"]["primary_answer_id"] == "fact_01"
    assert body_fact_order(state)[0] == "fact_01"
    assert state["story_arc"]["completeness"]["complete"] is True

    tea = [
        fact(1, "A cup of green tea contains about 30 milligrams of caffeine."),
        fact(2, "A cup of coffee contains about 95 milligrams of caffeine, more than green tea.", 0.95),
        fact(3, "Green tea is the oldest of the two drinks, used for thousands of years.", 0.6),
    ]
    arc = arc_for("Which has more caffeine, coffee or green tea?", tea)
    assert arc["primary_answer_id"] == "fact_02"
    assert units(arc)["fact_03"]["role"] == "secondary_insight"  # different dimension (age)
    assert arc["order"].index("fact_01") < arc["order"].index("fact_02")


TOPIC_WORDS = {"schweden", "sweden", "indonesien", "indonesia", "inseln", "island", "glühwürmchen", "firefly", "bäume", "tree", "sterne", "star", "fluss", "river", "nil", "cat", "caffeine"}


def test_story_arc_code_has_no_topic_vocabulary():
    found: set[str] = set()

    def scan(code: types.CodeType) -> None:
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                scan(const)
            elif isinstance(const, str) and const.isalpha():
                found.add(const.casefold())
            elif isinstance(const, frozenset | tuple):
                found.update(item.casefold() for item in const if isinstance(item, str) and item.isalpha())

    for value in vars(story_module).values():
        if isinstance(value, set | frozenset | list | tuple):
            found.update(item.casefold() for item in value if isinstance(item, str))
        elif isinstance(value, dict):
            for members in value.values():
                if isinstance(members, set | frozenset):
                    found.update(members)
            found.update(str(key) for key in value)
        elif isinstance(value, types.FunctionType) and value.__module__ == story_module.__name__:
            scan(value.__code__)
    assert not found & TOPIC_WORDS


# --- planner arc, repair and fallback ------------------------------------------

def test_planner_supplied_arc_is_merged_and_repaired():
    supplied = {
        "units": [
            {"fact_index": 4, "role": "secondary_insight"},
            {"fact_index": 2, "role": "primary_answer", "depends_on": [1]},
            {"fact_index": 3, "role": "explanation", "depends_on": [2]},
            {"fact_index": 1, "role": "evidence", "depends_on": [3]},  # creates a cycle
            {"fact_index": 9, "role": "evidence"},  # unknown fact
            {"fact_index": 1, "role": "made_up_role"},
        ],
        "primary_answer_index": 2,
        "final_payoff_index": 4,
        "curiosity_gap": "Welches der beiden Länder liegt vorn?",
    }
    arc = arc_for(ISLANDS_Q, ISLANDS, supplied=supplied)

    assert arc["source"] == "planner" and arc["status"] == "repaired"
    assert arc["primary_answer_id"] == "fact_02" and arc["final_payoff_id"] == "fact_04"
    assert arc["curiosity_gap"]["planner_text"] == "Welches der beiden Länder liegt vorn?"
    assert any(repair.startswith("dependency_cycle_broken") for repair in arc["repairs"])
    assert "dropped_unit_with_unknown_fact" in arc["repairs"]
    assert "unknown_role_made_up_role" in arc["repairs"]
    assert "dependency_cycle" not in story_arc_issues(arc)


def test_invalid_planner_references_fall_back_to_deterministic_answer():
    arc = arc_for(ISLANDS_Q, ISLANDS, supplied={"primary_answer_index": 99, "final_payoff_index": 42, "units": []})

    assert arc["primary_answer_id"] == "fact_02"
    assert {"primary_answer_reference_repaired", "final_payoff_reference_repaired"} <= set(arc["repairs"])


def test_story_planning_failure_never_fails_generation(monkeypatch, tmp_path):
    def explode(*_args, **_kwargs):
        raise RuntimeError("planner bug")

    monkeypatch.setattr(story_module, "build_story_arc", explode)
    arc = safe_story_arc({"question": "Q?"}, ISLANDS, {"selected_format": "comparison"})
    assert arc["status"] == "fallback" and arc["primary_answer_id"] == "fact_01"

    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS)
    assert state["story_arc"]["status"] == "fallback"
    assert state["script"]["blocks"]


# --- validator ------------------------------------------------------------------

def test_arc_validator_reports_structural_problems():
    arc = arc_for(ISLANDS_Q, ISLANDS)
    broken = copy.deepcopy(arc)
    by_id = units(broken)
    by_id["fact_01"]["depends_on"] = ["fact_03"]  # fact_03 -> fact_02 -> fact_01 -> fact_03
    broken["final_payoff_id"] = "fact_99"
    by_id["fact_02"]["role"] = "secondary_insight"

    issues = story_arc_issues(broken)

    assert "dependency_cycle" in issues
    assert "final_payoff_unknown_fact" in issues
    assert "secondary_insight_as_primary_answer" in issues
    assert "no_primary_answer" in story_arc_issues({**arc, "primary_answer_id": None})


def test_script_validator_reports_missing_duplicate_misplaced_and_filler():
    arc = arc_for(ISLANDS_Q, ISLANDS)
    state = {"story_arc": arc, "script": {"blocks": [
        {"id": "b1", "role": "support", "text": "…", "fact_ids": ["fact_01"]},
        {"id": "b2", "role": "answer", "text": "…", "fact_ids": ["fact_04"]},
        {"id": "b3", "role": "support", "text": "…", "fact_ids": ["fact_01"]},
        {"id": "b4", "role": "payoff", "text": "…", "fact_ids": ["fact_04"]},
        {"id": "b5", "role": "support", "text": "Folge für mehr!", "fact_ids": []},
    ]}}

    issues = story_script_issues(state)

    assert "primary_answer_missing_from_script" in issues
    assert "required_fact_missing:fact_02" in issues
    assert "secondary_insight_presented_as_answer" in issues
    assert "duplicate_information_block:b3" in issues
    assert "filler_after_final_payoff" in issues


def test_review_layer_reports_story_issues_as_warnings(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS)
    state["script"]["blocks"][1]["fact_ids"] = ["fact_04"]  # answer slot now carries the insight

    items = local_review_items(state)

    story_items = [item for item in items if item["message"].startswith("Story-arc issue")]
    assert story_items and all(item["severity"] == "warning" for item in story_items)


# --- integration: writer, hook, fitting, edits, old projects, TTS ---------------

def test_script_writer_receives_the_story_arc(tmp_path):
    captured = {}

    class Writer:
        name = "fixture"

        def generate(self, request):
            captured["request"] = request
            draft = ScriptDraftV2(language="de", blocks=[
                ScriptBlockV2(role="support", text=ISLANDS[0]["claim"], fact_ids=["fact_01"]),
                ScriptBlockV2(role="answer", text=ISLANDS[1]["claim"], fact_ids=["fact_02"]),
            ])
            return ScriptWriterResult(draft, "connected")

    arc = arc_for(ISLANDS_Q, ISLANDS)
    _blocks, diagnostics = _generate_body_with_v2_or_fallback(
        ISLANDS_Q, {"language": "de", "content_type": "factual_explainer"}, AdvancedOptions(),
        settings(tmp_path, openai_api_key="test-key"), copy.deepcopy(ISLANDS), [], provider=Writer(), story_arc=arc,
    )

    story = captured["request"].story_arc
    assert diagnostics["status"] == "v2_success"
    assert story["primary_answer_id"] == "fact_02" and story["final_payoff_id"] == "fact_04"
    assert [item["fact_id"] for item in story["information_order"]] == arc["order"]
    assert story == story_brief(arc)


def test_hook_generation_receives_the_story_arc(monkeypatch, tmp_path):
    captured = {}

    def fake_hooks(*_args, story_arc=None, **_kwargs):
        captured["story_arc"] = story_arc
        return AIHookGenerationResult([], None, "missing_key")

    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", fake_hooks)
    generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS)

    assert captured["story_arc"]["primary_answer_id"] == "fact_02"
    assert captured["story_arc"]["curiosity_gap"]["withhold_answer"] is True


def test_duration_fitting_drops_optional_information_before_required():
    arc = arc_for(ISLANDS_Q, ISLANDS)
    arc = copy.deepcopy(arc)
    units(arc)["fact_03"]["may_be_omitted"] = True
    long = " ".join(["Wort"] * 30)
    blocks = [
        {"role": "support", "text": long + ".", "fact_ids": ["fact_01"]},
        {"role": "answer", "text": long + ".", "fact_ids": ["fact_02"]},
        {"role": "explanation", "text": long + ".", "fact_ids": ["fact_03"]},
        {"role": "payoff", "text": long + ".", "fact_ids": ["fact_04"]},
    ]

    fitted = _fit_blocks(blocks, max_duration=33, wpm=165, story_arc=arc)
    unstructured = _fit_blocks(blocks, max_duration=33, wpm=165)

    kept = [block["fact_ids"][0] for block in fitted]
    assert "fact_03" not in kept and {"fact_02", "fact_04"} <= set(kept)
    assert [block["fact_ids"][0] for block in unstructured][-1] != "fact_04"  # old behaviour: trailing first


def test_edits_keep_story_annotation_and_real_tts_timing_is_untouched(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS)
    timings = [(scene["start"], scene["end"]) for scene in state["scenes"]]
    duration = copy.deepcopy(state["duration"])

    annotate_story_roles(state)
    assert [(scene["start"], scene["end"]) for scene in state["scenes"]] == timings
    assert state["duration"] == duration
    assert not any("second" in key for unit in state["story_arc"]["units"] for key in unit)

    state["script"]["blocks"][-1]["text"] = "Indonesien bleibt trotzdem der größte Inselstaat."
    _refresh_script_derivatives(state, old_scenes=copy.deepcopy(state["scenes"]))
    assert any(scene.get("is_final_payoff") for scene in state["scenes"])
    assert state["story_arc"]["completeness"]["complete"] is True


def test_old_projects_without_story_arc_stay_loadable(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS)
    state.pop("story_arc")
    for scene in state["scenes"]:
        for key in ("story_role", "story_unit_ids", "is_primary_answer", "is_final_payoff", "story_stage"):
            scene.pop(key, None)
        scene.get("visual_intent", {}).pop("story_role", None)

    assert annotate_story_roles(state) is None
    assert story_script_issues(state) == []
    analyze_pacing(state)
    plan_viewer_reactions(state)
    _refresh_script_derivatives(state, old_scenes=copy.deepcopy(state["scenes"]))
    local_review_items(state)
    assert state["pacing_analysis"]["status"] == "ready"
    assert state["reaction_plan"]["status"] == "ready"
    assert "story_arc" not in state


def test_novelty_redundant_fact_is_optional_and_left_out(monkeypatch, tmp_path):
    facts = [*ISLANDS, fact(5, "Indonesien hat etwa 17.000 Inseln.", 0.4)]
    arc = arc_for(ISLANDS_Q, facts)
    assert units(arc)["fact_05"]["novelty"] == "redundant"
    assert units(arc)["fact_05"]["may_be_omitted"] is True

    state = generate(monkeypatch, tmp_path, ISLANDS_Q, facts)
    assert "fact_05" not in body_fact_order(state)
    assert state["story_arc"]["completeness"]["complete"] is True


@pytest.mark.parametrize("question, facts", [(ISLANDS_Q, ISLANDS), (FIREFLY_Q, FIREFLY), (TREES_Q, TREES), (RANKING_Q, RANKING)])
def test_every_arc_is_persisted_and_valid(monkeypatch, tmp_path, question, facts):
    state = generate(monkeypatch, tmp_path, question, facts)
    arc = state["story_arc"]

    assert arc["version"] == 1 and arc["status"] == "planned"
    assert arc["issues"] == [] and arc["script_issues"] == []
    assert state["information_plan"]["story_order"] == arc["order"]
    assert {unit["id"] for unit in arc["units"]} == {item["id"] for item in facts}
