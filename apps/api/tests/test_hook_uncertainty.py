"""A hook is never more certain than the research it rests on.

Research V2 keeps hypotheses hedged ("One hypothesis is ...", "may",
"vermutlich", "könnte").  The hook authority (``verbal_hook.assess_verbal``,
shared by deterministic and provider-written candidates) rejects a hook that
states such a claim as fact, claim-relative: a certain part of the same fact,
or another fact that establishes the claim, still licenses a plain statement.

Topics are fixtures only; production logic has no topic vocabulary.
"""
from __future__ import annotations

import pytest
from test_story_arc import fact
from test_triple_hook import Judge, ai_candidate, by_id, generation, plan_for, story

from clipforge.ai import HOOK_GENERATION_INSTRUCTIONS, TRIPLE_HOOK_JUDGE_INSTRUCTIONS
from clipforge.config import Settings
from clipforge.hooks import _grounded_insight, hedged, uncertainty_scopes
from clipforge.pipeline import build_initial_state
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.verbal_hook import (
    assess_verbal,
    deterministic_candidates,
    hook_context,
    overstated_certainty,
    select_verbal,
)

YAWN_Q = "Why do we yawn?"
YAWN_FACTS = [
    fact(1, "One hypothesis is that yawning cools the brain when it gets too warm.", 0.95),
    fact(2, "Researchers are still unsure why yawning is contagious, but yawning was first studied systematically in 1986."),
    fact(3, "Contagious yawning may strengthen bonds between members of a group."),
    fact(4, "Even unborn babies yawn in the womb from about eleven weeks on."),
]
GAEHN_Q = "Warum gähnen wir?"
GAEHN_FACTS = [
    fact(1, "Gähnen könnte das Gehirn abkühlen, wenn es zu warm wird.", 0.95),
    fact(2, "Vermutlich hilft Gähnen dabei, nach dem Aufwachen schneller wach zu werden."),
    fact(3, "Schon ungeborene Babys gähnen ab etwa der elften Woche im Mutterleib."),
]


def context_for(question: str, facts: list[dict], language: str) -> dict:
    fixture = story(question, [dict(item) for item in facts], language=language)
    return hook_context(
        fixture["intent"], fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=fixture["body"],
    )


def yawn() -> dict:
    return context_for(YAWN_Q, YAWN_FACTS, "en")


def gaehn() -> dict:
    return context_for(GAEHN_Q, GAEHN_FACTS, "de")


def claims(*texts: str) -> dict:
    """The minimal context the certainty check reads: the research facts."""
    return {"facts": [{"id": f"fact_{index:02d}", "claim": text} for index, text in enumerate(texts, 1)]}


def overstates(text: str, context: dict, strategy: str = "evidence_insight") -> bool:
    return "overstates_certainty" in assess_verbal(text, strategy, context)["hard_fail"]


# ---------------------------------------------------------------------------
# The rule (generic X / Y claims)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("fact_text", "hook"), [
    # 1. a hypothesis said as fact
    ("One hypothesis is that cold water triggers the nerve reflex.", "Cold water triggers the nerve reflex."),
    # researchers' suspicion said as fact
    ("Researchers suspect that cold water may trigger the nerve reflex.", "Cold water triggers the nerve reflex."),
    # 3. a modal "may" dropped
    ("Cold water may trigger the nerve reflex.", "Cold water triggers the nerve reflex."),
    # 4. German "könnte" dropped
    ("Kaltes Wasser könnte den Nervenreflex verursachen.", "Kaltes Wasser verursacht den Nervenreflex."),
    # German "vermutlich" dropped
    ("Vermutlich hilft Kälte bei Muskelkater.", "Kälte hilft bei Muskelkater."),
])
def test_a_hook_that_drops_the_hedge_is_overstated(fact_text, hook):
    assert overstated_certainty(hook, claims(fact_text))


@pytest.mark.parametrize(("fact_text", "hook"), [
    # 2. the same hypothesis, kept a hypothesis
    ("One hypothesis is that cold water triggers the nerve reflex.", "One hypothesis is that cold water triggers the nerve reflex."),
    # an equivalent hedge in other words is enough
    ("One hypothesis is that cold water triggers the nerve reflex.", "Cold water might trigger the nerve reflex."),
    ("Kaltes Wasser könnte den Nervenreflex verursachen.", "Kaltes Wasser verursacht vermutlich den Nervenreflex."),
    ("Vermutlich hilft Kälte bei Muskelkater.", "Kälte könnte bei Muskelkater helfen."),
    # a question claims nothing
    ("Cold water may trigger the nerve reflex.", "Does cold water trigger the nerve reflex?"),
])
def test_a_hook_that_keeps_the_uncertainty_is_accepted(fact_text, hook):
    assert overstated_certainty(hook, claims(fact_text)) == []


def test_a_certain_fact_licenses_a_certain_hook():
    # 8.
    assert overstated_certainty("Cold water triggers the nerve reflex.", claims("Cold water triggers the nerve reflex.")) == []


def test_an_uncertain_mechanism_does_not_taint_a_certain_date_in_the_same_fact():
    # 9. claim-relative: "unsure why" scopes the mechanism, not the date after "but".
    fact_text = "Researchers are unsure why ball lightning forms, but ball lightning was first photographed in 1950."
    assert overstated_certainty("Ball lightning was first photographed in 1950.", claims(fact_text)) == []
    # ... while the open "why" stays open: a stated cause is still overstated.
    assert overstated_certainty("Ball lightning forms from charged dust.", claims(fact_text))


def test_a_modal_hedges_its_predicate_not_its_subject():
    fact_text = "Purring vibrations between 25 and 150 hertz may help bones and tissue heal."
    assert overstated_certainty("Cats purr at 25 to 150 hertz.", claims(fact_text)) == []
    assert overstated_certainty("Purring vibrations help bones heal.", claims(fact_text))


def test_one_certain_supporting_fact_is_enough():
    # 10. another accepted fact establishes the same claim with certainty.
    context = claims("Cold water may trigger the nerve reflex.", "Experiments show that cold water triggers the nerve reflex.")
    assert overstated_certainty("Cold water triggers the nerve reflex.", context) == []
    # A certain fact about only part of the claim does not license the rest.
    partial = claims("Cold water may trigger the nerve reflex.", "Cold water lowers skin temperature.")
    assert overstated_certainty("Cold water triggers the nerve reflex.", partial)


def test_month_may_and_negated_could_are_not_hedges():
    assert not hedged("The bridge opened in May 1950.")
    assert not hedged("Early aircraft could not fly in rain.")
    assert hedged("Early aircraft could fly in rain.")
    assert not hedged("Kühle die Stelle möglichst schnell.")
    assert hedged("Möglicherweise kühlt Gähnen das Gehirn.")


def test_scopes_break_at_adversative_turns_and_sentences():
    scopes = uncertainty_scopes("Researchers are unsure why X happens, but X was first seen in 1950. X may cause Y.")
    assert [bool(scope) for _segment, scope in scopes] == [True, False, True]
    assert scopes[2][1].strip() == "cause Y."


# ---------------------------------------------------------------------------
# The hook authority: deterministic and provider candidates alike
# ---------------------------------------------------------------------------

def test_assess_verbal_rejects_a_hypothesis_stated_as_fact_and_nothing_else():
    # Every other rule accepts this hook: only the certainty gate catches it.
    result = assess_verbal("Yawning cools an overheated brain.", "evidence_insight", yawn())
    assert result["hard_fail"] == ["overstates_certainty"]
    assert assess_verbal("Contagious yawning strengthens bonds within a group.", "evidence_insight", yawn())["hard_fail"] == ["overstates_certainty"]


def test_assess_verbal_keeps_hedged_and_certain_hooks_free_of_the_gate():
    context = yawn()
    for text in (
        "One hypothesis: yawning cools an overheated brain.",
        "Contagious yawning may strengthen bonds within a group.",
        "Scientists only started studying yawning systematically in 1986.",  # 9. certain date of a hedged fact
        "Even unborn babies yawn in the womb.",  # 8. certain fact
    ):
        assert not overstates(text, context), text
    assert assess_verbal("Scientists only started studying yawning systematically in 1986.", "evidence_insight", context)["hard_fail"] == []


def test_assess_verbal_german_hedges():
    context = gaehn()
    assert overstates("Gähnen kühlt das Gehirn ab, wenn es zu warm wird.", context)
    assert overstates("Gähnen hilft dabei, nach dem Aufwachen schneller wach zu werden.", context)
    assert not overstates("Gähnen könnte das Gehirn abkühlen, wenn es zu warm wird.", context)
    assert not overstates("Schon ungeborene Babys gähnen im Mutterleib.", context)


def test_grounded_insight_never_cuts_the_hedge_off_a_long_claim():
    # 5. Previously the main clause was cut out and said as fact.
    for claim in (
        (
            "After decades of debate among biologists and physiologists, one hypothesis is that, Wrinkled fingertips are "
            "an evolutionary adaptation that improves grip on wet stones and tools."
        ),
        (
            "Forscher vermuten seit vielen Jahren, und vieles spricht dafür, Falten an den Fingerkuppen sind beim Greifen "
            "nasser Steine und Werkzeuge ein echter Vorteil für den Menschen."
        ),
    ):
        insight = _grounded_insight(claim, {})
        assert hedged(insight), insight
        assert overstated_certainty(insight, claims(claim)) == []
    # A long certain claim is still shortened to its standalone clause.
    certain = (
        "During long baths in warm water, which most people take in winter, Wrinkled fingertips are formed by "
        "blood vessels that narrow under the skin of the fingers."
    )
    assert _grounded_insight(certain, {}) != certain.rstrip(".") + "."


def test_deterministic_candidates_never_produce_an_overstated_accepted_hook():
    # 6. ``_clause`` / ``_grounded_insight`` keep the hedge of the fact they quote.
    for context in (yawn(), gaehn()):
        candidates = deterministic_candidates(context)
        assert candidates
        by_fact = {str(item["id"]): item["claim"] for item in context["facts"]}
        for candidate in candidates:
            result = assess_verbal(candidate["text"], candidate["strategy"], context, fact_ids=candidate["supported_by_fact_ids"])
            assert "overstates_certainty" not in result["hard_fail"], candidate["text"]
            sources = [by_fact[fact_id] for fact_id in candidate["supported_by_fact_ids"] if fact_id in by_fact]
            if sources and all(hedged(text) for text in sources):
                assert hedged(candidate["text"]), candidate["text"]


def test_the_selected_verbal_hook_is_never_overstated():
    # Fallbacks (the first body sentence) pass the same gate; it is never relaxed.
    for context in (yawn(), gaehn()):
        chosen = select_verbal(context, extra=[
            {"strategy": "evidence_insight", "text": "Yawning cools an overheated brain.", "supported_by_fact_ids": ["fact_01"], "reason_codes": [], "origin": "ai"},
        ])
        assert chosen is not None
        assert overstated_certainty(chosen["text"], context) == []


# ---------------------------------------------------------------------------
# Provider candidates (Triple Hook) and cost
# ---------------------------------------------------------------------------

def yawn_candidates() -> list[dict]:
    dropped = ai_candidate(
        "A", "evidence_insight", "Yawning cools an overheated brain.", "person yawning widely", ["person yawning close up"],
        action="yawning", detail="wide open mouth", payoff_fact="fact_01",
    )
    certain = ai_candidate(
        "B", "evidence_insight", "Scientists only started studying yawning systematically in 1986.", "person yawning widely",
        ["person yawning close up"], action="yawning", detail="wide open mouth", payoff_fact="fact_01",
    )
    return [dropped, certain]


def test_provider_hook_that_drops_the_hedge_is_rejected_before_the_judge():
    # 7. the provider wrote a fluent, otherwise valid hook without the hedge.
    fixture = story(YAWN_Q, [dict(item) for item in YAWN_FACTS], language="en")
    judge = Judge({"hook_a": {key: 10 for key in ("curiosity", "insight", "attention_value")}})
    plan = plan_for(fixture, yawn_candidates(), judge=judge)
    assert "overstates_certainty" in by_id(plan)["hook_a"]["hard_fail"]
    assert plan["verbal_hook"] != "Yawning cools an overheated brain."
    assert overstated_certainty(plan["verbal_hook"], hook_context(
        fixture["intent"], fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=fixture["body"],
    )) == []
    # 12. at most one judge call, and the rejected candidate never reaches it.
    assert len(judge.calls) <= 1
    for _story, items in judge.calls:
        assert all(item["verbal_hook"] != "Yawning cools an overheated brain." for item in items)


def test_pipeline_makes_no_extra_call_and_never_ships_an_overstated_hook(monkeypatch):
    # 12. one generation call, at most one judge call.
    calls = {"generation": 0}

    def provider(*_args, **_kwargs):
        calls["generation"] += 1
        return generation(yawn_candidates())

    judge = Judge()
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult(
            [{key: value for key, value in item.items() if key != "id"} for item in YAWN_FACTS],
            [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture",
        ),
    )
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", provider)
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", judge)
    state = build_initial_state(YAWN_Q, AdvancedOptions(), Settings(clipforge_ai_mode="local", openai_api_key=None))
    plan = state["script"]["triple_hook"]
    assert calls["generation"] == 1 and len(judge.calls) <= 1
    assert plan["verbal_hook"] != "Yawning cools an overheated brain."
    assert overstated_certainty(plan["verbal_hook"], {"facts": state["facts"]}) == []
    first = state["script"]["blocks"][0]
    assert first["role"] == "hook" and first["text"] == plan["verbal_hook"]


def test_prompts_tell_the_provider_not_to_strengthen_certainty():
    assert "Never be more certain than the research" in HOOK_GENERATION_INSTRUCTIONS
    assert "only hedge" in TRIPLE_HOOK_JUDGE_INSTRUCTIONS
