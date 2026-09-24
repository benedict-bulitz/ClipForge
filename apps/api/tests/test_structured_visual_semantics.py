"""Structured, language-agnostic visual semantics.

Protected payoffs, comparison sides, thumbnail subjects and verifier subjects
come from the canonical visual plan (planner-assigned target keys and
provider-facing queries), never from spelling similarity or topic tables.
"""
import copy
import types

import pytest
from test_staged_media_search import Commons, Provider, Verifier, project, settings_for

import clipforge.media as media_module
import clipforge.pipeline as pipeline_module
import clipforge.thumbnails as thumbnails_module
import clipforge.visual_verifier as verifier_module
from clipforge.ai import AIPayoffPlan, AIVisualHook, AIVisualIntent
from clipforge.media import (
    build_visual_query_plan,
    canonical_visual_subjects,
    prepare_project_media,
    scene_coverage_targets,
    visual_target_key,
)
from clipforge.payoff import build_payoff_plan, fallback_triple_hook, normalise_triple_hook
from clipforge.thumbnails import _thumbnail_visual_prompt_groups, build_thumbnail_brief
from clipforge.visual_verifier import global_subject_text

ISLAND_QUERIES = ["swedish islands", "indonesian islands", "islands aerial"]
ISLAND_TARGETS = ["subject_a", "subject_b", "shared"]


def keyed_island_state(protected: str = "subject_a", reveal: str = "Schweden") -> dict:
    scene = {
        "narration": "Welches Land hat mehr Inseln?",
        "visual_intent": {
            "visual_goal": "islands of two countries from above",
            "media_queries": list(ISLAND_QUERIES),
            "media_query_targets": list(ISLAND_TARGETS),
        },
    }
    return project(
        scene,
        intent={"topic": "Welches Land hat mehr Inseln – Schweden oder Indonesien?"},
        format_plan={"selected_format": "comparison"},
        payoff_plan={"hook_must_not_reveal": reveal, "protected_visual_target": protected},
    )


# --- protected payoff: structured identity, not spelling ---------------------

def test_cross_language_protection_uses_target_identity():
    # "Schweden" and "swedish" share no reliable stem; only the key links them.
    state = keyed_island_state("subject_a", "Schweden")
    plan = build_visual_query_plan(state["scenes"][0], state)

    assert "swedish islands" not in plan["queries"]
    assert plan["queries"][:2] == ["indonesian islands", "islands aerial"]
    assert plan["protected_targets"] == ["subject_a"]
    assert "swedish" in plan["protected_entities"]


def test_protection_holds_with_no_word_overlap_at_all():
    scene = {
        "narration": "Zwei Kandidaten treten gegeneinander an.",
        "visual_intent": {
            "visual_goal": "two flower beds side by side",
            "media_queries": ["red roses", "blue tulips", "flower bed"],
            "media_query_targets": ["subject_a", "subject_b", "shared"],
        },
    }
    protected = {"payoff_plan": {"hook_must_not_reveal": "Kandidat Zwei", "protected_visual_target": "subject_b"}}
    unprotected = {"payoff_plan": {"hook_must_not_reveal": "Kandidat Zwei"}}

    assert "blue tulips" not in build_visual_query_plan(scene, protected)["queries"]
    # Same wording without the structured key: nothing links the payoff text to a query.
    assert "blue tulips" in build_visual_query_plan(scene, unprotected)["queries"]


def test_untagged_fallback_query_reusing_the_protected_side_is_blocked():
    scene = {
        "narration": "Im Frühling blühen sie.",
        "visual_goal": "blue tulips blooming in spring",
        "visual_intent": {
            "visual_goal": "blue tulips blooming in spring",
            "media_queries": ["red roses", "blue tulips"],
            "media_query_targets": ["subject_a", "subject_b"],
        },
    }
    state = {"payoff_plan": {"hook_must_not_reveal": "Kandidat Zwei", "protected_visual_target": "subject_b"}}

    queries = build_visual_query_plan(scene, state)["queries"]

    assert queries[0] == "red roses"
    assert not any("tulip" in query for query in queries)


def test_protected_target_never_reaches_any_provider_path(tmp_path):
    state = keyed_island_state("subject_a", "Schweden")
    pexels = Provider()  # nothing found: staged, Wikimedia and relaxed paths all run
    commons = Commons()

    prepare_project_media(state, "project", settings_for(tmp_path), client=pexels, fallback_client=commons, visual_verifier=Verifier())

    search = state["scenes"][0]["media_search"]
    searched = " ".join(pexels.queries + commons.queries).casefold()
    assert search["relaxed_fallback"] is True
    assert "swedish" not in searched and "schweden" not in searched
    assert search["logical_queries_executed"] <= 3
    assert "swedish" not in search["coverage_targets"]


def test_keyed_plan_takes_comparison_sides_from_target_keys():
    state = keyed_island_state(protected="", reveal="")
    scene = state["scenes"][0]
    plan = build_visual_query_plan(scene, state)

    assert plan["side_keys"] == {"swedish": "subject_a", "indonesian": "subject_b"}
    assert scene_coverage_targets(scene, state, plan)["targets"] == {
        "swedish": "subject_a", "indonesian": "subject_b", "island": "shared",
    }

    single = copy.deepcopy(scene)
    single["visual_intent"].update(media_queries=["indonesian islands", "islands aerial"], media_query_targets=["subject_b", "shared"])
    single_plan = build_visual_query_plan(single, state)
    assert scene_coverage_targets(single, state, single_plan)["targets"] == {"indonesian": "subject_a", "island": "shared"}


def test_target_keys_are_normalised_and_opaque():
    assert visual_target_key(" Subject-B ") == "subject_b"
    assert visual_target_key("zwei kandidaten") == "zwei_kandidaten"
    assert visual_target_key("side/../a") == ""
    assert visual_target_key(None) == ""


# --- planner plumbing ---------------------------------------------------------

def test_ai_schema_fields_are_optional_and_additive():
    assert AIVisualIntent(visual_goal="x y").media_query_targets == []
    assert AIVisualHook(visual_goal="x y").media_query_targets == []
    assert AIPayoffPlan(curiosity_question="q", payoff="p").protected_visual_target == ""


def test_payoff_plan_carries_protected_visual_target():
    intent = {"question": "Welches Land hat mehr Inseln?", "language": "de"}
    blocks = [{"role": "support", "text": "Beide haben viele Inseln."}, {"role": "payoff", "text": "Schweden gewinnt."}]

    protected = build_payoff_plan(intent, blocks, supplied={"hook_must_not_reveal": "Schweden", "protected_visual_target": "Subject-A"})
    unprotected = build_payoff_plan({"question": "How do tides work?"}, blocks, supplied={"protected_visual_target": "subject_a"})

    assert protected["protected_visual_target"] == "subject_a"
    assert build_payoff_plan(intent, blocks, supplied=protected)["protected_visual_target"] == "subject_a"
    assert unprotected["protected_visual_target"] == ""


def test_visual_hook_keeps_query_targets_aligned():
    plan = {"hook_must_not_reveal": "", "reveal_policy": "immediate_context_allowed"}
    fallback = fallback_triple_hook({"topic": "Katzen oder Hunde"}, plan, "Hook?", None)
    candidate = {
        "on_screen_text_hook": "WHO HEARS MORE?",
        "visual_hook": {
            "visual_goal": "cat and dog ears side by side",
            "media_queries": ["cat ears closeup", "   ", "dog ears closeup"],
            "media_query_targets": ["subject_a", "shared", "subject_b"],
        },
    }

    visual = normalise_triple_hook(candidate, fallback, plan)["visual_hook"]

    assert [query.rstrip(".") for query in visual["media_queries"]] == ["cat ears closeup", "dog ears closeup"]
    assert visual["media_query_targets"] == ["subject_a", "subject_b"]
    assert fallback["visual_hook"]["media_query_targets"] == []


# --- arbitrary topics: thumbnails and verifier ---------------------------------

def pets_state() -> dict:
    def scene(scene_id: str, queries: list[str], targets: list[str]) -> dict:
        return {"id": scene_id, "visual_intent": {"media_queries": queries, "media_query_targets": targets}}

    return {
        "intent": {"topic": "Hören Katzen oder Hunde besser?", "question": "Hören Katzen oder Hunde besser?", "language": "de"},
        "format_plan": {"selected_format": "comparison"},
        "payoff_plan": {"hook_must_not_reveal": "Katzen", "protected_visual_target": "subject_a"},
        "scenes": [
            scene("s1", ["cat ears closeup", "dog ears closeup"], ["subject_a", "subject_b"]),
            scene("s2", ["dog ears listening", "pet ears"], ["subject_b", "shared"]),
        ],
    }


def test_thumbnail_subjects_and_prompts_come_from_the_canonical_plan():
    brief = build_thumbnail_brief(pets_state())
    prompts = _thumbnail_visual_prompt_groups(brief)

    assert brief["comparison_visual_subjects"] == ["cat", "dog"]
    assert {"ear", "cat", "dog"} <= set(brief["primary_visual_subjects"])
    assert "cat ear" in prompts["primary"]
    assert "dog ear" in prompts["secondary"]
    assert not any("katzen" in value or "hunde" in value for group in prompts.values() for value in group)


def test_canonical_subjects_for_an_unkeyed_arbitrary_comparison():
    state = {"scenes": [{"visual_intent": {"media_queries": ["espresso cup", "matcha cup", "hot drink steam"]}}]}

    subjects = canonical_visual_subjects(state)

    assert subjects["concepts"] == ["cup"]
    assert subjects["sides"][:2] == ["espresso", "matcha"]


def test_verifier_subject_prefers_canonical_plan_over_topic_string():
    # Shared-target concepts of the plan, most used first; no script-language words.
    assert global_subject_text(pets_state()) == "ear pet"
    # Without a plan: generic content words, no topic-specific trimming.
    assert global_subject_text({"intent": {"topic": "Welches Land hat mehr Inseln?"}}) == "land inseln"
    assert verifier_module._subject_tokens("airplane window breather hole") == ["airplane", "window", "breather"]


def test_pipeline_fallback_visual_intent_is_topic_independent():
    pets = pipeline_module._fallback_visual_intent("Unsere Haustiere schlafen gern in der Sonne.", "de")
    coast = pipeline_module._fallback_visual_intent("Ein alter Leuchtturm steht an der Küste.", "de")

    assert pets["media_queries"] == [pets["visual_goal"]]
    assert "house" not in pets["visual_goal"] and "construction" not in pets["visual_goal"]
    assert "Haustiere" in pets["visual_goal"]
    assert coast["visual_goal"].startswith("alter Leuchtturm")
    assert pipeline_module._fallback_visual_intent("", "de")["visual_goal"] == "visuelle Erklärung"


# --- source guard: no topic tables in visual-semantics code ------------------

TOPIC_WORDS = {
    "sweden", "schweden", "swedish", "indonesia", "indonesien", "indonesian", "island", "islands", "insel",
    "inseln", "archipelago", "archipel", "egypt", "egyptian", "sudan", "sudanese", "nubia", "kush",
    "pyramid", "pyramids", "airplane", "flugzeug", "cheetah", "gepard", "lighthouse", "leuchtturm",
    "volcano", "volcanic", "firefly", "breath", "atem", "haus", "house", "tropical", "breather", "keeper",
}


def _word_constants(code: types.CodeType) -> set[str]:
    """Single-word string constants in code (table entries), not prose or prompts."""
    found: set[str] = set()
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            found |= _word_constants(const)
        elif isinstance(const, str) and const.isalpha():
            found.add(const.casefold())
        elif isinstance(const, frozenset | tuple):
            found |= {item.casefold() for item in const if isinstance(item, str) and item.isalpha()}
    return found


def _module_word_constants(module: types.ModuleType) -> set[str]:
    found: set[str] = set()
    for value in vars(module).values():
        if isinstance(value, dict):
            value = [*value.keys(), *(item for item in value.values() if isinstance(item, str))]
        if isinstance(value, set | frozenset | list | tuple):
            found |= {item.casefold() for item in value if isinstance(item, str) and item.isalpha()}
        elif isinstance(value, types.FunctionType) and value.__module__ == module.__name__:
            found |= _word_constants(value.__code__)
    return found


@pytest.mark.parametrize("module", [media_module, thumbnails_module, verifier_module])
def test_visual_semantics_modules_carry_no_topic_tables(module):
    assert not _module_word_constants(module) & TOPIC_WORDS


def test_pipeline_fallback_intent_carries_no_topic_templates():
    assert not _word_constants(pipeline_module._fallback_visual_intent.__code__) & TOPIC_WORDS


def test_removed_topic_tables_stay_removed():
    for module, names in (
        (media_module, ("_VISUAL_QUERY_ALIASES", "_PROVIDER_TERMS", "_VISUAL_ENTITY_ADJECTIVES", "_COVERAGE_SYNONYMS", "_AMBIGUOUS_LOCAL_TERMS")),
        (thumbnails_module, ("_VISUAL_ALIASES", "_TERM_ALIASES", "_VISUAL_SUBJECT_VOCAB")),
    ):
        for name in names:
            assert not hasattr(module, name), name
